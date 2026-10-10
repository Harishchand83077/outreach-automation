"""The `Scheduler`, and the assembly of the process as a whole.

§8 and §9.5. The scheduler is the task that cleans up after crashes, so the things worth proving
are that it requeues work a dead worker was holding, that it resolves interrupted sends without
ever re-sending, and that running it twice changes nothing — because `POST /internal/tick` and
the loop itself may both call it in the same minute.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.core.settings import config
from app.db import queue
from app.db.models import Draft, Job, Mailbox, Message
from app.db.queue import JobKind
from app.domain.state import LeadState
from app.services.reply.recovery import GRACE
from app.worker.context import WorkerContext
from app.worker.scheduler import Scheduler

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)


class FakePoller:
    """Answers the Sent-folder question from a dict, and records what it was asked."""

    def __init__(self, found: dict[str, str] | None = None) -> None:
        self.found = found or {}
        self.asked: list[str] = []

    async def find_by_message_id(self, message_id, *, folders=()):
        self.asked.append(message_id)
        return self.found.get(message_id)


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org, campaign):
    record = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(record)
    await db.flush()
    campaign.mailbox_id = record.id
    await db.commit()
    return record


def context(session_factory, poller) -> WorkerContext:
    return WorkerContext(
        session_factory=session_factory,
        settings=config,
        poller=poller,  # type: ignore[arg-type]
        sender=object(),  # type: ignore[arg-type]
    )


async def expired_job(session_factory, org) -> uuid.UUID:
    """A job a worker claimed and then died holding."""
    async with session_factory() as session, session.begin():
        job = await queue.enqueue(
            session,
            org_id=org.id,
            kind=JobKind.RESEARCH_LEAD,
            payload={"lead_id": str(uuid.uuid4())},
        )
        assert job is not None
        claimed = await queue.claim(
            session, worker_id="a-worker-that-died", kinds=[JobKind.RESEARCH_LEAD], limit=1
        )
        assert claimed
        row = await session.get(Job, claimed[0].id)
        assert row is not None
        row.locked_until = NOW - timedelta(minutes=5)
        return row.id


async def stuck_send(db, org, lead) -> Message:
    draft = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind="initial",
        version=1,
        subject="quick question",
        body="our pitch",
        status="approved",
    )
    db.add(draft)
    await db.flush()
    row = Message(
        org_id=org.id,
        lead_id=lead.id,
        direction="out",
        kind="initial",
        draft_id=draft.id,
        message_id_hdr="<stuck-sched@outreach.example>",
        subject="quick question",
        body_text="our pitch",
        from_addr="asha@outreach.example",
        to_addr=lead.email_norm,
        status="sending",
        idempotency_key=f"initial:{lead.id}:1",
        created_at=NOW - GRACE - timedelta(minutes=5),
    )
    db.add(row)
    await db.commit()
    return row


class TestTheReaper:
    async def test_an_expired_lease_is_requeued(self, session_factory, org):
        job_id = await expired_job(session_factory, org)
        scheduler = Scheduler(context(session_factory, FakePoller()))

        report = await scheduler.run_cycle(NOW)

        assert report.requeued >= 1
        async with session_factory() as session:
            row = await session.get(Job, job_id)
        assert row is not None
        assert row.status == "queued", "the work a dead worker held is not lost"
        assert row.locked_by is None


class TestInterruptedSends:
    async def test_one_found_in_sent_is_resolved_without_resending(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_send(db, org, lead)
        poller = FakePoller({"<stuck-sched@outreach.example>": "[Gmail]/Sent Mail"})

        report = await Scheduler(context(session_factory, poller)).run_cycle(NOW)

        assert report.recovered_sent == 1
        await db.refresh(row)
        assert row.status == "sent"

    async def test_one_that_cannot_be_found_asks_for_a_human(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        """Never auto-retried: a duplicate cold email is worse than one needing confirmation."""
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_send(db, org, lead)

        report = await Scheduler(context(session_factory, FakePoller())).run_cycle(NOW)

        assert report.needs_human == 1
        assert any("need a person" in alert for alert in report.alerts)
        await db.refresh(row)
        assert row.status == "unknown"


class TestItIsSafeToRunTwice:
    async def test_a_second_cycle_changes_nothing(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        """The loop and `POST /internal/tick` may both fire in the same minute."""
        lead = await lead_factory(state=LeadState.APPROVED.value)
        await stuck_send(db, org, lead)
        scheduler = Scheduler(context(session_factory, FakePoller()))

        first = await scheduler.run_cycle(NOW)
        second = await scheduler.run_cycle(NOW)

        assert first.needs_human == 1
        assert second.needs_human == 0, "already resolved rows are not revisited"


class TestTheProcess:
    async def test_all_five_tasks_are_assembled(self, session_factory):
        """§4 names five. A missing one is a whole capability silently absent."""
        from app.worker.main import build_tasks

        tasks = build_tasks(context(session_factory, FakePoller()))
        assert [task.name for task in tasks] == [
            "job_runner",
            "send_loop",
            "imap_loop",
            "scheduler",
            "heartbeat",
        ]

    async def test_every_task_shares_one_health_record(self, session_factory):
        from app.worker.main import build_tasks

        ctx = context(session_factory, FakePoller())
        tasks = build_tasks(ctx)
        assert all(task.health is ctx.health for task in tasks)

    async def test_only_one_task_can_reach_smtp(self, session_factory):
        """Hard rule 1, structurally: four of the five import nothing that can send."""
        import importlib

        from tests.helpers import executable_source

        for name in ("runner", "imap_loop", "scheduler", "heartbeat"):
            module = importlib.import_module(f"app.worker.{name}")
            source = executable_source(module)
            assert "SmtpSender" not in source, f"{name} can reach SMTP"
            assert "smtplib" not in source, f"{name} can reach SMTP"
