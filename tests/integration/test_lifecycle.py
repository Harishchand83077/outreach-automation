"""The follow-up that was never drafted, and the lead that never ended.

Everything both halves need has existed since Phase 5: `SendLoop` sets `next_action_at`,
`followup_due_at` computes when a follow-up is due, the state machine has `SENT ->
FOLLOWUP_SENT`, `CampaignSettings` has `followup_after_days` and `max_followups`, and
`find_candidates` looks for an approved `Draft(kind="followup")`.

**Nothing in the system ever wrote that draft.** A grep across `app/` returned the constant and
its consumers and no producer, so the locked product rule "one follow-up three calendar days
after the first email" had never once happened -- invisible only because no lead had yet sat
three days past a first send with anybody watching.

`no_response_at` had the same shape: it computed the date and no code wrote the state, so a lead
that never replied stayed `SENT` for ever, `final_wait_days` did nothing, and those leads were
never eligible for retention -- which only removes *terminal* leads.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import Draft, Job, Mailbox, Message
from app.db.queue import JobKind
from app.domain.campaign import CampaignSettings
from app.domain.state import LeadState
from app.services.lifecycle import run_lifecycle

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: A Tuesday inside the send window, so nothing here depends on when the suite runs.
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


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


async def sent_lead(db, org, campaign, mailbox, lead_factory, *, sent_days_ago=4, followups=0):
    """A lead that was emailed, with the outbound message that proves it."""
    sent_at = NOW - timedelta(days=sent_days_ago)
    lead = await lead_factory(
        state=LeadState.SENT.value,
        followups_sent=followups,
        last_message_at=sent_at,
        next_action_at=sent_at + timedelta(days=3),
    )
    db.add(
        Message(
            org_id=org.id,
            lead_id=lead.id,
            mailbox_id=mailbox.id,
            direction="out",
            status="sent",
            kind="initial",
            subject="Your pipeline",
            body_text="Hello",
            message_id_hdr=f"<{uuid.uuid4()}@example.com>",
            idempotency_key=str(uuid.uuid4()),
            sent_at=sent_at,
        )
    )
    await db.commit()
    return lead


class TestTheFollowupIsQueued:
    async def test_a_due_lead_gets_a_followup_job(self, db, org, campaign, mailbox, lead_factory):
        lead = await sent_lead(db, org, campaign, mailbox, lead_factory)

        report = await run_lifecycle(db, now=NOW)
        await db.commit()

        assert report.followups_queued == 1
        job = await db.scalar(
            select(Job).where(Job.lead_id == lead.id, Job.kind == JobKind.DRAFT_FOLLOWUP)
        )
        assert job is not None
        assert job.payload["number"] == 1

    async def test_a_lead_that_is_not_due_yet_is_left_alone(
        self, db, org, campaign, mailbox, lead_factory
    ):
        await sent_lead(db, org, campaign, mailbox, lead_factory, sent_days_ago=0)

        report = await run_lifecycle(db, now=NOW)

        assert report.followups_queued == 0

    async def test_running_twice_queues_one_job(self, db, org, campaign, mailbox, lead_factory):
        """The pass runs every minute. It has to be idempotent or it would queue sixty jobs an
        hour for the same lead."""
        lead = await sent_lead(db, org, campaign, mailbox, lead_factory)

        await run_lifecycle(db, now=NOW)
        await db.commit()
        await run_lifecycle(db, now=NOW)
        await db.commit()

        jobs = (
            (
                await db.execute(
                    select(Job).where(Job.lead_id == lead.id, Job.kind == JobKind.DRAFT_FOLLOWUP)
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1

    async def test_a_lead_with_a_waiting_followup_draft_is_skipped(
        self, db, org, campaign, mailbox, lead_factory
    ):
        """Two drafts of the same email in front of the same reviewer is not a queue."""
        lead = await sent_lead(db, org, campaign, mailbox, lead_factory)
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="followup",
                version=1,
                subject="Following up",
                body="Just checking in.",
                status="pending_review",
            )
        )
        await db.commit()

        report = await run_lifecycle(db, now=NOW)

        assert report.followups_queued == 0

    async def test_a_campaign_that_sends_no_followups_queues_none(
        self, db, org, campaign, mailbox, lead_factory
    ):
        campaign.settings = {**(campaign.settings or {}), "max_followups": 0}
        await db.commit()
        await sent_lead(db, org, campaign, mailbox, lead_factory)

        report = await run_lifecycle(db, now=NOW)

        assert report.followups_queued == 0

    async def test_a_paused_campaign_queues_none(self, db, org, campaign, mailbox, lead_factory):
        await sent_lead(db, org, campaign, mailbox, lead_factory)
        campaign.paused = True
        await db.commit()

        report = await run_lifecycle(db, now=NOW)

        assert report.followups_queued == 0

    async def test_a_paused_lead_queues_none(self, db, org, campaign, mailbox, lead_factory):
        lead = await sent_lead(db, org, campaign, mailbox, lead_factory)
        lead.paused = True
        await db.commit()

        report = await run_lifecycle(db, now=NOW)

        assert report.followups_queued == 0

    async def test_an_unreadable_campaign_skips_only_itself(
        self, db, org, campaign, mailbox, lead_factory
    ):
        """The same rule as the send loop: one bad row is that campaign's problem."""
        await sent_lead(db, org, campaign, mailbox, lead_factory)
        campaign.settings = {"send_days": ["monday"]}
        await db.commit()

        report = await run_lifecycle(db, now=NOW)

        assert report.followups_queued == 0
        assert report.skipped and "campaign settings cannot be read" in report.skipped


class TestTheLeadEventuallyEnds:
    async def test_a_silent_lead_is_closed_after_its_last_followup(
        self, db, org, campaign, mailbox, lead_factory
    ):
        settings = CampaignSettings.model_validate(campaign.settings or {})
        lead = await sent_lead(
            db,
            org,
            campaign,
            mailbox,
            lead_factory,
            sent_days_ago=settings.final_wait_days + 2,
            followups=settings.max_followups,
        )

        report = await run_lifecycle(db, now=NOW)
        await db.commit()
        await db.refresh(lead)

        assert report.closed_silent == 1
        assert lead.state == LeadState.NO_RESPONSE.value

    async def test_a_lead_still_owed_a_followup_is_not_closed(
        self, db, org, campaign, mailbox, lead_factory
    ):
        """Closing one that is still owed a follow-up would be giving up early."""
        lead = await sent_lead(
            db, org, campaign, mailbox, lead_factory, sent_days_ago=30, followups=0
        )

        report = await run_lifecycle(db, now=NOW)
        await db.commit()
        await db.refresh(lead)

        assert report.closed_silent == 0
        assert lead.state == LeadState.SENT.value

    async def test_a_lead_inside_the_waiting_period_is_not_closed(
        self, db, org, campaign, mailbox, lead_factory
    ):
        settings = CampaignSettings.model_validate(campaign.settings or {})
        lead = await sent_lead(
            db,
            org,
            campaign,
            mailbox,
            lead_factory,
            sent_days_ago=1,
            followups=settings.max_followups,
        )

        report = await run_lifecycle(db, now=NOW)
        await db.refresh(lead)

        assert report.closed_silent == 0
        assert lead.state == LeadState.SENT.value

    async def test_a_lead_with_unprocessed_inbound_mail_is_not_closed(
        self, db, org, campaign, mailbox, lead_factory
    ):
        """Closing a lead whose reply is still being classified would throw away the reply."""
        settings = CampaignSettings.model_validate(campaign.settings or {})
        lead = await sent_lead(
            db,
            org,
            campaign,
            mailbox,
            lead_factory,
            sent_days_ago=settings.final_wait_days + 2,
            followups=settings.max_followups,
        )
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                mailbox_id=mailbox.id,
                direction="in",
                status="received",
                kind="reply",
                subject="Re: your pipeline",
                body_text="Sorry for the delay",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
            )
        )
        await db.commit()

        report = await run_lifecycle(db, now=NOW)
        await db.refresh(lead)

        assert lead.state == LeadState.SENT.value
        assert report.skipped and "an inbound message is still being processed" in report.skipped

    async def test_closing_is_idempotent(self, db, org, campaign, mailbox, lead_factory):
        settings = CampaignSettings.model_validate(campaign.settings or {})
        await sent_lead(
            db,
            org,
            campaign,
            mailbox,
            lead_factory,
            sent_days_ago=settings.final_wait_days + 2,
            followups=settings.max_followups,
        )

        await run_lifecycle(db, now=NOW)
        await db.commit()
        second = await run_lifecycle(db, now=NOW)
        await db.commit()

        assert second.closed_silent == 0


class TestTheHandlerWritesTheDraft:
    async def test_a_template_campaign_renders_its_followup(
        self, db, org, campaign, mailbox, lead_factory, session_factory
    ):
        from app.core.settings import config
        from app.worker import handlers
        from app.worker.context import WorkerContext

        campaign.mode = "template"
        campaign.persona = {"sender_name": "Asha Rao"}
        campaign.templates = {
            "initial": {"subject": "Hi", "body": "Hello {{first_name|there}}"},
            "followups": [
                {
                    "subject": "Re: Hi",
                    "body": "Following up, {{first_name|there}}.\n\n{{sender_name}}",
                    "after_days": 3,
                }
            ],
        }
        await db.commit()
        lead = await sent_lead(db, org, campaign, mailbox, lead_factory)

        await run_lifecycle(db, now=NOW)
        await db.commit()
        job = await db.scalar(
            select(Job).where(Job.lead_id == lead.id, Job.kind == JobKind.DRAFT_FOLLOWUP)
        )
        assert job is not None

        ctx = WorkerContext(
            session_factory=session_factory,
            settings=config,
            poller=object(),  # type: ignore[arg-type]
            sender=object(),  # type: ignore[arg-type]
        )
        await handlers.handle_draft_followup(ctx, db, job)
        await db.commit()

        draft = await db.scalar(
            select(Draft).where(Draft.lead_id == lead.id, Draft.kind == "followup")
        )
        assert draft is not None
        assert draft.status == "pending_review"
        assert "Following up" in draft.body
        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value, "a follow-up is sent *from* SENT"

    async def test_a_campaign_with_no_template_for_that_followup_does_not_retry(
        self, db, org, campaign, mailbox, lead_factory, session_factory
    ):
        """`max_followups` and the template list are set in two places and the smaller wins, or
        the campaign tries to send an email nobody wrote."""
        from app.core.settings import config
        from app.worker import handlers
        from app.worker.context import WorkerContext

        campaign.mode = "template"
        campaign.persona = {"sender_name": "Asha Rao"}
        campaign.templates = {
            "initial": {"subject": "Hi", "body": "Hello"},
            "followups": [],
        }
        await db.commit()
        lead = await sent_lead(db, org, campaign, mailbox, lead_factory)

        from app.db import queue

        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_FOLLOWUP,
            lead_id=lead.id,
            payload={"lead_id": str(lead.id), "number": 1},
        )
        await db.commit()
        assert job is not None

        ctx = WorkerContext(
            session_factory=session_factory,
            settings=config,
            poller=object(),  # type: ignore[arg-type]
            sender=object(),  # type: ignore[arg-type]
        )

        with pytest.raises(handlers.PermanentFailure, match="no template for follow-up"):
            await handlers.handle_draft_followup(ctx, db, job)
