"""What happens when a verified booking arrives (F15, ADR-045).

**This is the second producer of `MEETING_BOOKED`, and the only one that is not a person.** The
locked product rule is that the state is never *inferred* -- a warm reply is not a meeting, and a
system that guessed would inflate every funnel it ever reported. A booking webhook is not an
inference: a third party is telling us a calendar slot was taken, signed with a secret this
workspace generated and pasted in itself.

Three things this deliberately does not do:

- **A cancellation does not move the lead back.** `MEETING_BOOKED` is terminal, with no exits in
  the state machine by construction. The cancellation is recorded on the booking row and as an
  event, and the lead stays where it is -- because a meeting that was booked *was* booked, and
  because widening a terminal state is a change to a locked product rule and not a side effect of
  adding a webhook.
- **It never creates a lead.** A booking from somebody who is in no campaign is stored unmatched
  and shown. Creating a lead would put a person into a sending pipeline because they booked a
  meeting, which is the opposite of what they asked for.
- **It never sends anything.** Nothing here imports the outbox, and a test asserts that over the
  package's source.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BookingEvent, Campaign, Lead, Message
from app.db.repo import OrgContext
from app.db.state import record_event, transition
from app.domain.outcome import Outcome, Stage
from app.domain.state import IllegalTransition, LeadState
from app.services import outcomes
from app.services.booking.payload import Booking

logger = logging.getLogger(__name__)

BOOKED_EVENT = "lead.meeting_booked"
UNMATCHED_EVENT = "booking.unmatched"
CANCELLED_EVENT = "booking.cancelled"

#: Which funnel stage a booked call is, per campaign kind. A booked call is a `meeting` when we
#: are selling and a `screen` when we are applying for a job; `outcomes.confirm` refuses a stage
#: that is not in the kind's own sequence, so one hard-coded value would raise for every job-hunt
#: campaign. An unknown kind sets the outcome and leaves the stage alone, which is the honest
#: answer rather than a guess at somebody else's funnel.
_MEETING_STAGE: dict[str, Stage] = {"outreach": Stage.MEETING, "job_hunt": Stage.SCREEN}


def _stage_for(campaign_kind: str) -> Stage | None:
    return _MEETING_STAGE.get(campaign_kind)


@dataclass(frozen=True)
class IntakeResult:
    booking_id: uuid.UUID
    lead_id: uuid.UUID | None
    matched_by: str
    #: False when this delivery was a retry of one already recorded. Reported rather than hidden,
    #: because "we already had this" and "we accepted this" are different answers and a provider
    #: dashboard shows both as a 200.
    created: bool
    state_changed: bool


async def _match(
    session: AsyncSession, ctx: OrgContext, booking: Booking
) -> tuple[Lead | None, str]:
    """Which lead this booking belongs to, and why -- in words.

    Matched on the address, which is the only thing a booking payload and a lead reliably share.
    When the same person is in several campaigns -- the ordinary case in this product, and the
    reason the per-person cool-down exists -- the one we are **actually in conversation with**
    wins, decided by the most recent outbound message. Taking the oldest or newest *lead* instead
    would attribute the meeting to a campaign that never wrote to them.
    """
    if not booking.invitee_email:
        return None, "unmatched"

    rows = list(
        (
            await session.scalars(
                select(Lead).where(
                    Lead.org_id == ctx.org_id, Lead.email_norm == booking.invitee_email
                )
            )
        ).all()
    )
    if not rows:
        return None, "unmatched"
    if len(rows) == 1:
        return rows[0], "email"

    latest = await session.scalar(
        select(Message.lead_id)
        .where(
            Message.org_id == ctx.org_id,
            Message.lead_id.in_([row.id for row in rows]),
            Message.direction == "out",
            Message.sent_at.is_not(None),
        )
        .order_by(desc(Message.sent_at))
        .limit(1)
    )
    if latest is not None:
        for row in rows:
            if row.id == latest:
                return row, "in_conversation"
    # Several leads, none ever emailed. The address is still a match; which campaign it belongs
    # to is genuinely unknown, so the oldest is taken and the reason says only "email".
    return sorted(rows, key=lambda row: row.created_at)[0], "email"


async def record(
    session: AsyncSession,
    ctx: OrgContext,
    booking: Booking,
    *,
    now: datetime | None = None,
) -> IntakeResult:
    """Store a verified booking, and move the lead if it is a new one."""
    now = now or datetime.now(tz=UTC)

    existing = await session.scalar(
        select(BookingEvent).where(
            BookingEvent.org_id == ctx.org_id,
            BookingEvent.provider == booking.provider,
            BookingEvent.external_id == booking.external_id,
        )
    )
    if existing is not None:
        # A retry. Providers retry on any non-2xx and some retry on 2xx as well, so this is the
        # ordinary path rather than an edge case -- and the unique index is what makes it safe
        # whichever of the two concurrent deliveries gets here first.
        return IntakeResult(
            booking_id=existing.id,
            lead_id=existing.lead_id,
            matched_by=existing.matched_by,
            created=False,
            state_changed=False,
        )

    lead, matched_by = await _match(session, ctx, booking)
    row = BookingEvent(
        org_id=ctx.org_id,
        lead_id=lead.id if lead is not None else None,
        provider=booking.provider,
        external_id=booking.external_id,
        kind=booking.kind,
        invitee_email_norm=booking.invitee_email,
        invitee_name=booking.invitee_name,
        scheduled_at=booking.scheduled_at,
        matched_by=matched_by,
        received_at=now,
    )
    session.add(row)
    await session.flush()

    if lead is None:
        # Recorded as an event too, because an unmatched booking is something somebody should
        # look at: usually the lead replied from a second address, or forwarded the link.
        await record_event(
            session,
            org_id=ctx.org_id,
            type=UNMATCHED_EVENT,
            actor="system",
            actor_id="booking",
            data={"provider": booking.provider, "booking_id": str(row.id)},
        )
        logger.info("a booking matched no lead", extra={"provider": booking.provider})
        return IntakeResult(
            booking_id=row.id,
            lead_id=None,
            matched_by=matched_by,
            created=True,
            state_changed=False,
        )

    if booking.kind != "booked":
        # Recorded against the lead and nothing else. See the module docstring: moving a lead out
        # of `MEETING_BOOKED` is a change to a locked product rule, not a webhook's business.
        await record_event(
            session,
            org_id=ctx.org_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            type=CANCELLED_EVENT if booking.kind == "cancelled" else "booking.rescheduled",
            actor="system",
            actor_id="booking",
            data={"provider": booking.provider, "kind": booking.kind},
        )
        return IntakeResult(
            booking_id=row.id,
            lead_id=lead.id,
            matched_by=matched_by,
            created=True,
            state_changed=False,
        )

    campaign = await session.get(Campaign, lead.campaign_id)
    campaign_kind = getattr(campaign, "kind", None) or "outreach"

    # **The state machine is the only authority on where a lead may be.** A list here of
    # "states a booking may move" was written first and deleted: it was a second copy of
    # `_ALLOWED` and the first thing it did was disagree with it, claiming `NEW` was bookable.
    # Attempting the move and catching the refusal keeps one authority -- and it is how the
    # missing `SENT -> MEETING_BOOKED` edge was found rather than papered over.
    state_changed = False
    try:
        await transition(
            session,
            org_id=ctx.org_id,
            lead_id=lead.id,
            to_state=LeadState.MEETING_BOOKED,
            actor="system",
            actor_id="booking",
            data={"provider": booking.provider, "booking_id": str(row.id)},
        )
    except IllegalTransition:
        # A lead that has opted out, hard-bounced or already booked is refused here, by the one
        # table that describes the lifecycle -- and the booking row is stored either way, which
        # is the half somebody can act on. An opted-out lead has not booked a meeting with us,
        # and a webhook that could move them would be a way to resurrect a suppressed lead from
        # outside the system.
        logger.info(
            "a booking could not move the lead",
            extra={"lead_id": str(lead.id), "state": lead.state},
        )
    else:
        state_changed = True

    if state_changed:
        await outcomes.confirm(
            session,
            lead=lead,
            outcome=Outcome.MEETING_BOOKED,
            # Not a person, and the dataset must not record it as one (ADR-026). `confirm` is
            # the only path to this outcome, and what it needs is who said so.
            by=f"booking:{booking.provider}",
            # **The stage is per campaign kind.** A booked call is `meeting` in an outreach
            # funnel and `screen` in a job hunt, and `confirm` refuses a stage that is not in
            # the kind's sequence -- so a hard-coded `Stage.MEETING` would raise for every
            # job-hunt campaign. The kind is read from the row rather than guessed.
            stage=_stage_for(campaign_kind),
        )
        await record_event(
            session,
            org_id=ctx.org_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            type=BOOKED_EVENT,
            actor="system",
            actor_id="booking",
            data={
                "provider": booking.provider,
                "booking_id": str(row.id),
                "matched_by": matched_by,
                "scheduled_at": (
                    booking.scheduled_at.isoformat() if booking.scheduled_at else None
                ),
            },
        )

    return IntakeResult(
        booking_id=row.id,
        lead_id=lead.id,
        matched_by=matched_by,
        created=True,
        state_changed=state_changed,
    )
