"""The two steps that close the loop: ask for a follow-up, and give up on silence.

Everything needed for both has existed since Phase 5 and nothing connected them.

- `SendLoop` sets `leads.next_action_at` after a first send, `domain/scheduling.followup_due_at`
  computes when a follow-up is due, the state machine has `SENT -> FOLLOWUP_SENT`,
  `CampaignSettings` has `followup_after_days` and `max_followups`, and `find_candidates` looks
  for an approved `Draft(kind="followup")` -- **which nothing in the system ever wrote.** So the
  locked product rule "one follow-up three calendar days after the first email" has never once
  happened.
- `no_response_at` computes when a silent lead may be closed, and no code wrote `NO_RESPONSE`.
  A lead that never replied stayed `SENT` for ever, so `final_wait_days` did nothing, the
  lifecycle had no end, and those leads were never eligible for retention -- which only removes
  *terminal* leads.

Both are the same shape as every other defect in this project: a correct table and a correct
consumer with nothing producing the thing between them.

This module is the producer. It is pure orchestration over the existing pieces: it decides
*which* leads are due and enqueues the drafting job, and it never writes a draft, never calls a
model and never sends. Those stay where they are.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Draft, Lead, Message
from app.db.queue import JobKind, enqueue
from app.db.state import transition
from app.domain.campaign import CampaignSettings
from app.domain.scheduling import no_response_at
from app.domain.state import LeadState

#: How many leads one pass may act on. A pass runs every minute, so this is a rate limit rather
#: than a cap on the work: a backlog drains over several minutes instead of enqueueing ten
#: thousand jobs in one transaction.
BATCH = 100

#: Statuses a draft can be in and still count as "one already exists for this step".
_LIVE = ("generated", "validated", "pending_review", "approved")


@dataclass
class LifecycleReport:
    followups_queued: int = 0
    closed_silent: int = 0
    skipped: dict[str, int] | None = None

    def skip(self, reason: str) -> None:
        if self.skipped is None:
            self.skipped = {}
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


async def run_lifecycle(
    session: AsyncSession, *, now: datetime | None = None, batch: int = BATCH
) -> LifecycleReport:
    """One pass: queue the follow-ups that are due, close the leads that have gone quiet.

    The caller commits. Both halves are idempotent -- a follow-up is not queued when one is
    already drafted or queued, and a lead already closed is not a candidate -- so running twice
    in the same minute changes nothing.
    """
    now = now or datetime.now(tz=UTC)
    report = LifecycleReport()

    await _queue_due_followups(session, now=now, batch=batch, report=report)
    await _close_silent_leads(session, now=now, batch=batch, report=report)
    return report


async def _queue_due_followups(
    session: AsyncSession, *, now: datetime, batch: int, report: LifecycleReport
) -> None:
    """Ask for a follow-up draft for every lead whose follow-up has come due.

    `next_action_at` is the clock, set by the send loop when the first email went out and rolled
    into the next window by `followup_due_at`. A lead is due when that time has passed and it
    has not had all the follow-ups its campaign allows.
    """
    rows = (
        await session.execute(
            select(Lead, Campaign)
            .join(Campaign, Campaign.id == Lead.campaign_id)
            .where(
                Lead.state == LeadState.SENT.value,
                Lead.paused.is_(False),
                Lead.next_action_at.is_not(None),
                Lead.next_action_at <= now,
                Campaign.paused.is_(False),
                Campaign.status == "running",
            )
            .order_by(Lead.next_action_at)
            .limit(batch)
        )
    ).all()

    for lead, campaign in rows:
        try:
            settings = CampaignSettings.model_validate(campaign.settings or {})
        except ValidationError:
            # One unreadable campaign is that campaign's problem, exactly as in the send loop.
            report.skip("campaign settings cannot be read")
            continue

        if settings.max_followups <= 0:
            report.skip("the campaign sends no follow-ups")
            continue
        if lead.followups_sent >= settings.max_followups:
            # Nothing more to send. The silent-close pass below is what finishes this lead.
            report.skip("all follow-ups already sent")
            continue

        existing = await session.scalar(
            select(func.count())
            .select_from(Draft)
            .where(
                Draft.lead_id == lead.id,
                Draft.kind == "followup",
                Draft.status.in_(_LIVE),
            )
        )
        if existing:
            # A follow-up is already written and waiting for a person. Queuing another would
            # put two drafts of the same email in front of the same reviewer.
            report.skip("a follow-up draft is already waiting")
            continue

        queued = await enqueue(
            session,
            org_id=lead.org_id,
            kind=JobKind.DRAFT_FOLLOWUP,
            lead_id=lead.id,
            payload={"lead_id": str(lead.id), "number": lead.followups_sent + 1},
            # Covers only live statuses, so a second pass while the first job is still queued
            # is a no-op, and a new one is allowed once that job has finished.
            dedupe_key=f"followup:{lead.id}:{lead.followups_sent + 1}",
        )
        if queued is not None:
            report.followups_queued += 1


async def _close_silent_leads(
    session: AsyncSession, *, now: datetime, batch: int, report: LifecycleReport
) -> None:
    """Close leads that have had everything we were going to send and said nothing.

    Only leads that have exhausted their follow-ups: closing one that is still owed a follow-up
    would be giving up early. `no_response_at` decides how long silence has to last, from the
    last message we actually sent rather than from the import.
    """
    candidates = (
        await session.execute(
            select(Lead, Campaign)
            .join(Campaign, Campaign.id == Lead.campaign_id)
            .where(
                Lead.state.in_((LeadState.SENT.value, LeadState.FOLLOWUP_SENT.value)),
                Lead.last_message_at.is_not(None),
            )
            .order_by(Lead.last_message_at)
            .limit(batch)
        )
    ).all()

    for lead, campaign in candidates:
        try:
            settings = CampaignSettings.model_validate(campaign.settings or {})
        except ValidationError:
            report.skip("campaign settings cannot be read")
            continue

        if lead.followups_sent < settings.max_followups:
            # Still owed a follow-up. Giving up now would be giving up early.
            continue
        assert lead.last_message_at is not None
        if now < no_response_at(last_sent_at=lead.last_message_at, settings=settings):
            continue

        # A reply that arrived but has not been processed would be lost if we closed now. The
        # state machine lets a late reply reopen `NO_RESPONSE`, so this is recoverable either
        # way -- but not closing a lead with unread mail waiting is the cheaper correctness.
        unread = await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.lead_id == lead.id,
                Message.direction == "in",
                Message.created_at > lead.last_message_at,
            )
        )
        if unread:
            report.skip("an inbound message is still being processed")
            continue

        await transition(
            session,
            org_id=lead.org_id,
            lead_id=lead.id,
            to_state=LeadState.NO_RESPONSE,
            actor="system",
            data={
                "after_days": settings.final_wait_days,
                "followups_sent": lead.followups_sent,
            },
            now=now,
        )
        report.closed_silent += 1


async def followup_number(session: AsyncSession, *, lead_id: uuid.UUID) -> int:
    """Which follow-up this would be, one-based. Read from the lead rather than the payload, so
    a job that waited in the queue while something else sent cannot write the wrong one."""
    lead = await session.get(Lead, lead_id)
    return (lead.followups_sent if lead is not None else 0) + 1
