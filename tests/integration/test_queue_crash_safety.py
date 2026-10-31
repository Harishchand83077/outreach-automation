"""What a crashed or stalled worker leaves behind (F21, slice S11.3).

The acceptance criterion from `docs/OUTREACH_PLATFORM_V3.md` is "a worker killed mid-job leaves
nothing sent twice or lost". Those are two properties and they fail in opposite directions:

- **Nothing lost** is the lease. A claimed job carries `locked_until`; if the process dies the
  lease simply runs out and `reap_expired` makes the job claimable again. That machinery already
  existed and the `Scheduler` already calls it — what was missing was a test that a job really
  does come back and get finished.
- **Nothing done twice** is harder, and writing this file found a gap in it. `queue.succeed` had
  no ownership check, and the runner's heartbeat loop deliberately tolerates a failed heartbeat
  ("the queue working as designed"). So a worker whose lease lapsed — a slow job, a long pause,
  a network blip — could finish and mark a job succeeded that a *second* worker had already
  reclaimed and was running. The handler would run twice and the queue would look perfectly
  healthy.

**Nothing sent twice specifically is the outbox's job, not the queue's**, and it is covered where
it lives: `tests/integration/test_outbox.py` drives two concurrent claims against the unique
`idempotency_key`, and `test_send_recovery.py` covers ADR-002's Sent-folder resolution. This file
does not duplicate that; it covers the layer above, where a *handler* could be run twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.settings import config
from app.db import queue
from app.db.models import Job
from app.db.queue import JobKind
from app.worker.context import WorkerContext
from app.worker.runner import JobRunner

pytestmark = pytest.mark.asyncio(loop_scope="session")

KIND = JobKind.RESEARCH_LEAD


class FakePoller:
    async def find_by_message_id(self, message_id, *, folders=()):
        return None


@pytest_asyncio.fixture(loop_scope="session")
async def ctx(session_factory):
    return WorkerContext(
        session_factory=session_factory,
        settings=config,
        poller=FakePoller(),  # type: ignore[arg-type]
        sender=object(),  # type: ignore[arg-type]
    )


def runner_with(ctx, handler, *, worker_id: str, monkeypatch) -> JobRunner:
    runner = JobRunner(ctx, worker_id=worker_id, kinds=[KIND])
    monkeypatch.setitem(
        __import__("app.worker.runner", fromlist=["HANDLERS"]).HANDLERS, KIND, handler
    )
    return runner


async def add_job(session_factory, org) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        job = await queue.enqueue(
            session, org_id=org.id, kind=KIND, payload={"lead_id": str(uuid.uuid4())}
        )
        assert job is not None
        return job.id


async def job_row(session_factory, job_id) -> Job:
    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        return row


async def expire_the_lease(session_factory, job_id) -> None:
    """What a dead process looks like to the database: a lease nobody is extending.

    Backdated rather than waited out, because the lease is minutes long and a test that sleeps
    for it is a test nobody runs.
    """
    async with session_factory() as session, session.begin():
        job = await session.get(Job, job_id)
        assert job is not None
        job.locked_until = datetime.now(tz=UTC) - timedelta(seconds=1)


class TestNothingIsLost:
    async def test_a_job_whose_worker_vanished_is_reclaimed_and_finished(
        self, session_factory, ctx, org, monkeypatch
    ):
        """The whole point of the lease, asserted end to end for the first time."""
        ran: list[str] = []

        async def handler(ctx_, session, job):
            ran.append("worker-b")

        job_id = await add_job(session_factory, org)

        # Worker A claims it and dies: no outcome recorded, no heartbeat, nothing.
        async with session_factory() as session, session.begin():
            claimed = await queue.claim(session, worker_id="worker-a", kinds=[KIND])
        assert [job.id for job in claimed] == [job_id]
        await expire_the_lease(session_factory, job_id)

        async with session_factory() as session, session.begin():
            reaped = await queue.reap_expired(session)
        assert reaped == [(job_id, "queued")]

        # The backoff is real, so the job is not claimable this instant. Undo it rather than
        # wait: what is under test is the reclaim, not the delay.
        async with session_factory() as session, session.begin():
            job = await session.get(Job, job_id)
            assert job is not None
            job.run_after = datetime.now(tz=UTC) - timedelta(seconds=1)

        report = await runner_with(
            ctx, handler, worker_id="worker-b", monkeypatch=monkeypatch
        ).run_batch()

        assert report.succeeded == 1
        assert ran == ["worker-b"], "the handler should have run exactly once, on the new owner"
        row = await job_row(session_factory, job_id)
        assert row.status == "succeeded"
        assert row.attempts == 2, "the dead attempt is counted, which is what bounds the retries"

    async def test_an_exhausted_job_is_declared_dead_rather_than_retried_forever(
        self, session_factory, ctx, org
    ):
        """A crash loop has to end somewhere visible, which is the dead-letter state."""
        async with session_factory() as session, session.begin():
            job = await queue.enqueue(session, org_id=org.id, kind=KIND, max_attempts=1)
            assert job is not None
            job_id = job.id

        async with session_factory() as session, session.begin():
            await queue.claim(session, worker_id="worker-a", kinds=[KIND])
        await expire_the_lease(session_factory, job_id)

        async with session_factory() as session, session.begin():
            reaped = await queue.reap_expired(session)

        assert reaped == [(job_id, "dead")]
        assert (await job_row(session_factory, job_id)).status == "dead"


class TestNothingIsDoneTwice:
    """The gap this file found.

    A worker that lost its lease must not be able to record an outcome for a job somebody else
    now owns, and the work it did before losing it must not survive.
    """

    async def test_a_worker_that_lost_its_lease_cannot_mark_the_job_succeeded(
        self, session_factory, org
    ):
        job_id = await add_job(session_factory, org)
        async with session_factory() as session, session.begin():
            await queue.claim(session, worker_id="worker-a", kinds=[KIND])
        await expire_the_lease(session_factory, job_id)

        # Reaped and taken by somebody else.
        async with session_factory() as session, session.begin():
            await queue.reap_expired(session)
            job = await session.get(Job, job_id)
            assert job is not None
            job.run_after = datetime.now(tz=UTC) - timedelta(seconds=1)
        async with session_factory() as session, session.begin():
            retaken = await queue.claim(session, worker_id="worker-b", kinds=[KIND])
        assert [job.id for job in retaken] == [job_id]

        # Worker A, oblivious, finishes its work and tries to record success.
        async with session_factory() as session, session.begin():
            owned = await queue.succeed(session, job_id, worker_id="worker-a")

        assert owned is False, "a worker that lost its lease recorded an outcome anyway"
        row = await job_row(session_factory, job_id)
        assert row.status == "running", "worker B still owns this job"
        assert row.locked_by == "worker-b"

    async def test_the_new_owner_can_still_record_its_outcome(self, session_factory, org):
        """The check must not break the ordinary path, which is the only one that matters most."""
        job_id = await add_job(session_factory, org)
        async with session_factory() as session, session.begin():
            await queue.claim(session, worker_id="worker-b", kinds=[KIND])

        async with session_factory() as session, session.begin():
            owned = await queue.succeed(session, job_id, worker_id="worker-b")

        assert owned is True
        assert (await job_row(session_factory, job_id)).status == "succeeded"

    async def test_work_from_a_lost_lease_is_rolled_back_with_it(
        self, session_factory, ctx, org, monkeypatch
    ):
        """**The property that makes a handler running twice harmless.**

        The handler shares the transaction that records the outcome. So if the outcome cannot be
        recorded — because the lease is gone — the handler's work goes with it, and only the
        worker that still owns the job leaves anything behind. Without this, a stalled worker
        would commit its side effects beside the new owner's.
        """
        marker = str(uuid.uuid4())

        async def handler(ctx_, session, job):
            await queue.enqueue(
                session, org_id=job.org_id, kind=JobKind.DRAFT_LEAD, payload={"marker": marker}
            )

        job_id = await add_job(session_factory, org)
        # Worker B owns it; worker A is about to pretend it still does.
        async with session_factory() as session, session.begin():
            await queue.claim(session, worker_id="worker-b", kinds=[KIND])

        runner = runner_with(ctx, handler, worker_id="worker-a", monkeypatch=monkeypatch)
        await runner._run_one(job_id, KIND, org.id, {})

        async with session_factory() as session:
            rows = (
                (await session.execute(select(Job).where(Job.kind == JobKind.DRAFT_LEAD)))
                .scalars()
                .all()
            )
        assert not [row for row in rows if (row.payload or {}).get("marker") == marker], (
            "a worker that no longer owns the job committed its work anyway"
        )
