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

from app.db.models import Campaign, Lead, Mailbox, Message, Suppression
from app.db.state import lock_lead, record_event, transition
from app.domain.state import LeadState
from app.services.mail.message import build_references

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


#: How recently we must have written to somebody for a *second* cold approach to be refused.
#: Not a suppression: the person said nothing wrong, we simply already have a conversation open
#: with them. Ninety days is long enough that a second pitch reads as a fresh one rather than as
#: the same agency forgetting it already wrote.
CONTACT_COOLDOWN_DAYS = 90

#: Lead states that mean a conversation with this person is live somewhere. A second campaign
#: must not cold-open while one of these is in progress, whatever the cool-down says.
_IN_CONVERSATION: frozenset[str] = frozenset(
    {
        LeadState.REPLIED.value,
        LeadState.IN_CONVERSATION.value,
        LeadState.MEETING_BOOKED.value,
        LeadState.SENT.value,
        LeadState.FOLLOWUP_SENT.value,
    }
)


async def _already_in_touch(
    session: AsyncSession, org_id: uuid.UUID, lead: Lead, now: datetime | None = None
) -> str | None:
    """Refuse a first email to somebody this org is already talking to, as a different lead.

    **`UNIQUE(campaign_id, email_norm)` is per campaign, on purpose** -- the same person may
    legitimately appear in a later campaign, and that was a deliberate fix for the prototype's
    global unique key, which made re-importing a corrected sheet silently do nothing. The cost
    of that choice is this: two campaigns can hold the same person, and if both run, both send.
    The recipient gets two cold emails from the same agency, and if they reply to one, the other
    campaign sends anyway because it is looking at its own lead row and that row knows nothing.

    So the check is **per person per org**, not per lead:

    - somebody we emailed within `CONTACT_COOLDOWN_DAYS` is not cold-approached again;
    - somebody with a live conversation anywhere in the org is never cold-approached, whatever
      the date says -- that is the case where a reply arrives mid-switch, and answering a reply
      in one campaign while opening a pitch in another is the worst version of this.

    Only first emails are checked. A follow-up or a reply belongs to a thread that is already
    open with that person, and blocking those would break the conversation this exists to
    protect.
    """
    now = now or datetime.now(tz=UTC)

    sibling = (
        await session.execute(
            select(Lead.id, Lead.state, Lead.campaign_id)
            .where(
                Lead.org_id == org_id,
                Lead.email_norm == lead.email_norm,
                Lead.id != lead.id,
                Lead.state.in_(_IN_CONVERSATION),
            )
            .limit(1)
        )
    ).first()
    if sibling is not None:
        return (
            f"another campaign is already in touch with this person (lead is {sibling.state} there)"
        )

    recent = await session.scalar(
        select(Message.sent_at)
        .join(Lead, Lead.id == Message.lead_id)
        .where(
            Message.org_id == org_id,
            Message.direction == "out",
            Message.status.in_(("sent", "sending")),
            Lead.email_norm == lead.email_norm,
            Lead.id != lead.id,
            Message.sent_at.is_not(None),
            Message.sent_at > now - timedelta(days=CONTACT_COOLDOWN_DAYS),
        )
        .order_by(Message.sent_at.desc())
        .limit(1)
    )
    if recent is not None:
        return (
            f"we emailed this person on {recent.date().isoformat()} from another campaign; "
            f"a second approach waits {CONTACT_COOLDOWN_DAYS} days"
        )
    return None


async def thread_headers(
    session: AsyncSession, *, lead_id: uuid.UUID, kind: str
) -> tuple[str | None, str | None]:
    """`In-Reply-To` and `References` for the message about to be claimed.

    **Every outbound message went out with neither**, so a follow-up arrived as a brand new
    conversation rather than under the email it was following up, and our answer to somebody's
    reply did the same. `build_message` has always accepted both headers and `build_references`
    has always known how to extend a chain; the send loop simply never passed anything. A
    correct producer and a correct consumer with nothing connecting them -- the same shape as
    every other defect this project has produced.

    What each kind threads onto:

    - a **follow-up** continues our own last message to this lead, which is the first email or
      the previous follow-up;
    - a **reply** answers their last inbound message;
    - a **first email** starts the thread and threads onto nothing.

    `References` extends the parent's chain rather than restarting it, so the third message in a
    conversation still names the first -- which is what clients use to group a thread when an
    intermediate message is missing from somebody's mailbox.
    """
    if kind == KIND_INITIAL:
        return None, None

    direction = "in" if kind == KIND_REPLY else "out"
    parent = await session.scalar(
        select(Message)
        .where(
            Message.lead_id == lead_id,
            Message.direction == direction,
            Message.message_id_hdr.is_not(None),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if parent is None or not parent.message_id_hdr:
        # Nothing to thread onto. Sending anyway is right -- a follow-up with no visible parent
        # is still a useful email -- and it is what happened before this existed.
        return None, None

    return parent.message_id_hdr, build_references(parent.references_hdr, parent.message_id_hdr)


async def is_suppressed(session: AsyncSession, *, org_id: uuid.UUID, email: str) -> bool:
    """True if this address, or its whole domain, must never be contacted.

    Checked here as well as at import, because a suppression can arrive at any time — someone
    may opt out between a draft being approved and the send loop reaching it.
    """
    from app.services.privacy.erasure import EMAIL_HASH, hashed_email

    normalised = email.strip().lower()
    domain = normalised.rpartition("@")[2]
    found = await session.scalar(
        select(Suppression.id)
        .where(
            Suppression.org_id == org_id,
            or_(
                (Suppression.kind == "email") & (Suppression.value == normalised),
                (Suppression.kind == "domain") & (Suppression.value == domain),
                # An erased lead leaves only a digest behind. Without this arm, erasing somebody
                # who had opted out would make them contactable again on the next import —
                # turning a privacy request into the harm it was meant to prevent.
                (Suppression.kind == EMAIL_HASH) & (Suppression.value == hashed_email(email)),
            ),
        )
        .limit(1)
    )
    return found is not None


async def why_not_sendable(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    kind: str,
    mailbox_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> str | None:
    """Why this lead cannot be sent a `kind` email right now, or `None` if it can.

    **This exists because the answer was invisible.** Every refusal here was already being
    computed, once per cycle, inside `claim_send` -- and the reason went into a `ClaimResult`
    that the send loop logged and nobody ever read. A lead sat `APPROVED` for eight days showing
    "next action: —", which looks exactly like "waiting its turn", while the real answer was that
    another campaign in the same org was already in conversation with that person. No screen
    anywhere could have said so.

    **`claim_send` calls this**; it is not a second copy of the same rules. A separate
    implementation for the dashboard would be one that drifts, and this project's dominant defect
    -- six times over -- is two individually correct pieces of code with nothing checking that
    they agree. The send loop's refusal and the "why is this stuck?" panel are now the same
    sentence by construction.

    Read-only: no lock, no writes. A dashboard asking about fifty leads cannot interfere with the
    loop actually sending one. Scheduling -- the window, the cap, warm-up, pacing -- is **not**
    here, because it belongs to the send loop which owns the clock; `send_loop.blocking_reason`
    covers that half.
    """
    now = now or datetime.now(tz=UTC)

    if kind not in _VALID_FROM:
        raise ValueError(f"unknown message kind {kind!r}")

    if lead.paused:
        return "this lead is paused"

    state = LeadState(lead.state)
    if state not in _VALID_FROM[kind]:
        return f"the lead is {state.value}, and a {kind} email is not sent from that state"

    campaign = await session.get(Campaign, lead.campaign_id)
    if campaign is None:
        return "the campaign is missing"
    if campaign.paused:
        return "the campaign is paused"

    mailbox_id = mailbox_id or campaign.mailbox_id
    if mailbox_id is None:
        return "the campaign has no mailbox attached"
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None:
        return "the campaign's mailbox is missing"
    if mailbox.paused:
        return f"the mailbox is paused ({mailbox.pause_reason or 'no reason given'})"

    if await is_suppressed(session, org_id=org_id, email=lead.email_norm):
        return "this address is on the suppression list"

    if kind == KIND_INITIAL and (clash := await _already_in_touch(session, org_id, lead, now)):
        return clash

    unresolved = await session.scalar(
        select(Message.id)
        .where(
            Message.lead_id == lead.id,
            Message.kind == kind,
            Message.direction == "out",
            Message.status == "sending",
        )
        .limit(1)
    )
    if unresolved is not None:
        return f"a {kind} send is already in flight for this lead"

    return None


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
    # The lock first, so the checks below run against state nobody else can change underneath
    # them. `why_not_sendable` deliberately does not lock -- it is also called by the dashboard,
    # which must not be able to interfere with a send in progress.
    lead = await lock_lead(session, org_id, lead_id)

    # **Every stored-state refusal lives in one function**, which the dashboard asks too. The
    # list it walks is: lead paused, lead in the wrong state for this kind (the common real case
    # being a reply that landed while this send was queued), campaign missing or paused, no
    # mailbox, mailbox missing or paused, recipient suppressed, somebody else in the org already
    # in conversation with this person, and -- load-bearing, and not covered by the unique key --
    # an earlier send of this kind still unresolved.
    #
    # That last one matters because two concurrent claims are serialised by the lead lock, so the
    # second runs *after* the first has committed its `sending` row; it would then count that
    # row, compute the next sequence number, get a perfectly valid distinct key, and send a
    # second copy. The lead's own state does not help, because a claim deliberately does not move
    # it. Repeat sends over time are bounded by the state machine instead: after `mark_sent` an
    # initial leaves APPROVED, a follow-up leaves SENT and a reply leaves REPLIED.
    if refusal := await why_not_sendable(
        session, org_id=org_id, lead=lead, kind=kind, mailbox_id=mailbox_id, now=now
    ):
        return ClaimResult(None, refusal)

    campaign = await session.get(Campaign, lead.campaign_id)
    assert campaign is not None, "why_not_sendable returned None, so the campaign exists"
    mailbox_id = mailbox_id or campaign.mailbox_id
    mailbox = await session.get(Mailbox, mailbox_id)
    assert mailbox is not None, "why_not_sendable returned None, so the mailbox exists"

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
        # Explicit rather than the server default, so every timestamp on the send path comes
        # from one clock. The daily cap is counted from these rows, and a count that mixed the
        # database's clock with the caller's could not be reasoned about.
        created_at=now,
    )
    session.add(message)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # **Only the idempotency key means "somebody else got there first".** Catching every
        # integrity error and calling it that sends whoever reads the reason looking for a
        # concurrent worker that does not exist -- a foreign key violation on `draft_id`
        # reported as "already claimed by another worker" is a wrong answer, not a vague one.
        if "idempotency" in str(getattr(exc, "orig", exc)).lower():
            # The unique index did its job, and this is the duplicate it was there to stop.
            return ClaimResult(None, "already claimed by another worker")
        raise

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

    # `messages.lead_id` is nullable so that inbound mail matching no lead can still be stored
    # (§10.2), and `ck_messages_out_has_lead` keeps every outbound row attached to one. If that
    # ever fails to hold, stopping here is right: the alternative is a sent message whose lead
    # never leaves `APPROVED` and is therefore eligible to be sent again.
    if message.lead_id is None:
        raise ValueError(f"outbound message {message_id} has no lead; refusing to record the send")

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
