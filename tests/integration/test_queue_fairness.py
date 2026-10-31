"""One workspace's backlog must not starve another (F21, slice S11.2).

The acceptance criterion is "a workspace over its queue cap cannot starve another". The important
word is **starve**: the risk is not that a workspace enqueues a lot of work, it is that its work
crowds everybody else's out of every claim for as long as it lasts.

`claim` ordered by `(priority, run_after)` across the whole table. So a workspace that imported
two thousand leads held the two thousand oldest `queued` rows, and every claim for the next while
was theirs. A second workspace's single urgent job sat behind all of it — correctly by the
ordering, and uselessly by any other measure.

**The fix is round-robin across workspaces**, which is the same answer PR #67 reached for two
campaigns sharing one mailbox: rank each workspace's jobs among its own, then take the first from
each workspace before the second from any. Ordering *within* a workspace is untouched, so
priority and age still decide which of its jobs goes next.

A hard cap on enqueueing is deliberately **not** how this is solved — see `test_the_absolute
_ceiling_is_far_above_any_real_import` below and the ADR. An import of a thousand leads legitimately
creates a thousand jobs, spread over the days they could be sent; refusing that would break the
product to solve a problem the ordering already solves.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db import queue
from app.db.models import Org
from app.db.queue import JobKind

pytestmark = pytest.mark.asyncio(loop_scope="session")

KIND = JobKind.RESEARCH_LEAD


async def other_org(db, name: str = "Another workspace") -> Org:
    org = Org(name=name)
    db.add(org)
    await db.commit()
    return org


async def backlog(db, org, count: int, *, oldest: datetime) -> None:
    """`count` queued jobs for one workspace, all due, oldest first."""
    for index in range(count):
        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=KIND,
            payload={"lead_id": str(uuid.uuid4())},
            run_after=oldest + timedelta(seconds=index),
        )
        assert job is not None
    await db.commit()


class TestABacklogCannotStarveAnotherWorkspace:
    async def test_a_claim_reaches_both_workspaces(self, db, org):
        """**The criterion, as directly as it can be stated.**

        One workspace holds a backlog of jobs all older than the other's single job. A claim of
        two must not come back with two from the same workspace.
        """
        now = datetime.now(tz=UTC)
        await backlog(db, org, 5, oldest=now - timedelta(hours=1))
        quiet = await other_org(db)
        await backlog(db, quiet, 1, oldest=now - timedelta(minutes=1))

        claimed = await queue.claim(db, worker_id="w1", kinds=[KIND], limit=2)
        await db.commit()

        orgs = {job.org_id for job in claimed}
        assert len(claimed) == 2
        assert orgs == {org.id, quiet.id}, (
            "both workspaces should be represented: the backlog is starving the quiet one"
        )

    async def test_the_busy_workspace_still_gets_its_share(self, db, org):
        """Fairness is not a penalty. With nobody else waiting, one workspace takes every slot."""
        now = datetime.now(tz=UTC)
        await backlog(db, org, 4, oldest=now - timedelta(hours=1))

        claimed = await queue.claim(db, worker_id="w1", kinds=[KIND], limit=3)
        await db.commit()

        assert len(claimed) == 3
        assert {job.org_id for job in claimed} == {org.id}

    async def test_order_within_a_workspace_is_still_oldest_first(self, db, org):
        """Round-robin changes which workspace goes next, never which of its jobs does."""
        now = datetime.now(tz=UTC)
        await backlog(db, org, 3, oldest=now - timedelta(hours=1))

        first = await queue.claim(db, worker_id="w1", kinds=[KIND], limit=1)
        await db.commit()
        second = await queue.claim(db, worker_id="w2", kinds=[KIND], limit=1)
        await db.commit()

        assert first[0].run_after < second[0].run_after

    async def test_priority_still_wins_inside_a_workspace(self, db, org):
        """A priority is a statement about one workspace's own work, and it is still honoured."""
        now = datetime.now(tz=UTC)
        await backlog(db, org, 2, oldest=now - timedelta(hours=1))
        urgent = await queue.enqueue(
            db,
            org_id=org.id,
            kind=KIND,
            payload={"lead_id": str(uuid.uuid4())},
            priority=1,
            run_after=now,
        )
        assert urgent is not None
        await db.commit()

        claimed = await queue.claim(db, worker_id="w1", kinds=[KIND], limit=1)
        await db.commit()

        assert claimed[0].id == urgent.id


class TestTheAbsoluteCeiling:
    """A runaway loop still has to hit something, but far above any legitimate use."""

    async def test_the_ceiling_is_far_above_any_real_import(self):
        """A thousand-lead import is ordinary. The ceiling must not be near it.

        Asserted as a property of the setting rather than by enqueueing fifty thousand rows,
        which would be a slow test proving arithmetic.
        """
        from app.core.settings import config

        assert config.MAX_QUEUED_JOBS_PER_ORG >= 10_000

    async def test_enqueue_refuses_above_the_ceiling_and_says_which_workspace(
        self, db, org, monkeypatch
    ):
        """**It raises rather than returning None, and that distinction is the whole point.**

        `enqueue` returns `None` for a duplicate, and every caller in the tree reads `None` as
        "already queued, nothing to do". Reporting a full queue the same way would silently drop
        work -- which is precisely the cross-workspace bug ADR-032 had just fixed. A refusal
        nobody can detect is worse than a loud failure.
        """
        monkeypatch.setattr(queue.config, "MAX_QUEUED_JOBS_PER_ORG", 2)
        await backlog(db, org, 2, oldest=datetime.now(tz=UTC) - timedelta(hours=1))

        with pytest.raises(queue.QueueFull) as caught:
            await queue.enqueue(db, org_id=org.id, kind=KIND, payload={"lead_id": "x"})

        assert str(org.id) in str(caught.value)

    async def test_the_ceiling_is_per_workspace(self, db, org, monkeypatch):
        """One workspace at its ceiling must not block another's work -- the same rule again."""
        monkeypatch.setattr(queue.config, "MAX_QUEUED_JOBS_PER_ORG", 2)
        await backlog(db, org, 2, oldest=datetime.now(tz=UTC) - timedelta(hours=1))
        quiet = await other_org(db, "Unaffected workspace")

        job = await queue.enqueue(
            db, org_id=quiet.id, kind=KIND, payload={"lead_id": str(uuid.uuid4())}
        )
        await db.commit()

        assert job is not None

    async def test_finished_jobs_do_not_count_against_it(self, db, org, monkeypatch):
        """The ceiling bounds work *waiting*, not work ever done.

        Counting finished rows would mean a workspace that had run a lot of jobs eventually
        could not enqueue any, which is a slow fuse rather than a safety valve.
        """
        monkeypatch.setattr(queue.config, "MAX_QUEUED_JOBS_PER_ORG", 2)
        await backlog(db, org, 2, oldest=datetime.now(tz=UTC) - timedelta(hours=1))
        claimed = await queue.claim(db, worker_id="w1", kinds=[KIND], limit=2)
        for job in claimed:
            await queue.succeed(db, job.id, worker_id="w1")
        await db.commit()

        job = await queue.enqueue(
            db, org_id=org.id, kind=KIND, payload={"lead_id": str(uuid.uuid4())}
        )
        await db.commit()

        assert job is not None
