"""The `JobRunner` against a real queue.

§8. What these tests care about is not that a handler ran — that is easy — but what happens when
one does not: a failure must leave the job retryable, a bad payload must not burn five attempts
on an error that will never change, and a handler that raises must not take the runner with it.

The handlers themselves are faked. Their real behaviour is covered where it lives (research,
drafting, reply drafting each have their own suites); here the subject is the claim/run/record
cycle around them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.settings import config
from app.db import queue
from app.db.models import Job
from app.db.queue import JobKind
from app.worker.context import WorkerContext
from app.worker.handlers import PermanentFailure
from app.worker.runner import CONCURRENCY, JobRunner

pytestmark = pytest.mark.asyncio(loop_scope="session")

KIND = JobKind.RESEARCH_LEAD


@dataclass
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


def runner_with(ctx, handler, kind: str = KIND, monkeypatch=None) -> JobRunner:
    """A runner whose only handler is the one supplied."""
    runner = JobRunner(ctx, worker_id="test-worker", kinds=[kind])
    monkeypatch.setitem(
        __import__("app.worker.runner", fromlist=["HANDLERS"]).HANDLERS, kind, handler
    )
    return runner


async def add_job(session_factory, org, *, payload=None, kind: str = KIND) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        job = await queue.enqueue(
            session,
            org_id=org.id,
            kind=kind,
            payload=payload or {"lead_id": str(uuid.uuid4())},
        )
        assert job is not None
        return job.id


async def job_row(session_factory, job_id) -> Job:
    async with session_factory() as session:
        row = await session.get(Job, job_id)
        assert row is not None
        return row


class TestASuccessfulJob:
    async def test_it_is_marked_succeeded(self, session_factory, ctx, org, monkeypatch):
        seen = []

        async def handler(ctx_, session, job):
            seen.append(job.id)

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        job_id = await add_job(session_factory, org)

        report = await runner.run_batch()

        assert report.claimed == 1
        assert report.succeeded == 1
        assert seen == [job_id]
        assert (await job_row(session_factory, job_id)).status == "succeeded"

    async def test_the_handlers_work_is_committed(self, session_factory, ctx, org, monkeypatch):
        """The handler shares the transaction that marks the job done, so both land together."""
        marker = uuid.uuid4()

        async def handler(ctx_, session, job):
            await queue.enqueue(
                session, org_id=job.org_id, kind=JobKind.DRAFT_LEAD, payload={"marker": str(marker)}
            )

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        await add_job(session_factory, org)
        await runner.run_batch()

        async with session_factory() as session:
            rows = (
                (await session.execute(select(Job).where(Job.kind == JobKind.DRAFT_LEAD)))
                .scalars()
                .all()
            )
        assert any((row.payload or {}).get("marker") == str(marker) for row in rows)


class TestAFailingJob:
    async def test_a_transient_failure_is_requeued(self, session_factory, ctx, org, monkeypatch):
        async def handler(ctx_, session, job):
            raise RuntimeError("the website was down")

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        job_id = await add_job(session_factory, org)

        report = await runner.run_batch()

        assert report.failed == 1
        assert report.dead == 0
        row = await job_row(session_factory, job_id)
        assert row.status == "queued", "it will be tried again"
        assert "the website was down" in (row.last_error or "")

    async def test_a_permanent_failure_does_not_burn_retries(
        self, session_factory, ctx, org, monkeypatch
    ):
        """Retrying a bad payload only produces the same error more slowly."""

        async def handler(ctx_, session, job):
            raise PermanentFailure("payload has no lead_id")

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        job_id = await add_job(session_factory, org)

        report = await runner.run_batch()

        assert report.dead == 1
        row = await job_row(session_factory, job_id)
        assert row.status == "dead"
        assert row.attempts == 1, "it did not spend five attempts on a hopeless job"

    async def test_the_runner_survives_a_handler_that_raises(
        self, session_factory, ctx, org, monkeypatch
    ):
        calls = {"n": 0}

        async def handler(ctx_, session, job):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        await add_job(session_factory, org)
        await runner.run_batch()

        # The requeued job is not due yet (backoff), so enqueue a fresh one and check the
        # runner still works at all.
        await add_job(session_factory, org, payload={"lead_id": str(uuid.uuid4())})
        report = await runner.run_batch()
        assert report.succeeded == 1

    async def test_a_handlerless_kind_is_never_claimed(
        self, session_factory, ctx, org, monkeypatch
    ):
        """A job the runner cannot do stays visible instead of being failed repeatedly."""

        async def handler(ctx_, session, job):
            raise AssertionError("must not run")

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        job_id = await add_job(session_factory, org, kind=JobKind.SEND_MESSAGE)

        report = await runner.run_batch()

        assert report.claimed == 0
        assert (await job_row(session_factory, job_id)).status == "queued"


class TestConcurrency:
    async def test_the_batch_is_capped_by_the_per_kind_limit(
        self, session_factory, ctx, org, monkeypatch
    ):
        async def handler(ctx_, session, job):
            return None

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        for _ in range(CONCURRENCY[KIND] + 3):
            await add_job(session_factory, org)

        report = await runner.run_batch()
        assert report.claimed == CONCURRENCY[KIND]

    async def test_capacity_is_released_after_the_batch(self, session_factory, ctx, monkeypatch):
        async def handler(ctx_, session, job):
            return None

        runner = runner_with(ctx, handler, monkeypatch=monkeypatch)
        before = runner.capacity()
        await runner.run_batch()
        assert runner.capacity() == before, "a finished job holds no slot"


class TestWhatTheRunnerRefusesToDo:
    async def test_sending_is_not_a_job_kind_it_handles(self):
        """Hard rule 1: the SendLoop is the only code that sends. Two senders race the cap."""
        from app.worker.handlers import HANDLERS

        assert JobKind.SEND_MESSAGE not in HANDLERS

    async def test_classification_is_not_a_separate_job(self):
        """§10.3: the label is persisted in the same transaction as the message."""
        from app.worker.handlers import HANDLERS

        assert JobKind.CLASSIFY_REPLY not in HANDLERS

    async def test_the_runner_module_cannot_send(self):
        import app.worker.runner as module
        from tests.helpers import executable_source

        source = executable_source(module)
        for forbidden in ("smtplib", "SmtpSender", "sendmail"):
            assert forbidden not in source
