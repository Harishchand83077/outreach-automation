"""Job-post intake (F5, slice S12.2)."""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import JobPost, Org
from app.db.repo import OrgContext
from app.services.jobposts import feeds, intake

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


def postings(source: str):
    return feeds.parse_feed(source, (FIXTURES / f"{source}.json").read_text(encoding="utf-8"))


class TestStoringAFeed:
    async def test_postings_become_rows(self, db, org):
        result = await intake.store(db, OrgContext(org_id=org.id), postings("lever"))
        await db.commit()

        assert result.created > 0
        rows = list((await db.scalars(select(JobPost).where(JobPost.org_id == org.id))).all())
        assert len(rows) == result.created
        assert all(row.apply_url.startswith("http") for row in rows)

    async def test_re_polling_updates_rather_than_duplicating(self, db, org):
        ctx = OrgContext(org_id=org.id)
        first = await intake.store(db, ctx, postings("ashby"))
        await db.commit()

        second = await intake.store(db, ctx, postings("ashby"))
        await db.commit()

        assert second.created == 0
        assert second.updated == first.created

    async def test_two_workspaces_may_hold_the_same_posting(self, db, org):
        """The unique key is per org, so two candidates can watch the same board."""
        other = Org(name="Another candidate")
        db.add(other)
        await db.commit()

        await intake.store(db, OrgContext(org_id=org.id), postings("lever"))
        await intake.store(db, OrgContext(org_id=other.id), postings("lever"))
        await db.commit()

        mine = list((await db.scalars(select(JobPost).where(JobPost.org_id == org.id))).all())
        theirs = list((await db.scalars(select(JobPost).where(JobPost.org_id == other.id))).all())
        assert mine and theirs
        assert len(mine) == len(theirs)

    async def test_a_feed_that_stops_carrying_text_does_not_blank_what_we_have(self, db, org):
        """Overwriting stored text with nothing would lose the only copy we have."""
        ctx = OrgContext(org_id=org.id)
        with_text = postings("lever")[:1]
        await intake.store(db, ctx, with_text)
        await db.commit()

        stripped = [
            feeds.Posting(
                source=p.source,
                external_id=p.external_id,
                title=p.title,
                apply_url=p.apply_url,
                body_text=None,
            )
            for p in with_text
        ]
        await intake.store(db, ctx, stripped)
        await db.commit()

        row = await db.scalar(select(JobPost).where(JobPost.org_id == org.id))
        assert row is not None and row.body_text


class TestPastingOne:
    async def test_a_pasted_url_is_stored(self, db, org):
        row, created = await intake.store_pasted(
            db, OrgContext(org_id=org.id), url="https://naukri.test/jobs/123", title="Backend"
        )
        await db.commit()

        assert created is True
        assert row.source == "pasted"
        assert row.company_domain == "naukri.test"

    async def test_the_same_url_twice_is_one_row(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await intake.store_pasted(db, ctx, url="https://x.test/jobs/1")
        await db.commit()
        _row, created = await intake.store_pasted(db, ctx, url="https://x.test/jobs/1/")
        await db.commit()

        assert created is False

    async def test_a_tracking_parameter_does_not_make_a_second_posting(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await intake.store_pasted(db, ctx, url="https://x.test/jobs/2")
        await db.commit()
        _row, created = await intake.store_pasted(db, ctx, url="https://x.test/jobs/2?utm=li")
        await db.commit()

        assert created is False

    async def test_a_non_http_url_is_refused(self, db, org):
        with pytest.raises(ValueError):
            await intake.store_pasted(db, OrgContext(org_id=org.id), url="javascript:alert(1)")


class TestTheRoutes:
    async def test_pasting_through_the_api(self, api, owner):
        response = api.post(
            "/api/v1/job-posts",
            headers=owner,
            json={"url": "https://instahyre.test/j/9", "title": "Platform engineer"},
        )

        assert response.status_code == 201
        assert response.json()["job_post"]["source"] == "pasted"

    async def test_an_unauthenticated_caller_is_refused(self, api):
        assert api.get("/api/v1/job-posts").status_code == 401

    async def test_listing_shows_only_this_workspace(self, api, owner, db, org):
        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        await intake.store_pasted(
            db, OrgContext(org_id=other.id), url="https://theirs.test/j/1", title="Not mine"
        )
        await db.commit()
        api.post("/api/v1/job-posts", headers=owner, json={"url": "https://mine.test/j/1"})

        titles = [
            p["title"] for p in api.get("/api/v1/job-posts", headers=owner).json()["job_posts"]
        ]

        assert "Not mine" not in titles

    async def test_archiving_keeps_the_row(self, api, owner, db, org):
        made = api.post(
            "/api/v1/job-posts", headers=owner, json={"url": "https://mine.test/j/2"}
        ).json()["job_post"]

        assert api.delete(f"/api/v1/job-posts/{made['id']}", headers=owner).status_code == 200

        row = await db.get(JobPost, uuid.UUID(made["id"]))
        assert row is not None
        assert row.status == "archived"

    async def test_another_workspaces_posting_cannot_be_archived(self, api, owner, db):
        other = Org(name="Theirs again")
        db.add(other)
        await db.commit()
        row, _ = await intake.store_pasted(
            db, OrgContext(org_id=other.id), url="https://theirs.test/j/2"
        )
        await db.commit()

        assert api.delete(f"/api/v1/job-posts/{row.id}", headers=owner).status_code == 404

    async def test_the_route_never_fetches_the_url(self):
        """Nothing scrapes a job board: the user supplies the text (hard rule 4 and their terms)."""
        source = Path("app/api/routers/jobposts.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

        assert "httpx" not in code
        assert "fetch" not in code
