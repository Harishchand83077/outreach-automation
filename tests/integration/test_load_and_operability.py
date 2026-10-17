"""The fixes from the architecture review's §27.8: fan-out, orphans, regeneration and dead jobs.

Each of these was recorded as a gap before it was a symptom, which is the first time in this
project that has happened in that order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.db import drafts as drafts_repo
from app.db.models import Job, Mailbox, Message
from app.db.queue import JobKind
from app.db.repo import OrgContext
from app.services.drafting.generator import DraftAttempt, DraftResult
from app.services.drafting.schema import DraftOutput
from app.services.drafting.validate import ValidationReport
from app.services.privacy.retention import delete_orphan_inbound

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
def ctx(org):
    return OrgContext(org_id=org.id, actor_id="reviewer@example.com")


def a_draft() -> DraftResult:
    out = DraftOutput.model_validate(
        {"subject": "Your pipeline", "body": "Hi there,\n\nShort note.\n\nAsha", "claims": []}
    )
    return DraftResult(draft=out, attempts=[DraftAttempt(draft=out, report=ValidationReport())])


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


class TestOrphanedInboundMailIsNotKeptForEver:
    """Unmatched inbound mail is stored on purpose so a reply we failed to match is not lost.
    One run against a personal inbox ingested 1,400 unrelated messages -- other people's
    correspondence in our database, which is a privacy surface before it is a storage one."""

    async def test_an_old_orphan_is_deleted(self, db, org, mailbox):
        db.add(
            Message(
                org_id=org.id,
                lead_id=None,
                mailbox_id=mailbox.id,
                direction="in",
                status="received",
                kind="reply",
                subject="Unrelated",
                body_text="Not for us",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                created_at=NOW - timedelta(days=60),
            )
        )
        await db.commit()

        deleted = await delete_orphan_inbound(db, older_than=timedelta(days=45), now=NOW)
        await db.commit()

        assert deleted == 1

    async def test_a_recent_orphan_is_kept(self, db, org, mailbox):
        """`match_lead`'s fallback looks back 30 days, so a recent orphan may still be placed."""
        db.add(
            Message(
                org_id=org.id,
                lead_id=None,
                mailbox_id=mailbox.id,
                direction="in",
                status="received",
                kind="reply",
                subject="Maybe ours",
                body_text="Recent",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                created_at=NOW - timedelta(days=3),
            )
        )
        await db.commit()

        deleted = await delete_orphan_inbound(db, older_than=timedelta(days=45), now=NOW)
        await db.commit()

        assert deleted == 0

    async def test_a_message_attached_to_a_lead_is_never_touched(
        self, db, org, mailbox, lead_factory
    ):
        """A message on a lead is part of that lead's record and is governed by
        `LEAD_RETENTION_MONTHS` instead."""
        lead = await lead_factory(state="REPLIED")
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                mailbox_id=mailbox.id,
                direction="in",
                status="received",
                kind="reply",
                subject="Theirs",
                body_text="A real reply",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                created_at=NOW - timedelta(days=400),
            )
        )
        await db.commit()

        await delete_orphan_inbound(db, older_than=timedelta(days=45), now=NOW)
        await db.commit()

        still = await db.scalar(
            select(func.count()).select_from(Message).where(Message.lead_id == lead.id)
        )
        assert still == 1


class TestRejectingHasALimit:
    """Each rejection is a model call. A loop -- a lead the model cannot draft well, or a
    reviewer looking for a sentence that is not in the evidence -- would spend the day's quota
    on one lead while every other lead waits behind it."""

    async def test_the_sixth_rejection_is_refused_with_what_to_do_instead(
        self, db, ctx, lead_factory
    ):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        for _ in range(drafts_repo.MAX_REJECTIONS):
            draft = await drafts_repo.save_draft(
                db, ctx, lead=lead, kind="initial", result=a_draft()
            )
            await db.commit()
            await drafts_repo.reject(
                db, ctx, draft_id=draft.id, rejected_by="r@example.com", feedback="again"
            )
            await db.commit()

        final = await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=a_draft())
        await db.commit()

        with pytest.raises(ValueError, match="already been rejected"):
            await drafts_repo.reject(
                db, ctx, draft_id=final.id, rejected_by="r@example.com", feedback="no"
            )

    async def test_the_limit_is_per_kind(self, db, ctx, lead_factory):
        """A reply exhausting its rejections must not block the lead's first email."""
        lead = await lead_factory(state="REPLIED")
        for _ in range(drafts_repo.MAX_REJECTIONS):
            draft = await drafts_repo.save_draft(db, ctx, lead=lead, kind="reply", result=a_draft())
            await db.commit()
            await drafts_repo.reject(
                db, ctx, draft_id=draft.id, rejected_by="r@example.com", feedback="again"
            )
            await db.commit()

        initial = await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=a_draft())
        await db.commit()

        rejected = await drafts_repo.reject(
            db, ctx, draft_id=initial.id, rejected_by="r@example.com", feedback="shorter"
        )
        assert rejected.status == "rejected"

    async def test_under_the_limit_still_works(self, db, ctx, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        draft = await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=a_draft())
        await db.commit()

        rejected = await drafts_repo.reject(
            db, ctx, draft_id=draft.id, rejected_by="r@example.com", feedback="shorter"
        )
        await db.commit()

        assert rejected.status == "rejected"
        queued = await db.scalar(
            select(Job).where(Job.lead_id == lead.id, Job.kind == JobKind.DRAFT_LEAD)
        )
        assert queued is not None
