"""The outbox: claim, send, mark — in that order, never any other.

`ARCHITECTURE_TARGET.md` §9.5. SMTP cannot give exactly-once delivery, so we choose
**at-most-once** deliberately: a duplicate cold email is worse than a missed one, and a missed one
a human can resend.

The shape:

1. **Claim** (this transaction): lock the lead, re-check eligibility, insert a `messages` row with
   `status='sending'`, a pre-generated `Message-ID` and a UNIQUE `idempotency_key`. Commit.
2. **Send** over SMTP using that exact `Message-ID`. Not this module's job — `app.worker.send_loop`
   owns it, and it is the only code that talks to SMTP.
3. **Mark** (a second transaction): `sent`, `failed` or `unknown`, plus the lead's transition.

Because the row exists *before* the send, a crash between sending and marking cannot produce a
second send: the next attempt finds the row and resolves it rather than starting over. The
prototype sent first and wrote the database afterwards, so a crash in between resent on the next
run (issue B12).

A row left in `sending` is the one genuinely ambiguous case. It is never auto-retried — it becomes
`unknown` and waits for a person, because we cannot tell whether the mail left.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Mailbox, Message, Suppression
from app.db.state import lock_lead, record_event, transition
from app.domain.state import LeadState

#: Outbound message kinds. Each maps to the lead states it may legitimately be sent from.
KIND_INITIAL = "initial"
KIND_FOLLOWUP = "followup"
KIND_REPLY = "reply"

#: A send of this kind is only valid when the lead is in one of these states. Checked at claim
#: time *and* again before the send, because a reply may have arrived in between.
_VALID_FROM: dict[str, frozenset[LeadState]] = {
    KIND_INITIAL: frozenset({LeadState.APPROVED}),
    KIND_FOLLOWUP: frozenset({LeadState.SENT}),
    KIND_REPLY: frozenset({LeadState.REPLIED}),
}

#: How long a `sending` row may sit before a human has to resolve it.
STUCK_AFTER = timedelta(minutes=10)


@dataclass(frozen=True)
class ClaimResult:
    """Either a claimed message, or why nothing was claimed.

    A refusal is normal operation, not an error: a suppressed recipient or a lead that replied
    while we were queuing is exactly what these checks are for.
    """

    message: Message | None
    reason: str | None = None

    @property
    def claimed(self) -> bool:
        return self.message is not None


def build_message_id(from_email: str) -> str:
    """An RFC 5322 Message-ID, generated before the send so it can be matched afterwards.

    The domain comes from the sending address, so the identifier is plausibly ours — which
    matters when searching the mailbox to resolve a `sending` row (ADR-002).
    """
    domain = from_email.rpartition("@")[2] or "localhost"
    return f"<{uuid.uuid4()}@{domain}>"


async def _next_sequence(session: AsyncSession, lead_id: uuid.UUID, kind: str) -> int:
    """How many messages of this kind we have already claimed for this lead, plus one.

    Two concurrent claims can compute the same number. That is the point: the second insert
    violates the unique index on `idempotency_key` and is refused, so the race ends in one
    message rather than two.
    """
    count = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.lead_id == lead_id, Message.kind == kind, Message.direction == "out")
    )
    return int(count or 0) + 1


async def is_suppressed(session: AsyncSession, *, org_id: uuid.UUID, email: str) -> bool:
    """True if this address, or its whole domain, must never be contacted.

    Checked here as well as at import, because a suppression can arrive at any time — someone
    may opt out between a draft being approved and the send loop reaching it.
    """
    normalised = email.strip().lower()
    domain = normalised.rpartition("@")[2]
    found = await session.scalar(
        select(Suppression.id)
        .where(
            Suppression.org_id == org_id,
            or_(
                (Suppression.kind == "email") & (Suppression.value == normalised),
                (Suppression.kind == "domain") & (Suppression.value == domain),
            ),
        )
        .limit(1)
    )
    return found is not None


async def claim_send(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead_id: uuid.UUID,
    kind: str,
    mailbox_id: uuid.UUID | None = None,
    draft_id: uuid.UUID | None = None,
    subject: str | None = None,
    body_text: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    now: datetime | None = None,
) -> ClaimResult:
    """Reserve the right to send one email, or explain why we must not.

    On success the returned `Message` is `status='sending'` and carries the `Message-ID` the SMTP
    send must use. The caller commits, sends, then calls `mark_sent` or `mark_failed`.

    Scheduling eligibility — the IST window, the daily cap, warm-up and pacing — is **not**
    checked here. That belongs to the send loop (Phase 5), which owns the clock; this function
    covers the checks that are purely about stored state.
    """
    if kind not in _VALID_FROM:
        raise ValueError(f"unknown message kind {kind!r}")

    now = now or datetime.now(tz=UTC)
    lead = await lock_lead(session, org_id, lead_id)

    if lead.paused:
        return ClaimResult(None, "lead is paused")

    state = LeadState(lead.state)
    if state not in _VALID_FROM[kind]:
        # The common real case: a reply landed while this send was queued.
        return ClaimResult(None, f"lead is {state.value}, not valid for a {kind} send")

    campaign = await session.get(Campaign, lead.campaign_id)
    if campaign is None:
        return ClaimResult(None, "campaign is missing")
    if campaign.paused:
        return ClaimResult(None, "campaign is paused")

    mailbox_id = mailbox_id or campaign.mailbox_id
    if mailbox_id is None:
        return ClaimResult(None, "campaign has no mailbox")
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return ClaimResult(None, "mailbox is missing")
    if mailbox.paused:
        return ClaimResult(None, f"mailbox is paused ({mailbox.pause_reason or 'no reason'})")

    if await is_suppressed(session, org_id=org_id, email=lead.email_norm):
        return ClaimResult(None, "recipient is suppressed")

    # Refuse while an earlier send of this kind is still unresolved.
    #
    # This is load-bearing, and the unique key alone does not cover it. Two concurrent claims are
    # serialised by the lead lock, so the second one runs *after* the first has committed its
    # `sending` row — it then counts that row, computes the next sequence number, and gets a
    # perfectly valid distinct key. Both sends would go out. The lead's own state does not help
    # either, because a claim deliberately does not move it.
    #
    # Repeat sends of the same kind over time are still possible, and are bounded by the state
    # machine: after `mark_sent` an initial leaves APPROVED, a follow-up leaves SENT and a reply
    # leaves REPLIED, so none of them is claimable again from the same state.
    unresolved = await session.scalar(
        select(Message.id)
        .where(
            Message.lead_id == lead_id,
            Message.kind == kind,
            Message.direction == "out",
            Message.status == "sending",
        )
        .limit(1)
    )
    if unresolved is not None:
        return ClaimResult(None, f"a {kind} send is already in flight for this lead")

    sequence = await _next_sequence(session, lead_id, kind)
    message = Message(
        org_id=org_id,
        lead_id=lead_id,
        mailbox_id=mailbox_id,
        draft_id=draft_id,
        direction="out",
        kind=kind,
        status="sending",
        message_id_hdr=build_message_id(mailbox.from_email),
        in_reply_to=in_reply_to,
        references_hdr=references,
        subject=subject,
        body_text=body_text,
        from_addr=mailbox.from_email,
        to_addr=lead.email_norm,
        idempotency_key=f"{lead_id}:{kind}:{sequence}",
    )
    session.add(message)

    try:
        await session.flush()
    except IntegrityError:
        # Another worker claimed this exact send first. Not an error — the unique index did its
        # job, and this is the duplicate it was there to stop.
        await session.rollback()
        return ClaimResult(None, "already claimed by another worker")

    await record_event(
        session,
        org_id=org_id,
        campaign_id=lead.campaign_id,
        lead_id=lead_id,
        type="message.claimed",
        data={"kind": kind, "message_id": message.message_id_hdr},
    )
    return ClaimResult(message)


async def mark_sent(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    smtp_response: str | None = None,
    next_action_at: datetime | None = None,
    now: datetime | None = None,
) -> Message:
    """Record that the mail left, and move the lead on.

    The lead transition happens here rather than at claim time, so a lead is only ever `SENT`
    because something was actually sent.
    """
    now = now or datetime.now(tz=UTC)
    message = await _locked_message(session, org_id, message_id)

    message.status = "sent"
    message.smtp_response = (smtp_response or "")[:1000] or None
    message.sent_at = now

    to_state = {
        KIND_INITIAL: LeadState.SENT,
        KIND_FOLLOWUP: LeadState.FOLLOWUP_SENT,
        KIND_REPLY: LeadState.IN_CONVERSATION,
    }[str(message.kind)]

    lead = await transition(
        session,
        org_id=org_id,
        lead_id=message.lead_id,
        to_state=to_state,
        data={"kind": message.kind, "message_id": message.message_id_hdr},
        next_action_at=next_action_at,
        now=now,
    )
    lead.last_message_at = now
    if message.kind == KIND_FOLLOWUP:
        lead.followups_sent += 1

    await session.flush()
    return message


async def mark_failed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    error: str,
    now: datetime | None = None,
) -> Message:
    """Record that SMTP refused the mail. The lead keeps its state so a retry is possible."""
    message = await _locked_message(session, org_id, message_id)
    message.status = "failed"
    message.smtp_response = error[:1000]

    await record_event(
        session,
        org_id=org_id,
        lead_id=message.lead_id,
        type="message.failed",
        data={"kind": message.kind, "error": error[:500]},
    )
    await session.flush()
    return message


async def mark_unknown(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    note: str,
    now: datetime | None = None,
) -> Message:
    """Record that we cannot tell whether the mail left. **Never auto-retried.**

    Reached when a worker died between the SMTP call and the mark. A human decides: confirm it
    was sent, or send it again. Guessing either way is worse — an automatic retry risks a
    duplicate cold email, and silently writing it off risks a lead who was contacted but looks
    untouched.
    """
    message = await _locked_message(session, org_id, message_id)
    message.status = "unknown"
    message.smtp_response = note[:1000]

    await record_event(
        session,
        org_id=org_id,
        lead_id=message.lead_id,
        type="message.unknown",
        data={"kind": message.kind, "note": note[:500], "needs_human": True},
    )
    await session.flush()
    return message


async def find_stuck_sending(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    older_than: timedelta = STUCK_AFTER,
    now: datetime | None = None,
) -> list[Message]:
    """Messages left mid-send, for the recovery pass to resolve."""
    now = now or datetime.now(tz=UTC)
    stmt = select(Message).where(
        Message.direction == "out",
        Message.status == "sending",
        Message.created_at < now - older_than,
    )
    if org_id is not None:
        stmt = stmt.where(Message.org_id == org_id)
    result = await session.execute(stmt.order_by(Message.created_at))
    return list(result.scalars().all())


async def _locked_message(
    session: AsyncSession, org_id: uuid.UUID, message_id: uuid.UUID
) -> Message:
    result = await session.execute(
        select(Message).where(Message.id == message_id, Message.org_id == org_id).with_for_update()
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise LookupError(f"message {message_id} not found in this org")
    return message
