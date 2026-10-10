"""Acting on an inbound message: match it, record what it is, then act.

`ARCHITECTURE_TARGET.md` §10.2 to §10.4. The order is the design, and each step exists because
the prototype got it wrong:

1. **Match by threading headers**, not by sender address. The prototype matched on `From` alone,
   so any unrelated mail from that person counted as "the reply", and a reply from an assistant
   or a different alias was missed entirely (issue B11).
2. **Store the message**, so what arrived is on the record regardless of what we decide about it.
3. **Persist the classification before acting.** The prototype classified inside the handler, so
   a crash or retry re-ran the model and could reach a different answer, with no trace of the
   first (issue B17).
4. **Act according to the label's policy** — and no policy sends. A reply is drafted for a human
   to approve (issues B4, B5, C5).

Matching deliberately ignores the lead's state, so a late reply to a lead already written off as
`NO_RESPONSE` is still found (issue A6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import queue
from app.db.models import Classification, Lead, Message
from app.db.repo import OrgContext, add_suppression
from app.db.state import record_event, transition
from app.domain.campaign import CampaignSettings
from app.domain.reply import ReplyAction, ReplyLabel, policy_for, reconcile
from app.domain.scheduling import next_window_open
from app.domain.state import TERMINAL, IllegalTransition, LeadState
from app.services.mail.parse import MessageType, ParsedMessage, normalised_subject

#: How far back the sender+subject fallback will look. Beyond this a coincidental match is more
#: likely than a real one.
FALLBACK_WINDOW_DAYS = 30

#: Where to push a follow-up when someone is out of office and gave no return date.
DEFAULT_OOO_DEFER_DAYS = 5

#: Jobs to cancel the moment a reply, bounce or opt-out lands.
CANCEL_ON_REPLY = (queue.JobKind.SEND_MESSAGE, queue.JobKind.DRAFT_LEAD)


@dataclass
class ReplyOutcome:
    """What happened to one inbound message."""

    matched_lead_id: uuid.UUID | None = None
    message_row_id: uuid.UUID | None = None
    label: ReplyLabel | None = None
    action: ReplyAction | None = None
    reasons: list[str] = field(default_factory=list)
    #: True when this message had already been processed.
    duplicate: bool = False
    #: True when nothing could be matched; the message is stored as an orphan.
    orphan: bool = False


async def already_stored(
    session: AsyncSession, *, org_id: uuid.UUID, message_id_hdr: str
) -> Message | None:
    """Has this exact message been seen before?

    The deduplication that makes a `UIDVALIDITY` rescan safe: replaying two weeks of mail is
    harmless because every message is recognised by its own `Message-ID`.
    """
    if not message_id_hdr:
        return None
    return await session.scalar(
        select(Message).where(
            Message.org_id == org_id,
            Message.direction == "in",
            Message.message_id_hdr == message_id_hdr,
        )
    )


async def match_lead(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    parsed: ParsedMessage,
    now: datetime | None = None,
) -> tuple[Lead | None, str]:
    """Find the lead this message belongs to. Returns (lead, how it was matched).

    Three strategies, strongest first:

    1. **Threading headers against our own stored `Message-ID`s.** Exact: this message claims to
       answer a specific email we sent.
    2. **For a bounce, the `Final-Recipient`** from the DSN, which names the address that failed
       even when the threading headers are lost in the wrapper.
    3. **Sender plus normalised subject**, within 30 days. A fallback, used only when the first
       two fail, and bounded because beyond a month a coincidence is likelier than a real match.
    """
    now = now or datetime.now(tz=UTC)

    # 1. Threading.
    for candidate_id in parsed.threading_ids:
        ours = await session.scalar(
            select(Message).where(
                Message.org_id == org_id,
                Message.direction == "out",
                Message.message_id_hdr == candidate_id,
            )
        )
        if ours is not None:
            lead = await session.get(Lead, ours.lead_id)
            if lead is not None and lead.org_id == org_id:
                return lead, f"threading header matched our message {candidate_id}"

    # 2. A bounce names the address that failed.
    if parsed.is_bounce and parsed.failed_recipient:
        lead = await session.scalar(
            select(Lead)
            .where(Lead.org_id == org_id, Lead.email_norm == parsed.failed_recipient.lower())
            .order_by(Lead.created_at.desc())
        )
        if lead is not None:
            return lead, f"DSN Final-Recipient matched {parsed.failed_recipient}"

    # 3. Sender plus subject, bounded.
    if parsed.from_addr:
        subject_key = normalised_subject(parsed.subject)
        cutoff = now - timedelta(days=FALLBACK_WINDOW_DAYS)
        candidates = (
            await session.execute(
                select(Lead, Message)
                .join(Message, Message.lead_id == Lead.id)
                .where(
                    Lead.org_id == org_id,
                    Lead.email_norm == parsed.from_addr,
                    Message.direction == "out",
                    Message.created_at >= cutoff,
                )
                .order_by(Message.created_at.desc())
            )
        ).all()
        for lead, ours in candidates:
            if subject_key and normalised_subject(ours.subject or "") == subject_key:
                return lead, "sender and subject matched within 30 days"
        if candidates:
            # Same person, different subject. Still almost certainly them, but say so plainly
            # so the weaker basis is visible in the audit trail.
            return candidates[0][0], "sender matched within 30 days (subject did not)"

    return None, "no lead matched"


async def store_inbound(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    parsed: ParsedMessage,
    lead: Lead | None,
    folder: str,
    uid: int,
    now: datetime | None = None,
) -> Message:
    """Record the message itself, matched or not.

    An unmatched message is stored as an orphan rather than dropped: §10.2 is explicit that
    unmatched mail is never deleted and never marked read, because the alternative is silently
    losing somebody's reply.
    """
    now = now or datetime.now(tz=UTC)
    row = Message(
        org_id=org_id,
        lead_id=lead.id if lead is not None else None,
        direction="in",
        kind="reply",
        message_id_hdr=parsed.message_id or None,
        in_reply_to=parsed.in_reply_to or None,
        references_hdr=" ".join(parsed.references) or None,
        subject=parsed.subject or None,
        body_text=parsed.body_full or None,
        from_addr=parsed.from_addr or None,
        to_addr=parsed.to_addr or None,
        status="received",
        received_at=parsed.date or now,
        imap_uid=uid,
        imap_folder=folder,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def record_classification(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    message_row: Message,
    lead: Lead | None,
    label: ReplyLabel,
    reasons: list[str],
    confidence: float = 0.0,
    llm_entry: str | None = None,
    prompt_version: str | None = None,
) -> Classification:
    """Persist the decision *before* anything acts on it.

    B17's fix. With this written first, a crash between deciding and acting leaves a record of
    what was decided, and the retry acts on the same answer instead of asking the model again
    and possibly getting a different one.
    """
    row = Classification(
        org_id=org_id,
        message_id=message_row.id,
        lead_id=lead.id if lead is not None else None,
        label=label.value,
        confidence=confidence,
        rationale=reasons,
        llm_entry=llm_entry,
        prompt_version=prompt_version,
    )
    session.add(row)
    await session.flush()
    return row


async def handle_inbound(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    parsed: ParsedMessage,
    folder: str,
    uid: int,
    settings: CampaignSettings,
    model_label: ReplyLabel = ReplyLabel.OTHER,
    confidence: float = 0.0,
    llm_entry: str | None = None,
    prompt_version: str | None = None,
    now: datetime | None = None,
) -> ReplyOutcome:
    """Process one inbound message end to end.

    `model_label` comes from the classifier, which the caller runs. Passed in rather than called
    here so this function stays testable without a model, and so the keyword rules can be
    exercised on their own.
    """
    now = now or datetime.now(tz=UTC)

    existing = await already_stored(session, org_id=org_id, message_id_hdr=parsed.message_id)
    if existing is not None:
        return ReplyOutcome(
            matched_lead_id=existing.lead_id,
            message_row_id=existing.id,
            duplicate=True,
            reasons=["already processed"],
        )

    lead, how = await match_lead(session, org_id=org_id, parsed=parsed, now=now)
    message_row = await store_inbound(
        session, org_id=org_id, parsed=parsed, lead=lead, folder=folder, uid=uid, now=now
    )

    label, reasons = reconcile(
        model_label=model_label,
        body=parsed.body_reply_only,
        is_bounce=parsed.is_bounce,
        is_auto=parsed.message_type is MessageType.AUTO_REPLY,
    )
    reasons.insert(0, how)

    if lead is None:
        # Stored, never deleted, never marked read. A human can find it; we simply cannot act.
        await record_classification(
            session,
            org_id=org_id,
            message_row=message_row,
            lead=None,
            label=label,
            reasons=reasons,
            confidence=confidence,
            llm_entry=llm_entry,
            prompt_version=prompt_version,
        )
        await record_event(
            session,
            org_id=org_id,
            type="reply.orphan",
            data={"from": parsed.from_addr, "subject": parsed.subject[:200]},
        )
        return ReplyOutcome(
            message_row_id=message_row.id, label=label, reasons=reasons, orphan=True
        )

    classification = await record_classification(
        session,
        org_id=org_id,
        message_row=message_row,
        lead=lead,
        label=label,
        reasons=reasons,
        confidence=confidence,
        llm_entry=llm_entry,
        prompt_version=prompt_version,
    )

    policy = policy_for(label)
    await _act(
        session,
        org_id=org_id,
        lead=lead,
        parsed=parsed,
        label=label,
        action=policy.action,
        message_row=message_row,
        settings=settings,
        now=now,
    )

    classification.action = policy.action.value
    classification.acted_at = now
    await session.flush()

    return ReplyOutcome(
        matched_lead_id=lead.id,
        message_row_id=message_row.id,
        label=label,
        action=policy.action,
        reasons=reasons,
    )


async def _act(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    parsed: ParsedMessage,
    label: ReplyLabel,
    action: ReplyAction,
    message_row: Message,
    settings: CampaignSettings,
    now: datetime,
) -> None:
    """Carry out the label's policy. Note what no branch does: send."""
    lead.last_message_at = now

    if action is ReplyAction.SUPPRESS:
        # Suppression and the state change in one transaction: a crash between them would
        # leave an address we meant to stop contacting still contactable.
        reason = "opt_out" if label is ReplyLabel.OPT_OUT else "bounce"
        if label is ReplyLabel.BOUNCE and parsed.message_type is MessageType.BOUNCE_SOFT:
            # A full mailbox is not a reason to give up on somebody forever.
            await _defer(session, org_id=org_id, lead=lead, days=1, settings=settings, now=now)
            await record_event(
                session,
                org_id=org_id,
                lead_id=lead.id,
                type="reply.soft_bounce",
                data={"status": parsed.dsn_status},
            )
            return

        await add_suppression(
            OrgContext(org_id=org_id),
            session,
            value=lead.email_norm,
            reason=reason,
            source_message_id=message_row.id,
        )
        await queue.cancel_for_lead(
            session, org_id=org_id, lead_id=lead.id, kinds=list(CANCEL_ON_REPLY)
        )
        await _move(
            session,
            org_id=org_id,
            lead=lead,
            to_state=(LeadState.OPTED_OUT if label is ReplyLabel.OPT_OUT else LeadState.BOUNCED),
            data={"label": label.value, "status": parsed.dsn_status},
            now=now,
        )
        return

    if action is ReplyAction.CLOSE:
        await queue.cancel_for_lead(
            session, org_id=org_id, lead_id=lead.id, kinds=list(CANCEL_ON_REPLY)
        )
        await _move(
            session,
            org_id=org_id,
            lead=lead,
            to_state=LeadState.NOT_INTERESTED,
            data={"label": label.value},
            now=now,
        )
        return

    if action is ReplyAction.DEFER:
        # An autoresponder means nobody read the email, so the lead stays where it is and the
        # follow-up moves out. Treating it as engagement would cancel the follow-up for someone
        # who never saw the first email.
        days = DEFAULT_OOO_DEFER_DAYS
        if parsed.return_date is not None:
            delta = (parsed.return_date.date() - now.date()).days
            days = max(delta + 1, 1)
        await _defer(session, org_id=org_id, lead=lead, days=days, settings=settings, now=now)
        await record_event(
            session,
            org_id=org_id,
            lead_id=lead.id,
            type="reply.out_of_office",
            data={
                "defer_days": days,
                "return_date": (parsed.return_date.isoformat() if parsed.return_date else None),
            },
        )
        return

    # NEEDS_HUMAN. A person replies; we only prepare.
    await queue.cancel_for_lead(
        session, org_id=org_id, lead_id=lead.id, kinds=[queue.JobKind.SEND_MESSAGE]
    )
    await _move(
        session,
        org_id=org_id,
        lead=lead,
        to_state=LeadState.REPLIED,
        data={"label": label.value},
        clear_next_action=True,
        now=now,
    )
    # Drafting a proposed reply is a job, not something done inline: it needs an LLM call, and
    # the poller must not block on one.
    await queue.enqueue(
        session,
        org_id=org_id,
        kind=queue.JobKind.DRAFT_REPLY,
        lead_id=lead.id,
        payload={"message_id": str(message_row.id), "label": label.value},
        dedupe_key=f"draft_reply:{message_row.id}",
        priority=10,
    )


async def _move(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    to_state: LeadState,
    data: dict[str, Any],
    clear_next_action: bool = True,
    now: datetime,
) -> None:
    """Transition a lead, tolerating the cases where it cannot legally move.

    A reply to an already-`OPTED_OUT` or `BOUNCED` lead is real and must be recorded, but those
    states never reopen by design. So the message and classification are kept and the transition
    is skipped rather than raising — losing the record would be the worse outcome.
    """
    current = LeadState(lead.state)
    if current is to_state:
        return
    try:
        await transition(
            session,
            org_id=org_id,
            lead_id=lead.id,
            to_state=to_state,
            data=data,
            clear_next_action=clear_next_action,
            now=now,
        )
    except IllegalTransition:
        await record_event(
            session,
            org_id=org_id,
            lead_id=lead.id,
            type="reply.transition_skipped",
            data={
                **data,
                "from_state": current.value,
                "to_state": to_state.value,
                "why": "the state machine does not permit this move; the reply is still recorded",
            },
        )


async def _defer(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    days: int,
    settings: CampaignSettings,
    now: datetime,
) -> None:
    """Push the next action out, landing inside the send window."""
    target = now + timedelta(days=days)
    lead.next_action_at = next_window_open(target, settings)
    await session.flush()


async def is_terminal_lead(lead: Lead) -> bool:
    return LeadState(lead.state) in TERMINAL
