"""A candidate's facts and answer bank (F6, slice S12.4)."""

# ruff: noqa: F811

from __future__ import annotations

import pytest

from app.db.models import Org
from app.db.repo import OrgContext
from app.services.candidate import profile

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestFacts:
    async def test_a_typed_fact_is_confirmed_immediately(self, db, org):
        row = await profile.add_fact(
            db, OrgContext(org_id=org.id), kind="skill", statement="Three years of Postgres"
        )
        await db.commit()

        assert row.fact_id == "pp1"
        assert row.confirmed_at is not None

    async def test_a_mined_fact_arrives_as_a_proposal(self, db, org):
        """**The rule the whole thing rests on.**

        The person who gets asked about a claim in an interview is the person who has to have
        agreed to it, so a fact read out of GitHub is a suggestion until they say so.
        """
        row = await profile.add_fact(
            db,
            OrgContext(org_id=org.id),
            kind="project",
            statement="Maintains an open-source parser",
            source="github",
        )
        await db.commit()

        assert row.confirmed_at is None

    async def test_only_confirmed_facts_are_offered_for_drafting(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await profile.add_fact(db, ctx, kind="skill", statement="Typed and true")
        await profile.add_fact(
            db, ctx, kind="skill", statement="Guessed from a repo", source="github"
        )
        await db.commit()

        offered = await profile.confirmed_facts(db, ctx)

        assert [fact.text for fact in offered] == ["Typed and true"]

    async def test_confirming_makes_it_usable(self, db, org):
        ctx = OrgContext(org_id=org.id)
        row = await profile.add_fact(
            db, ctx, kind="skill", statement="From a resume", source="resume"
        )
        await db.commit()

        assert await profile.confirm_fact(db, ctx, fact_row_id=row.id) is True
        await db.commit()

        assert len(await profile.confirmed_facts(db, ctx)) == 1

    async def test_ids_do_not_repeat_after_a_delete(self, db, org):
        """A duplicate ppN would make a citation ambiguous, the one thing it cannot be."""
        ctx = OrgContext(org_id=org.id)
        first = await profile.add_fact(db, ctx, kind="skill", statement="One")
        await profile.add_fact(db, ctx, kind="skill", statement="Two")
        await db.commit()
        await profile.delete_fact(db, ctx, fact_row_id=first.id)
        await db.commit()

        third = await profile.add_fact(db, ctx, kind="skill", statement="Three")
        await db.commit()

        assert third.fact_id == "pp3"

    async def test_facts_are_per_workspace(self, db, org):
        other = Org(name="Another candidate")
        db.add(other)
        await db.commit()
        await profile.add_fact(db, OrgContext(org_id=org.id), kind="skill", statement="Mine")
        await profile.add_fact(db, OrgContext(org_id=other.id), kind="skill", statement="Theirs")
        await db.commit()

        mine = await profile.list_facts(db, OrgContext(org_id=org.id))

        assert [row.statement for row in mine] == ["Mine"]
        # Each workspace numbers its own facts from pp1; the id is unique within one, not across.
        assert mine[0].fact_id == "pp1"

    async def test_a_bad_kind_is_refused(self, db, org):
        with pytest.raises(ValueError):
            await profile.add_fact(
                db, OrgContext(org_id=org.id), kind="vibes", statement="Good at things"
            )

    async def test_an_empty_statement_is_refused(self, db, org):
        with pytest.raises(ValueError):
            await profile.add_fact(db, OrgContext(org_id=org.id), kind="skill", statement="  ")


class TestTheAnswerBank:
    async def test_an_answer_is_unapproved_by_default(self, db, org):
        row = await profile.add_answer(
            db, OrgContext(org_id=org.id), question_pattern="notice period", answer="30 days"
        )
        await db.commit()

        assert row.approved_at is None

    async def test_only_approved_answers_are_matched(self, db, org):
        """Anything unapproved is as good as absent: nothing is offered by accident."""
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(db, ctx, question_pattern="notice period", answer="30 days")
        await db.commit()

        assert await profile.match_question(db, ctx, label="What is your notice period?") is None

    async def test_an_approved_answer_matches_a_form_label(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(
            db, ctx, question_pattern="notice period", answer="30 days", approve=True
        )
        await db.commit()

        found = await profile.match_question(db, ctx, label="What is your notice period?")

        assert found is not None
        assert found.answer == "30 days"

    async def test_every_pattern_word_must_appear(self, db, org):
        """A pattern of two words must not match a label carrying only one of them."""
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(
            db, ctx, question_pattern="notice period", answer="30 days", approve=True
        )
        await db.commit()

        assert await profile.match_question(db, ctx, label="Pay period") is None

    async def test_the_most_specific_pattern_wins(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(db, ctx, question_pattern="salary", answer="Open", approve=True)
        await profile.add_answer(
            db, ctx, question_pattern="expected salary inr", answer="18 LPA", approve=True
        )
        await db.commit()

        found = await profile.match_question(db, ctx, label="Expected salary in INR?")

        assert found is not None
        assert found.answer == "18 LPA"

    async def test_an_unknown_question_returns_nothing_rather_than_a_guess(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(db, ctx, question_pattern="notice", answer="30", approve=True)
        await db.commit()

        assert await profile.match_question(db, ctx, label="Why do you want this job?") is None

    async def test_editing_an_answer_unapproves_it(self, db, org):
        """The approval was of the previous wording, so it does not carry over."""
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(
            db, ctx, question_pattern="notice period", answer="30 days", approve=True
        )
        await db.commit()

        edited = await profile.add_answer(
            db, ctx, question_pattern="notice period", answer="60 days"
        )
        await db.commit()

        assert edited.approved_at is None


class TestTheRoutes:
    async def test_adding_and_listing_a_fact(self, api, owner):
        made = api.post(
            "/api/v1/profile/facts",
            headers=owner,
            json={"kind": "skill", "statement": "Three years of Postgres"},
        )

        assert made.status_code == 201
        assert made.json()["fact"]["confirmed"] is True
        listed = api.get("/api/v1/profile/facts", headers=owner).json()
        assert listed["facts"]
        assert "confirmed fact" in listed["note"]

    async def test_a_bad_kind_is_a_422_not_a_500(self, api, owner):
        response = api.post(
            "/api/v1/profile/facts", headers=owner, json={"kind": "vibes", "statement": "x"}
        )

        assert response.status_code == 422

    async def test_an_anonymous_caller_is_refused(self, api):
        assert api.get("/api/v1/profile/facts").status_code == 401

    async def test_another_workspaces_fact_cannot_be_confirmed(self, api, owner, db):
        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        row = await profile.add_fact(
            db, OrgContext(org_id=other.id), kind="skill", statement="Theirs", source="github"
        )
        await db.commit()

        response = api.post(f"/api/v1/profile/facts/{row.id}/confirm", headers=owner)

        assert response.status_code == 404

    async def test_approving_an_answer_through_the_api(self, api, owner):
        made = api.post(
            "/api/v1/profile/answers",
            headers=owner,
            json={"question_pattern": "notice period", "answer": "30 days"},
        ).json()["answer"]

        assert made["approved"] is False
        assert (
            api.post(f"/api/v1/profile/answers/{made['id']}/approve", headers=owner).status_code
            == 200
        )
        after = api.get("/api/v1/profile/answers", headers=owner).json()["answers"]
        assert after[0]["approved"] is True

    async def test_no_model_is_called_in_a_request(self):
        """Extraction and scoring are queued jobs; a request does database work only."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path("app/api/routers/candidate.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any("llm" in name for name in imported)
        assert "app.services.candidate.requirements" not in imported
