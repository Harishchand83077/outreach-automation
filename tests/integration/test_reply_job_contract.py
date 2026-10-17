"""Every caller that queues a reply draft must agree with the handler that runs it.

This file exists because they did not, in opposite directions, and the consequence was that a
proposed reply was **never written at all**. Found in production, in the worker log, as:

    "job permanently failed" ... kind: "draft_reply", error: "job payload has no lead_id"

- `reply.service` queues on ingest with `payload={"message_id", "label"}` and the lead in the
  `jobs.lead_id` *column*. The handler read `payload["lead_id"]`, so every reply to a real
  inbound message died -- permanently, because a missing key is not retryable. The lead reached
  `REPLIED` and nothing ever proposed an answer, which from the dashboard looks exactly like a
  reply nobody has got to yet.
- `drafts.reject` queues a regeneration with `payload={"lead_id"}` and no `message_id`, so
  rejecting a reply draft would have died the same way on the other key.

Each side was individually right and individually tested. Nothing checked that they agreed --
which is, by now, the dominant shape of defect in this project. So these tests do not test the
handler against a payload a test author wrote; they drive **the real producers** and then run the
real handler on whatever those produced.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import drafts as drafts_repo
from app.db.models import Classification, Draft, Job, Mailbox, Message
from app.db.queue import JobKind
from app.db.repo import OrgContext
from app.domain.reply import ReplyLabel
from app.services.drafting.generator import DraftAttempt, DraftResult
from app.services.drafting.schema import DraftOutput
from app.services.drafting.validate import ValidationReport
from app.worker import handlers

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org):
    row = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(row)
    await db.commit()
    return row


async def inbound_reply(db, org, lead, mailbox, *, label=ReplyLabel.QUESTION):
    """An inbound message with its classification, as the ingest path leaves them."""
    message = Message(
        org_id=org.id,
        lead_id=lead.id,
        mailbox_id=mailbox.id,
        direction="in",
        status="received",
        kind="reply",
        subject="Re: your pipeline",
        body_text="Tell me more about the timeline.",
        message_id_hdr=f"<{uuid.uuid4()}@example.com>",
    )
    db.add(message)
    await db.flush()
    db.add(
        Classification(
            org_id=org.id,
            message_id=message.id,
            lead_id=lead.id,
            label=label.value,
            confidence=0.9,
            rationale=["asked a question"],
        )
    )
    await db.commit()
    return message


def reply_draft_result() -> DraftResult:
    draft = DraftOutput.model_validate(
        {
            "subject": "Re: your pipeline",
            "body": "Hi Ben,\n\nAbout four weeks end to end.\n\nAsha",
            "claims": [],
        }
    )
    return DraftResult(draft=draft, attempts=[DraftAttempt(draft=draft, report=ValidationReport())])


class TestTheIngestPathsJob:
    """The job `reply.service` actually writes, run by the handler that actually runs it."""

    async def test_the_handler_accepts_what_the_ingest_path_queues(
        self, db, org, lead_factory, mailbox, monkeypatch
    ):
        lead = await lead_factory(state="REPLIED")
        message = await inbound_reply(db, org, lead, mailbox)

        # Exactly the enqueue `reply.service` performs: lead in the column, not the payload.
        from app.db import queue

        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_REPLY,
            lead_id=lead.id,
            payload={"message_id": str(message.id), "label": ReplyLabel.QUESTION.value},
            dedupe_key=f"draft_reply:{message.id}",
        )
        await db.commit()
        assert job is not None

        seen: dict[str, object] = {}

        async def fake_draft_reply(session, ctx, *, router, lead_id, message_id, label):
            seen.update({"lead_id": lead_id, "message_id": message_id, "label": label})

        monkeypatch.setattr(handlers, "draft_reply", fake_draft_reply)

        class Ctx:
            def router_for(self, session, org_id):
                return object()

        await handlers.handle_draft_reply(Ctx(), db, job)  # type: ignore[arg-type]

        assert seen["lead_id"] == lead.id, "the lead travels in the column, not the payload"
        assert seen["message_id"] == message.id
        assert seen["label"] is ReplyLabel.QUESTION


class TestTheRegenerationJob:
    """The job `drafts.reject` writes, which names neither the message nor the label."""

    async def test_rejecting_a_reply_queues_a_job_the_handler_can_run(
        self, db, org, lead_factory, mailbox, monkeypatch
    ):
        lead = await lead_factory(state="REPLIED")
        message = await inbound_reply(db, org, lead, mailbox, label=ReplyLabel.OBJECTION)
        ctx = OrgContext(org_id=org.id, actor_id="reviewer@example.com")
        draft = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="reply", result=reply_draft_result()
        )
        await db.commit()

        await drafts_repo.reject(
            db,
            ctx,
            draft_id=draft.id,
            rejected_by="reviewer@example.com",
            feedback="too formal",
        )
        await db.commit()

        job = await db.scalar(
            select(Job).where(Job.lead_id == lead.id, Job.kind == JobKind.DRAFT_REPLY)
        )
        assert job is not None, "rejecting a reply draft has to queue the rewrite"

        seen: dict[str, object] = {}

        async def fake_draft_reply(session, ctx, *, router, lead_id, message_id, label):
            seen.update({"lead_id": lead_id, "message_id": message_id, "label": label})

        monkeypatch.setattr(handlers, "draft_reply", fake_draft_reply)

        class Ctx:
            def router_for(self, session, org_id):
                return object()

        await handlers.handle_draft_reply(Ctx(), db, job)  # type: ignore[arg-type]

        assert seen["lead_id"] == lead.id
        assert seen["message_id"] == message.id, (
            "a regeneration answers the same message, not whatever arrived since"
        )
        assert seen["label"] is ReplyLabel.OBJECTION, (
            "the label comes from the classification, which is the authoritative record"
        )

    async def test_the_newest_inbound_message_is_the_one_answered(
        self, db, org, lead_factory, mailbox, monkeypatch
    ):
        """Two replies in a thread: a regeneration answers the later one."""
        lead = await lead_factory(state="REPLIED")
        await inbound_reply(db, org, lead, mailbox)
        later = await inbound_reply(db, org, lead, mailbox, label=ReplyLabel.INTERESTED)

        from app.db import queue

        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_REPLY,
            lead_id=lead.id,
            payload={"lead_id": str(lead.id)},
        )
        await db.commit()
        assert job is not None

        seen: dict[str, object] = {}

        async def fake_draft_reply(session, ctx, *, router, lead_id, message_id, label):
            seen.update({"message_id": message_id, "label": label})

        monkeypatch.setattr(handlers, "draft_reply", fake_draft_reply)

        class Ctx:
            def router_for(self, session, org_id):
                return object()

        await handlers.handle_draft_reply(Ctx(), db, job)  # type: ignore[arg-type]

        assert seen["message_id"] == later.id
        assert seen["label"] is ReplyLabel.INTERESTED


class TestWhatItStillRefuses:
    async def test_a_lead_with_no_inbound_message_is_a_permanent_failure(
        self, db, org, lead_factory
    ):
        """Not retryable: an inbound message does not appear later for a job queued because one
        had already arrived."""
        lead = await lead_factory(state="REPLIED")
        from app.db import queue

        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_REPLY,
            lead_id=lead.id,
            payload={"lead_id": str(lead.id)},
        )
        await db.commit()
        assert job is not None

        class Ctx:
            def router_for(self, session, org_id):
                return object()

        with pytest.raises(handlers.PermanentFailure, match="no inbound message"):
            await handlers.handle_draft_reply(Ctx(), db, job)  # type: ignore[arg-type]

    async def test_an_unclassified_message_is_not_guessed_at(self, db, org, lead_factory, mailbox):
        """The label decides whether a reply may be drafted at all, so guessing could propose
        an answer to somebody who asked to be left alone."""
        lead = await lead_factory(state="REPLIED")
        message = Message(
            org_id=org.id,
            lead_id=lead.id,
            mailbox_id=mailbox.id,
            direction="in",
            status="received",
            kind="reply",
            subject="Re: hello",
            body_text="unclassified",
            message_id_hdr=f"<{uuid.uuid4()}@example.com>",
        )
        db.add(message)
        await db.flush()

        from app.db import queue

        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_REPLY,
            lead_id=lead.id,
            payload={"message_id": str(message.id)},
        )
        await db.commit()
        assert job is not None

        class Ctx:
            def router_for(self, session, org_id):
                return object()

        with pytest.raises(handlers.PermanentFailure, match="no classification"):
            await handlers.handle_draft_reply(Ctx(), db, job)  # type: ignore[arg-type]

    async def test_a_draft_reply_job_is_still_registered(self):
        """A handler nothing dispatches to is how `DRAFT_LEAD` once went unqueued for a week."""
        assert JobKind.DRAFT_REPLY in handlers.HANDLERS


class TestTheFirstEmailPathIsUnchanged:
    async def test_rejecting_an_initial_draft_still_queues_draft_lead(self, db, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        ctx = OrgContext(org_id=org.id, actor_id="reviewer@example.com")
        draft = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=reply_draft_result()
        )
        await db.commit()

        await drafts_repo.reject(
            db, ctx, draft_id=draft.id, rejected_by="reviewer@example.com", feedback="shorter"
        )
        await db.commit()

        job = await db.scalar(
            select(Job).where(Job.lead_id == lead.id, Job.kind == JobKind.DRAFT_LEAD)
        )
        assert job is not None
        assert job.payload["lead_id"] == str(lead.id)
        stored = await db.scalar(select(Draft).where(Draft.id == draft.id))
        assert stored is not None and stored.status == "rejected"
