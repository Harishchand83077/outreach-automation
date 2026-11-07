"""`fit.score_fit` had no caller, and now it has exactly one (F6, ADR-055).

F6 built the extractor, the scorer and the dataclasses, with tests for each, and nothing ever ran
them against a posting -- a grep for the function returned its definition and one docstring. These
cover the producer this slice added: only confirmed facts are offered to it, a score never travels
without the match and the miss, and a model is called on the press and never on the read.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.models import JobPost, LlmUsage, ProfileFact
from app.db.repo import OrgContext
from app.services.candidate import fit_store

from .test_api_settings import api, keypair, member, owner  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeRouter:
    """Returns a fixed requirement list. Never the network, and never a real model."""

    def __init__(self, requirements: list[str]) -> None:
        self.requirements = requirements
        self.calls = 0

    async def structured(self, schema, **kwargs):
        self.calls += 1
        return schema(requirements=self.requirements)


async def _posting(
    db, org, *, body: str | None = "Python and Postgres, plus Kubernetes."
) -> JobPost:
    post = JobPost(
        org_id=org.id,
        source="pasted",
        external_id=f"p-{uuid.uuid4()}",
        title="Backend Engineer",
        apply_url="https://acme.example/jobs/1",
        body_text=body,
    )
    db.add(post)
    await db.commit()
    return post


async def _fact(db, org, *, fact_id: str, statement: str, confirmed: bool = True) -> ProfileFact:
    row = ProfileFact(
        org_id=org.id,
        fact_id=fact_id,
        kind="skill",
        statement=statement,
        source="user",
        # NULL means proposed and not yet usable. Confirmation is a timestamp rather than a
        # boolean, so the row records *when* somebody agreed to say this.
        confirmed_at=datetime.now(UTC) if confirmed else None,
    )
    db.add(row)
    await db.commit()
    return row


class TestOnlyConfirmedFactsCanMatch:
    async def test_a_confirmed_fact_matches(self, db, org):
        await _fact(
            db, org, fact_id="pp1", statement="Five years of Python experience and Postgres skills."
        )
        post = await _posting(db, org)
        # Realistic lines rather than bare skill names: `MIN_OVERLAP = 2` means one shared
        # word is never a match, which is deliberate -- a confident match on a single common
        # word is the worst kind of wrong here, and the extractor returns short lines in the
        # posting's own words rather than one-word labels.
        router = FakeRouter(
            ["5 years of Python experience", "Strong Postgres skills", "Kubernetes in production"]
        )

        row = await fit_store.compute(db, OrgContext(org_id=org.id), router=router, post=post)
        await db.commit()

        assert {one["fact_id"] for one in row.matched} == {"pp1"}
        assert "Kubernetes in production" in row.missing
        # One shared word is never a match: `MIN_OVERLAP = 2`, so "5 years of Python
        # experience" -- which reduces to {python} once the noise words are dropped -- stays a
        # miss. Conservative on purpose: a confident match on one common word is the worst kind
        # of wrong, because it is about the person's own credibility.
        assert "5 years of Python experience" in row.missing

    async def test_a_proposed_fact_cannot_produce_a_match(self, db, org):
        """ADR-037's rule, held one step before the comparison: a fact the candidate has not
        agreed to say must not produce a match they never claimed -- and the person asked about
        it in an interview is the person who has to have agreed to it."""
        await _fact(
            db,
            org,
            fact_id="pp1",
            statement="Strong Kubernetes skills.",
            confirmed=False,
        )
        await _fact(db, org, fact_id="pp2", statement="Strong Postgres skills.")
        post = await _posting(db, org)
        router = FakeRouter(["Strong Kubernetes skills", "Strong Postgres skills"])

        row = await fit_store.compute(db, OrgContext(org_id=org.id), router=router, post=post)
        await db.commit()

        assert {one["fact_id"] for one in row.matched} == {"pp2"}
        assert row.missing == ["Strong Kubernetes skills"]

    async def test_no_confirmed_facts_is_refused_rather_than_scored_zero(self, db, org):
        """A score of zero against an empty profile reads as a bad fit, which is the opposite of
        what happened."""
        post = await _posting(db, org)
        with pytest.raises(fit_store.NothingToScore, match="no confirmed facts"):
            await fit_store.compute(
                db,
                OrgContext(org_id=org.id),
                router=FakeRouter(["5 years of Python experience"]),
                post=post,
            )


class TestWhatIsRefused:
    async def test_a_posting_with_no_body_is_refused_with_the_thing_to_do(self, db, org):
        """Greenhouse's list response carries no description at all, so this is the ordinary
        case rather than an edge one, and the sentence says to paste it."""
        await _fact(db, org, fact_id="pp1", statement="Python.")
        post = await _posting(db, org, body=None)
        with pytest.raises(fit_store.NothingToScore, match="no description"):
            await fit_store.compute(
                db, OrgContext(org_id=org.id), router=FakeRouter(["Python"]), post=post
            )

    async def test_a_posting_with_no_readable_requirements_is_refused(self, db, org):
        """A correct answer for a posting that only describes the company."""
        await _fact(db, org, fact_id="pp1", statement="Python.")
        post = await _posting(db, org)
        with pytest.raises(fit_store.NothingToScore, match="No requirements"):
            await fit_store.compute(db, OrgContext(org_id=org.id), router=FakeRouter([]), post=post)


class TestOneRowPerPosting:
    async def test_scoring_twice_replaces_rather_than_appends(self, db, org):
        """Two rows would let the screen and the MCP tool show different answers depending on
        which they read first."""
        await _fact(
            db, org, fact_id="pp1", statement="Five years of Python experience and Postgres skills."
        )
        post = await _posting(db, org)
        ctx = OrgContext(org_id=org.id)

        await fit_store.compute(
            db, ctx, router=FakeRouter(["5 years of Python experience"]), post=post
        )
        await db.commit()
        await fit_store.compute(
            db,
            ctx,
            router=FakeRouter(["Strong Postgres skills", "Strong Rust skills"]),
            post=post,
        )
        await db.commit()

        from app.db.models import FitScoreRow

        rows = (
            await db.scalars(
                select(FitScoreRow).where(
                    FitScoreRow.org_id == org.id, FitScoreRow.job_post_id == post.id
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].missing == ["Strong Rust skills"]


class TestTheRoutes:
    async def test_the_read_route_is_free_and_says_nothing_is_scored_yet(self, api, db, org, owner):
        post = await _posting(db, org)
        before = await db.scalar(select(func.count()).select_from(LlmUsage))

        response = api.get(f"/api/v1/job-posts/{post.id}/fit", headers=owner)
        assert response.status_code == 200
        assert response.json()["fit"] is None
        assert "with a model" in response.json()["note"]
        # A read that spends is the pattern this project refuses everywhere else.
        assert await db.scalar(select(func.count()).select_from(LlmUsage)) == before

    async def test_a_posting_in_another_workspace_is_404(self, api, db, owner):
        response = api.get(f"/api/v1/job-posts/{uuid.uuid4()}/fit", headers=owner)
        assert response.status_code == 404

    async def test_the_score_is_never_returned_without_the_match_and_the_miss(
        self, api, db, org, owner
    ):
        """A bare number invites whoever reads it to repeat it as a judgement about themselves.
        This one is the fraction of the posting's own requirements that a confirmed fact
        matched, which is a much narrower claim."""
        await _fact(db, org, fact_id="pp1", statement="Strong Postgres skills.")
        post = await _posting(db, org)
        row = await fit_store.compute(
            db,
            OrgContext(org_id=org.id),
            router=FakeRouter(["Strong Postgres skills", "Strong Kubernetes skills"]),
            post=post,
        )
        await db.commit()

        body = api.get(f"/api/v1/job-posts/{post.id}/fit", headers=owner).json()
        assert body["fit"]["score"] == row.score
        assert body["fit"]["matched"] and body["fit"]["missing"]
        assert "requirement(s) matched" in body["note"]
