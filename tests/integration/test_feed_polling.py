"""Daily polling of watched ATS boards (F5, slice S12.3)."""

# ruff: noqa: F811

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from app.db.models import Job, JobPost, Org, WatchedBoard
from app.db.queue import JobKind
from app.db.repo import OrgContext
from app.services.jobposts import polling
from app.services.jobposts.client import fetch_board
from app.services.jobposts.feeds import FeedError, feed_url

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


async def watch(db, org, source="lever", slug="leverdemo") -> WatchedBoard:
    row = WatchedBoard(org_id=org.id, source=source, slug=slug)
    db.add(row)
    await db.commit()
    return row


class TestEnqueueing:
    async def test_a_new_board_is_polled(self, db, org):
        board = await watch(db, org)

        report = await polling.enqueue_due_polls(db, now=NOW)
        await db.commit()

        assert report.enqueued == 1
        job = await db.scalar(select(Job).where(Job.kind == JobKind.POLL_ATS_FEED))
        assert job is not None
        assert job.payload["slug"] == board.slug

    async def test_a_board_polled_today_is_left_alone(self, db, org):
        board = await watch(db, org)
        board.last_polled_at = NOW - timedelta(hours=2)
        await db.commit()

        report = await polling.enqueue_due_polls(db, now=NOW)
        await db.commit()

        assert report.enqueued == 0

    async def test_a_board_polled_yesterday_is_due(self, db, org):
        board = await watch(db, org)
        board.last_polled_at = NOW - timedelta(hours=25)
        await db.commit()

        assert (await polling.enqueue_due_polls(db, now=NOW)).enqueued == 1

    async def test_two_workspaces_watching_one_board_both_get_a_poll(self, db, org):
        """**The case ADR-032 had to land before.**

        The dedupe key is `feed:{source}:{slug}`, which two workspaces share. Before dedupe was
        scoped by org, the second workspace's poll was silently refused and its postings simply
        never arrived.
        """
        other = Org(name="Another candidate")
        db.add(other)
        await db.commit()
        await watch(db, org)
        db.add(WatchedBoard(org_id=other.id, source="lever", slug="leverdemo"))
        await db.commit()

        report = await polling.enqueue_due_polls(db, now=NOW)
        await db.commit()

        assert report.enqueued == 2
        jobs = list((await db.scalars(select(Job).where(Job.kind == JobKind.POLL_ATS_FEED))).all())
        assert {job.org_id for job in jobs} == {org.id, other.id}

    async def test_a_poll_already_queued_is_not_queued_twice(self, db, org):
        await watch(db, org)
        await polling.enqueue_due_polls(db, now=NOW)
        await db.commit()
        # Force it due again; the live job should still block a duplicate.
        board = await db.scalar(select(WatchedBoard))
        board.last_polled_at = None
        await db.commit()

        report = await polling.enqueue_due_polls(db, now=NOW)
        await db.commit()

        assert report.skipped == 1


class TestFetching:
    @respx.mock
    async def test_a_board_is_read_and_stored(self, db, org):
        url = feed_url("lever", "leverdemo")
        respx.get(url).mock(
            return_value=httpx.Response(200, content=(FIXTURES / "lever.json").read_bytes())
        )

        postings = await fetch_board("lever", "leverdemo")
        await __import__("app.services.jobposts.intake", fromlist=["store"]).store(
            db, OrgContext(org_id=org.id), postings
        )
        await db.commit()

        rows = list((await db.scalars(select(JobPost).where(JobPost.org_id == org.id))).all())
        assert rows

    @respx.mock
    async def test_a_404_says_it_is_probably_the_slug(self):
        """`api.lever.co` answers 404 for a company that is simply not on Lever.

        Verified against the live service: netflix returns 404 there. An error that said "the
        endpoint is gone" would send somebody looking in the wrong place.
        """
        respx.get(feed_url("lever", "netflix")).mock(return_value=httpx.Response(404))

        with pytest.raises(FeedError) as caught:
            await fetch_board("lever", "netflix")

        assert "slug" in str(caught.value)

    @respx.mock
    async def test_a_server_error_is_reported_with_its_status(self):
        respx.get(feed_url("ashby", "ramp")).mock(return_value=httpx.Response(503))

        with pytest.raises(FeedError) as caught:
            await fetch_board("ashby", "ramp")

        assert "503" in str(caught.value)

    @respx.mock
    async def test_an_oversized_response_is_refused(self):
        from app.services.jobposts.feeds import MAX_FEED_BYTES

        respx.get(feed_url("ashby", "huge")).mock(
            return_value=httpx.Response(200, content=b"x" * (MAX_FEED_BYTES + 1))
        )

        with pytest.raises(FeedError):
            await fetch_board("ashby", "huge")


class TestTheRoutes:
    async def test_watching_a_board(self, api, owner):
        response = api.post(
            "/api/v1/watched-boards",
            headers=owner,
            json={"source": "greenhouse", "slug": "vercel", "label": "Vercel"},
        )

        assert response.status_code == 201
        assert response.json()["created"] is True

    async def test_watching_the_same_board_twice_is_idempotent(self, api, owner):
        api.post("/api/v1/watched-boards", headers=owner, json={"source": "ashby", "slug": "ramp"})
        again = api.post(
            "/api/v1/watched-boards", headers=owner, json={"source": "ashby", "slug": "ramp"}
        )

        assert again.json()["created"] is False

    async def test_an_unknown_source_is_refused_at_the_door(self, api, owner):
        """Validated with the same `feed_url` the poll uses, so a board that saves can be polled."""
        response = api.post(
            "/api/v1/watched-boards", headers=owner, json={"source": "workday", "slug": "x"}
        )

        assert response.status_code == 422

    async def test_a_member_cannot_add_a_board(self, api, member):
        response = api.post(
            "/api/v1/watched-boards", headers=member, json={"source": "lever", "slug": "x"}
        )

        assert response.status_code == 403

    async def test_listing_is_scoped_to_the_workspace(self, api, owner, db):
        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        db.add(WatchedBoard(org_id=other.id, source="lever", slug="theirs"))
        await db.commit()
        api.post("/api/v1/watched-boards", headers=owner, json={"source": "lever", "slug": "mine"})

        slugs = [
            b["slug"]
            for b in api.get("/api/v1/watched-boards", headers=owner).json()["watched_boards"]
        ]

        assert "theirs" not in slugs
