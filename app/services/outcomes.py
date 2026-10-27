"""Recording what happened to a lead, so it can be learned from later.

`docs/DATASET_DESIGN.md` is the design; `app/domain/outcome.py` is the vocabulary, which has no
I/O so the two rules can be tested against a clock. This module is the thin layer that writes.

**Three properties it has to keep, and they are the whole point:**

1. **Silence is never written as a negative.** A lead with no reply has `outcome = NULL` until a
   wait window passes, and then `no_reply_after_window` with `censored = true`. The database
   enforces that pairing (`ck_lead_outcomes_censoring`) rather than trusting this module.
2. **A human outranks a model.** Once `confirmed_by` names a person, a later classification does
   not quietly overwrite what they said.
3. **It is written in the caller's transaction**, beside the classification that justifies it —
   the same rule every other write in this project follows. An outcome recorded while the
   classification it came from rolled back would be a result with no cause.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Lead, LeadOutcome, Message
from app.domain.outcome import Outcome, Stage, outcome_for_label, resolve_silence, stages_for_kind
from app.domain.reply import ReplyLabel

logger = logging.getLogger(__name__)

#: How long to watch a lead that never replies before calling it `no_reply_after_window`.
#: Generous on purpose: the cost of waiting is a row that says NULL for longer, and the cost of
#: being hasty is a dataset that records "no" for people who answered in week three.
DEFAULT_WAIT_DAYS = 21

SYSTEM = "system"


async def _row_for(session: AsyncSession, lead: Lead) -> LeadOutcome:
    """This lead's outcome row, created if it does not exist yet.

    `campaign_kind` is copied in rather than joined at read time: a campaign can be deleted, and
    an exported row still has to be able to say what kind produced it.
    """
    row = await session.scalar(select(LeadOutcome).where(LeadOutcome.lead_id == lead.id))
    if row is not None:
        return row

    campaign = await session.get(Campaign, lead.campaign_id) if lead.campaign_id else None
    row = LeadOutcome(
        org_id=lead.org_id,
        lead_id=lead.id,
        campaign_id=lead.campaign_id,
        campaign_kind=getattr(campaign, "kind", None) or "outreach",
        stage=Stage.SENT.value,
    )
    session.add(row)
    await session.flush()
    return row


async def record_first_send(
    session: AsyncSession, *, lead: Lead, sent_at: datetime, wait_days: int = DEFAULT_WAIT_DAYS
) -> LeadOutcome:
    """Start the clock. Called once, when the first email to this lead leaves.

    `first_sent_at` is what makes silence measurable at all: without it there is no window to be
    inside or past, and a lead that never replied is indistinguishable from one never written to.
    """
    row = await _row_for(session, lead)
    if row.first_sent_at is None:
        row.first_sent_at = sent_at
        row.observed_until = sent_at + timedelta(days=wait_days)
        row.updated_at = datetime.now(tz=UTC)
        await session.flush()
    return row


async def record_reply(
    session: AsyncSession,
    *,
    lead: Lead,
    label: ReplyLabel,
    confidence: float = 0.0,
    now: datetime | None = None,
) -> LeadOutcome | None:
    """Translate a classified reply into the lead's standing outcome.

    Returns None when the label implies no outcome — `question` and `other` mean a person has to
    decide, and inventing a result for them would manufacture training labels out of ambiguity.

    **A human's answer is not overwritten.** If somebody has already confirmed this lead's
    outcome, a later model classification is logged and dropped: the alternative is a reviewer's
    judgement being silently undone by a reply that arrived afterwards.
    """
    now = now or datetime.now(tz=UTC)
    outcome = outcome_for_label(label)
    if outcome is None:
        return None

    row = await _row_for(session, lead)
    if row.confirmed_by not in (None, SYSTEM):
        logger.info(
            "outcome already confirmed by a person; model classification not applied",
            extra={"lead_id": str(lead.id), "label": label.value},
        )
        return row

    _set(row, outcome=outcome, confidence=confidence, confirmed_by=SYSTEM, now=now)
    # A reply is a reply, whatever it said: the funnel advanced even when the answer was no.
    if row.stage == Stage.SENT.value:
        row.stage = Stage.REPLIED.value
    await session.flush()
    return row


async def confirm(
    session: AsyncSession,
    *,
    lead: Lead,
    outcome: Outcome,
    by: str,
    stage: Stage | None = None,
    now: datetime | None = None,
) -> LeadOutcome:
    """A person says what happened. Outranks anything a model decided.

    This is the only path to `MEETING_BOOKED`, which is a locked product rule: a warm reply is
    not a meeting, and a system that inferred one would inflate every funnel it ever reported.
    """
    now = now or datetime.now(tz=UTC)
    row = await _row_for(session, lead)
    _set(row, outcome=outcome, confidence=1.0, confirmed_by=by, now=now)
    if stage is not None:
        if stage not in stages_for_kind(row.campaign_kind):
            raise ValueError(f"{stage} is not a stage of a {row.campaign_kind} campaign")
        row.stage = stage.value
    await session.flush()
    return row


async def resolve_overdue(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 500
) -> int:
    """Mark leads that were watched for the whole window and never replied.

    Run from the `Scheduler`'s hourly pass. Returns how many were resolved.

    **The row this writes is censored, not negative**, and the check constraint makes that
    inseparable from the outcome value: `no_reply_after_window` and `censored` are true together
    or neither is. Anything reading the dataset can therefore tell "we asked and heard nothing"
    from "they said no" without trusting the writer to have been careful.
    """
    now = now or datetime.now(tz=UTC)
    rows = (
        await session.scalars(
            select(LeadOutcome)
            .where(
                LeadOutcome.outcome.is_(None),
                LeadOutcome.first_sent_at.is_not(None),
                LeadOutcome.observed_until.is_not(None),
                LeadOutcome.observed_until <= now,
            )
            .limit(limit)
        )
    ).all()

    resolved = 0
    for row in rows:
        assert row.first_sent_at is not None
        wait = max(0, (row.observed_until - row.first_sent_at).days) if row.observed_until else 0
        if resolve_silence(first_sent_at=row.first_sent_at, wait_days=wait, now=now) is None:
            continue
        _set(
            row,
            outcome=Outcome.NO_REPLY_AFTER_WINDOW,
            confidence=1.0,
            confirmed_by=SYSTEM,
            now=now,
        )
        resolved += 1

    if resolved:
        await session.flush()
        logger.info("silent leads resolved", extra={"count": resolved})
    return resolved


def _set(
    row: LeadOutcome, *, outcome: Outcome, confidence: float, confirmed_by: str, now: datetime
) -> None:
    """The one place `outcome` and `censored` are written, so they cannot disagree."""
    row.outcome = outcome.value
    row.censored = outcome.censored
    row.confidence = confidence
    row.confirmed_by = confirmed_by
    row.resolved_at = now
    row.updated_at = now


async def message_is_first_outbound(session: AsyncSession, *, message: Message) -> bool:
    """Whether this is the first thing we ever sent this lead.

    Asked so the clock starts once. Counted from `messages` rather than from a flag, for the
    reason `NEW-3` taught: a counter drifts, and the rows are the record.
    """
    if message.lead_id is None:
        return False
    earlier = await session.scalar(
        select(Message.id)
        .where(
            Message.lead_id == message.lead_id,
            Message.direction == "out",
            Message.id != message.id,
        )
        .limit(1)
    )
    return earlier is None


__all__ = [
    "DEFAULT_WAIT_DAYS",
    "confirm",
    "message_is_first_outbound",
    "record_first_send",
    "record_reply",
    "resolve_overdue",
]
