"""The job queue against a real Postgres.

`FOR UPDATE SKIP LOCKED`, `ON CONFLICT DO NOTHING` against a partial unique index, and lease
expiry are database behaviour. A fake would only prove the fake works, so these run for real.

The prototype had no queue: parallelism was `asyncio.gather` in one process with no retries, no
dead-letter and no resumability, so a restart orphaned every in-flight lead (issues B9, E4).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import queue
from app.db.models import Job
from app.db.queue import JobKind

pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestEnqueue:
    async def test_a_job_is_queued_and_claimable(self, db, org, lead_factory):
        lead = await lead_factory()
        job = await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD, lead_id=lead.id)
        await db.commit()

        assert job is not None
        assert job.status == "queued"
        assert job.attempts == 0

    async def test_a_duplicate_dedupe_key_is_a_no_op(self, db, org, lead_factory):
        """The scheduler may notice the same due lead twice; that must not double the work."""
        lead = await lead_factory()
        key = f"lead:{lead.id}"

        first = await queue.enqueue(
            db, org_id=org.id, kind=JobKind.ENRICH_LEAD, lead_id=lead.id, dedupe_key=key
        )
        await db.commit()
        second = await queue.enqueue(
            db, org_id=org.id, kind=JobKind.ENRICH_LEAD, lead_id=lead.id, dedupe_key=key
        )
        await db.commit()

        assert first is not None
        assert second is None, "the second enqueue must be refused while the first is live"

    async def test_the_same_key_may_be_reused_once_the_first_job_finished(
        self, db, org, lead_factory
    ):
        """The index covers only live jobs, so a later retry of the same work is allowed."""
        lead = await lead_factory()
        key = f"lead:{lead.id}"

        first = await queue.enqueue(
            db, org_id=org.id, kind=JobKind.ENRICH_LEAD, lead_id=lead.id, dedupe_key=key
        )
        await db.commit()
        assert first is not None
        await queue.succeed(db, first.id)
        await db.commit()

        again = await queue.enqueue(
            db, org_id=org.id, kind=JobKind.ENRICH_LEAD, lead_id=lead.id, dedupe_key=key
        )
        await db.commit()
        assert again is not None

    async def test_different_kinds_do_not_collide_on_the_same_key(self, db, org, lead_factory):
        lead = await lead_factory()
        key = f"lead:{lead.id}"
        a = await queue.enqueue(
            db, org_id=org.id, kind=JobKind.ENRICH_LEAD, lead_id=lead.id, dedupe_key=key
        )
        b = await queue.enqueue(
            db, org_id=org.id, kind=JobKind.DRAFT_LEAD, lead_id=lead.id, dedupe_key=key
        )
        await db.commit()
        assert a is not None and b is not None


class TestClaim:
    async def test_claiming_leases_the_job_and_counts_the_attempt(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()

        claimed = await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD])
        await db.commit()

        assert len(claimed) == 1
        job = claimed[0]
        assert job.status == "running"
        assert job.locked_by == "w1"
        assert job.locked_until is not None
        assert job.attempts == 1

    async def test_a_job_not_yet_due_is_not_claimed(self, db, org):
        await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.ENRICH_LEAD,
            run_after=datetime.now(tz=UTC) + timedelta(hours=1),
        )
        await db.commit()

        assert await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]) == []

    async def test_only_the_requested_kinds_are_claimed(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await queue.enqueue(db, org_id=org.id, kind=JobKind.SEND_MESSAGE)
        await db.commit()

        claimed = await queue.claim(db, worker_id="w1", kinds=[JobKind.SEND_MESSAGE], limit=10)
        await db.commit()
        assert [j.kind for j in claimed] == [JobKind.SEND_MESSAGE]

    async def test_priority_then_age_decides_order(self, db, org):
        now = datetime.now(tz=UTC)
        await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.ENRICH_LEAD,
            priority=100,
            run_after=now - timedelta(minutes=5),
            payload={"label": "old-normal"},
        )
        await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.ENRICH_LEAD,
            priority=10,
            run_after=now,
            payload={"label": "urgent"},
        )
        await db.commit()

        claimed = await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD], limit=2)
        await db.commit()
        assert [j.payload["label"] for j in claimed] == ["urgent", "old-normal"]

    async def test_two_workers_never_claim_the_same_job(self, engine_factory, org):
        """This is what SKIP LOCKED buys: concurrent claims divide the work, never duplicate it.

        Two separate engines means two real connections, so the database — not asyncio
        scheduling — decides the outcome.
        """
        maker_a = async_sessionmaker(engine_factory(), expire_on_commit=False)
        maker_b = async_sessionmaker(engine_factory(), expire_on_commit=False)

        async with maker_a() as setup:
            for _ in range(10):
                await queue.enqueue(setup, org_id=org.id, kind=JobKind.ENRICH_LEAD)
            await setup.commit()

        async def worker(maker, name: str) -> list[str]:
            async with maker() as session:
                jobs = await queue.claim(
                    session, worker_id=name, kinds=[JobKind.ENRICH_LEAD], limit=5
                )
                await session.commit()
                return [str(j.id) for j in jobs]

        got_a, got_b = await asyncio.gather(worker(maker_a, "w1"), worker(maker_b, "w2"))

        assert set(got_a).isdisjoint(got_b), "a job was claimed twice"
        assert len(got_a) + len(got_b) == 10


class TestCompletion:
    async def test_success_is_recorded(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]

        await queue.succeed(db, job.id)
        await db.commit()
        await db.refresh(job)

        assert job.status == "succeeded"
        assert job.locked_by is None
        assert job.finished_at is not None

    async def test_a_transient_failure_is_retried_with_backoff(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]

        status = await queue.fail(db, job.id, error="connection reset")
        await db.commit()
        await db.refresh(job)

        assert status == "queued"
        assert job.status == "queued"
        assert job.last_error == "connection reset"
        assert job.run_after > datetime.now(tz=UTC), "a retry must wait, not spin"

    async def test_a_permanent_failure_goes_straight_to_dead(self, db, org):
        """Retrying an auth rejection or bad input just produces the same error more slowly."""
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]

        status = await queue.fail(db, job.id, error="401 unauthorized", permanent=True)
        await db.commit()
        await db.refresh(job)

        assert status == "dead"
        assert job.status == "dead"
        assert job.attempts == 1, "a permanent failure must not burn the retry budget"

    async def test_a_job_dies_once_attempts_are_exhausted(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD, max_attempts=2)
        await db.commit()

        for expected in ("queued", "dead"):
            job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]
            status = await queue.fail(db, job.id, error="still broken")
            await db.commit()
            assert status == expected
            if expected == "queued":
                # Undo the backoff so the next claim can happen immediately.
                job.run_after = datetime.now(tz=UTC) - timedelta(seconds=1)
                await db.commit()

    async def test_error_text_is_truncated(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]

        await queue.fail(db, job.id, error="x" * 5000)
        await db.commit()
        await db.refresh(job)
        assert len(job.last_error or "") == 2000


class TestLeaseAndReaper:
    async def test_heartbeat_extends_a_held_lease(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]
        await db.commit()
        first = job.locked_until

        assert await queue.heartbeat(db, job.id, worker_id="w1", lease_seconds=600) is True
        await db.commit()
        await db.refresh(job)
        assert job.locked_until > first

    async def test_heartbeat_fails_for_a_worker_that_lost_the_lease(self, db, org):
        """A worker whose lease was reaped must not keep working as if it still owns the job."""
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD]))[0]
        await db.commit()

        assert await queue.heartbeat(db, job.id, worker_id="someone-else") is False

    async def test_an_expired_lease_is_requeued(self, db, org):
        """A crashed worker is harmless: the lease runs out and the job becomes claimable."""
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        job = (await queue.claim(db, worker_id="crashed", kinds=[JobKind.ENRICH_LEAD]))[0]
        job.locked_until = datetime.now(tz=UTC) - timedelta(minutes=1)
        await db.commit()

        outcomes = await queue.reap_expired(db)
        await db.commit()
        await db.refresh(job)

        assert outcomes == [(job.id, "queued")]
        assert job.status == "queued"
        assert job.locked_by is None

    async def test_an_expired_lease_dies_when_attempts_are_spent(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD, max_attempts=1)
        await db.commit()
        job = (await queue.claim(db, worker_id="crashed", kinds=[JobKind.ENRICH_LEAD]))[0]
        job.locked_until = datetime.now(tz=UTC) - timedelta(minutes=1)
        await db.commit()

        outcomes = await queue.reap_expired(db)
        await db.commit()
        await db.refresh(job)

        assert outcomes == [(job.id, "dead")]
        assert job.status == "dead"

    async def test_a_live_lease_is_left_alone(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD])
        await db.commit()

        assert await queue.reap_expired(db) == []


class TestCancellation:
    async def test_pending_jobs_for_a_lead_are_cancelled(self, db, org, lead_factory):
        """A reply, bounce or opt-out must kill the pending follow-up in the same transaction
        that records the reason — otherwise we email someone who just asked us not to."""
        lead = await lead_factory()
        other = await lead_factory()
        await queue.enqueue(db, org_id=org.id, kind=JobKind.SEND_MESSAGE, lead_id=lead.id)
        await queue.enqueue(db, org_id=org.id, kind=JobKind.DRAFT_LEAD, lead_id=lead.id)
        await queue.enqueue(db, org_id=org.id, kind=JobKind.SEND_MESSAGE, lead_id=other.id)
        await db.commit()

        cancelled = await queue.cancel_for_lead(db, org_id=org.id, lead_id=lead.id)
        await db.commit()

        assert cancelled == 2
        remaining = (await db.execute(select(Job).where(Job.status == "queued"))).scalars().all()
        assert [j.lead_id for j in remaining] == [other.id]

    async def test_cancellation_can_be_limited_to_certain_kinds(self, db, org, lead_factory):
        lead = await lead_factory()
        await queue.enqueue(db, org_id=org.id, kind=JobKind.SEND_MESSAGE, lead_id=lead.id)
        await queue.enqueue(db, org_id=org.id, kind=JobKind.DRAFT_LEAD, lead_id=lead.id)
        await db.commit()

        cancelled = await queue.cancel_for_lead(
            db, org_id=org.id, lead_id=lead.id, kinds=[JobKind.SEND_MESSAGE]
        )
        await db.commit()
        assert cancelled == 1

    async def test_a_running_job_is_not_cancelled(self, db, org, lead_factory):
        """The worker holding it re-checks eligibility before acting, so let its lease stand."""
        lead = await lead_factory()
        await queue.enqueue(db, org_id=org.id, kind=JobKind.SEND_MESSAGE, lead_id=lead.id)
        await db.commit()
        await queue.claim(db, worker_id="w1", kinds=[JobKind.SEND_MESSAGE])
        await db.commit()

        assert await queue.cancel_for_lead(db, org_id=org.id, lead_id=lead.id) == 0

    async def test_another_org_cannot_cancel_our_jobs(self, db, org, lead_factory):
        import uuid

        lead = await lead_factory()
        await queue.enqueue(db, org_id=org.id, kind=JobKind.SEND_MESSAGE, lead_id=lead.id)
        await db.commit()

        assert await queue.cancel_for_lead(db, org_id=uuid.uuid4(), lead_id=lead.id) == 0


class TestQueueDepth:
    async def test_counts_by_status(self, db, org):
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await queue.enqueue(db, org_id=org.id, kind=JobKind.ENRICH_LEAD)
        await db.commit()
        claimed = await queue.claim(db, worker_id="w1", kinds=[JobKind.ENRICH_LEAD])
        await queue.succeed(db, claimed[0].id)
        await db.commit()

        depth = await queue.queue_depth(db, org_id=org.id)
        assert depth == {"queued": 1, "succeeded": 1}
