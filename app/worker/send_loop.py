"""The send loop: the only thing that causes mail to leave.

`ARCHITECTURE_TARGET.md` §9. Single-threaded and deliberately the only sender, so send rate is
controlled in exactly one place. Two workers both sending would make the daily cap a race.

The order within one send is the whole design:

1. **Re-check eligibility against the database**, immediately before sending. An approval might
   be hours old; a reply, bounce or opt-out may have arrived since.
2. **Claim** an outbox row with a pre-generated `Message-ID` and a unique idempotency key.
3. **Send**, through the one SMTP chokepoint.
4. **Mark**, in a second transaction.

A crash between 3 and 4 leaves a row in `sending`, which the recovery pass turns into `unknown`
for a human rather than resending. At-most-once is the deliberate choice (§9.5).

`sent_today` is counted from `messages` every time, never kept as a separate number, so the cap
cannot drift (§9.2).
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import attachments as attachment_store
from app.db import outbox
from app.db.models import Campaign, Draft, Lead, Mailbox, Message, Org
from app.db.state import record_event
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.domain.scheduling import (
    followup_due_at,
    in_window,
    ist_day_bounds,
    next_window_open,
    pacing_gap,
    remaining_capacity,
    warmup_cap,
)
from app.services.mail.connection import MailboxUnavailable, login_for, mark_unavailable
from app.services.mail.message import Attachment as OutgoingAttachment
from app.services.mail.message import FooterIdentity, build_message
from app.services.mail.message import Mailbox as MailboxIdentity
from app.services.mail.smtp import SmtpOutcome, SmtpSender

#: Priority order when capacity is short (§9.2): a reply to someone who wrote to us first, then
#: a due follow-up, then a new lead. Ignoring someone who replied is the worst outcome
#: available, so replies win.
KIND_PRIORITY = {outbox.KIND_REPLY: 0, outbox.KIND_FOLLOWUP: 1, outbox.KIND_INITIAL: 2}


@dataclass
class SendDecision:
    """One pass of the loop: what happened, or why nothing did."""

    sent: bool = False
    message_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    reason: str = ""
    #: How long the caller should wait before asking again.
    wait: timedelta = field(default_factory=lambda: timedelta(seconds=30))


@dataclass
class Candidate:
    """A draft that is approved and whose lead is ready for that kind of send."""

    lead: Lead
    draft: Draft
    kind: str
    #: **The campaign this lead belongs to, not the mailbox's first one.** A mailbox may serve
    #: several running campaigns, and each carries its own persona — the name, agency and footer
    #: the recipient sees. Sending a campaign B lead signed by campaign A is the kind of mistake
    #: nobody notices until somebody replies to the wrong person.
    campaign: Campaign
    #: **Resolved once, where the failure can be contained.** These were properties that called
    #: `model_validate` lazily, which meant an unreadable persona raised deep inside `send_one`
    #: -- after the candidate had been chosen, out through `tick()`, into `WorkerTask.run`,
    #: which recorded a failure and backed off. One campaign with a persona missing a field
    #: therefore stopped **every** mailbox from sending anything, and said so only as a
    #: traceback in the worker log. That is the identical shape as the `send_days` incident
    #: that produced the per-mailbox reporting in the first place; a lazy property put it back.
    persona: Persona
    settings: CampaignSettings

    @property
    def priority(self) -> int:
        return KIND_PRIORITY.get(self.kind, 9)


async def sent_today(
    session: AsyncSession,
    *,
    mailbox_id: uuid.UUID,
    settings: CampaignSettings,
    now: datetime,
) -> int:
    """Today's send count, straight from `messages`.

    §9.2 is explicit that this is never a stored counter: a counter drifts the first time a
    crash lands between the send and the increment, and a drifted cap is how a mailbox gets
    suspended.
    """
    start, end = ist_day_bounds(now, settings)
    # `sending` rows count too: a claimed-but-unresolved message is capacity already committed,
    # and not counting it would let a crash hand back allowance the mailbox has really used.
    #
    # Bucketed on sent_at where there is one, falling back to created_at for a row still in
    # flight, so a message belongs to the day it actually went out.
    occurred_at = func.coalesce(Message.sent_at, Message.created_at)
    total = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.mailbox_id == mailbox_id,
            Message.direction == "out",
            Message.status.in_(("sent", "sending")),
            occurred_at >= start,
            occurred_at < end,
        )
    )
    return int(total or 0)


async def effective_cap(
    session: AsyncSession, *, mailbox: Mailbox, settings: CampaignSettings, now: datetime
) -> int:
    """Today's ceiling for this mailbox, after warm-up."""
    local_today = now.astimezone(settings.zone).date()
    started = (
        mailbox.warmup_started_on.astimezone(settings.zone).date()
        if mailbox.warmup_started_on is not None
        else None
    )
    return warmup_cap(
        today=local_today,
        settings=settings,
        warmup_started_on=started,
        warmup_start=mailbox.warmup_start,
        warmup_step=mailbox.warmup_step,
        warmup_step_days=mailbox.warmup_step_days,
        daily_cap=mailbox.daily_cap,
    )


async def find_candidates(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    now: datetime,
    limit: int = 20,
) -> list[Candidate]:
    """Approved drafts whose lead is in a state that permits that kind of send.

    Joined on the draft rather than scanning leads, because an `APPROVED` lead with no approved
    draft is not sendable and should not occupy a slot in the queue.
    """
    rows = (
        await session.execute(
            select(Lead, Draft, Campaign)
            .join(Draft, Draft.lead_id == Lead.id)
            .join(Campaign, Campaign.id == Lead.campaign_id)
            .where(
                Lead.org_id == org_id,
                Campaign.mailbox_id == mailbox_id,
                Campaign.paused.is_(False),
                Campaign.status == "running",
                Lead.paused.is_(False),
                Draft.status == "approved",
                Lead.state.in_(("APPROVED", "SENT", "REPLIED")),
            )
            .order_by(Draft.approved_at)
            .limit(limit)
        )
    ).all()

    candidates: list[Candidate] = []
    for lead, draft, campaign in rows:
        kind = str(draft.kind)
        # The draft's kind and the lead's state have to agree, or this is a stale draft for a
        # lead that has already moved on.
        if kind == outbox.KIND_INITIAL and lead.state != "APPROVED":
            continue
        if kind == outbox.KIND_FOLLOWUP:
            if lead.state != "SENT":
                continue
            if lead.next_action_at is None or lead.next_action_at > now:
                continue  # not due yet
        if kind == outbox.KIND_REPLY and lead.state != "REPLIED":
            continue
        # Everything that can refuse this campaign is resolved here, in one guarded place,
        # because this is the only point at which "skip this campaign" is still an option.
        #
        # **A campaign with no readable persona cannot send, and must not borrow one.** The
        # persona is the name, agency and footer the recipient sees. The previous code fell back
        # to the mailbox's first campaign's persona, which signed one campaign's email with
        # another campaign's identity -- the same defect this file was changed to fix, surviving
        # in the one branch that was meant to handle its absence.
        try:
            settings = CampaignSettings.model_validate(campaign.settings or {})
            persona = Persona.model_validate(campaign.persona or {})
        except ValidationError:
            # One unreadable campaign is that campaign's problem. `SendLoop` reports it by name;
            # here we simply do not send for it, and the other campaigns have their turn.
            continue

        # A campaign whose window is shut right now contributes nothing, and it must not block
        # a campaign whose window is open. The window is per campaign; the cap is per mailbox.
        if not in_window(now, settings):
            continue

        candidates.append(
            Candidate(
                lead=lead,
                draft=draft,
                kind=kind,
                campaign=campaign,
                persona=persona,
                settings=settings,
            )
        )

    # Replies first, then due follow-ups, then new leads (§9.2) — and within a kind, one from
    # each campaign in turn.
    return _interleave(candidates, now)


def _interleave(candidates: list[Candidate], now: datetime) -> list[Candidate]:
    """Order the queue so every running campaign makes progress under one shared cap.

    The cap belongs to the mailbox, because it exists to protect a sending *reputation* and
    reputation attaches to the address — not to how many campaigns somebody split the work
    into. So two live campaigns share one allowance, and the question is who goes first.

    **Replies first, across every campaign.** Someone who wrote to us is waiting, and which
    campaign they came from is our bookkeeping, not their problem. Then due follow-ups, then
    first emails.

    **Within a kind, round-robin across campaigns**, oldest approval first inside each. Strict
    first-approved-first-sent would be simpler and wrong: a campaign holding five hundred
    approved drafts would starve a campaign holding five for a working week, and an operator
    running two campaigns expects both to move today.
    """
    by_kind: dict[int, dict[uuid.UUID, list[Candidate]]] = {}
    for candidate in sorted(candidates, key=lambda c: c.draft.approved_at or now):
        by_kind.setdefault(candidate.priority, {}).setdefault(
            candidate.lead.campaign_id, []
        ).append(candidate)

    ordered: list[Candidate] = []
    for priority in sorted(by_kind):
        queues = list(by_kind[priority].values())
        # Deal one from each campaign in turn until every queue is empty.
        while any(queues):
            for queue in queues:
                if queue:
                    ordered.append(queue.pop(0))
    return ordered


async def send_one(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    mailbox: Mailbox,
    settings: CampaignSettings,
    sender: SmtpSender,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> SendDecision:
    """Do at most one send, and say how long to wait before the next attempt.

    One per call so the caller controls pacing, and so a single send is the unit that either
    completes or is recoverable — never half a batch.

    `settings` are the **mailbox-level** defaults, and are used only for what belongs to the
    mailbox: the day boundary for the cap, and how long to wait when nothing is ready. There is
    deliberately no `persona` parameter. A message is signed by the persona of the campaign the
    lead belongs to and by nothing else, so the only correct source is the chosen candidate --
    passing one in is what allowed a lead to go out under another campaign's name.
    """
    now = now or datetime.now(tz=UTC)

    org = await session.get(Org, org_id)
    if org is None:
        return SendDecision(reason="org not found", wait=timedelta(minutes=5))
    if org.paused:
        # The global kill switch. Checked first, so flipping it stops everything at once.
        return SendDecision(reason="the organisation is paused", wait=timedelta(minutes=5))
    if mailbox.paused:
        return SendDecision(
            reason=f"mailbox paused: {mailbox.pause_reason or 'no reason given'}",
            wait=timedelta(minutes=5),
        )

    cap = await effective_cap(session, mailbox=mailbox, settings=settings, now=now)
    used = await sent_today(session, mailbox_id=mailbox.id, settings=settings, now=now)
    remaining = remaining_capacity(effective_cap=cap, sent_today=used)
    if remaining <= 0:
        opens = next_window_open(now + timedelta(days=1), settings)
        return SendDecision(
            reason=f"daily cap reached ({used}/{cap})",
            wait=max(opens - now, timedelta(minutes=10)),
        )

    if mailbox.next_send_at is not None and mailbox.next_send_at > now:
        return SendDecision(
            reason="pacing", wait=max(mailbox.next_send_at - now, timedelta(seconds=5))
        )

    # `find_candidates` drops anything whose own campaign window is shut, so an empty list here
    # means either nothing is ready or nothing is ready *yet*. Distinguishing the two matters
    # only for how long to wait, and the window is the longer of the two.
    candidates = await find_candidates(session, org_id=org_id, mailbox_id=mailbox.id, now=now)
    if not candidates:
        if not in_window(now, settings):
            opens = next_window_open(now, settings)
            return SendDecision(
                reason="outside the send window",
                wait=max(opens - now, timedelta(seconds=30)),
            )
        return SendDecision(reason="nothing is ready to send", wait=timedelta(minutes=2))

    chosen = candidates[0]
    # From here on the lead's own campaign decides: its persona signs the message and its
    # settings schedule the follow-up. Both were validated when the candidate was built.
    persona = chosen.persona
    settings = chosen.settings

    # Step 1 and 2: re-check everything against the database, then claim the outbox row. The
    # approval may be hours old, and a reply or opt-out may have arrived since.
    # **As whom.** Resolved before the claim, deliberately: a claim writes a `sending` row
    # that ADR-002's recovery pass must afterwards resolve, and burning one on a mailbox we
    # already know cannot authenticate would manufacture work for a human to confirm.
    #
    # A mailbox whose connection has lapsed is paused with a reason rather than retried. With an
    # OAuth client in Testing status a refresh token expires every 7 days, so this is a weekly
    # event by design -- and retrying a credential that will never work again is how a provider
    # starts treating us as an attack.
    try:
        login = await login_for(session, mailbox=mailbox, settings=sender.settings, now=now)
    except MailboxUnavailable as exc:
        await mark_unavailable(session, mailbox=mailbox, reason=str(exc), now=now)
        await session.commit()
        return SendDecision(
            reason=f"mailbox needs reconnecting: {exc}",
            wait=timedelta(minutes=15),
        )

    # The thread this message belongs to. Without these a follow-up arrives as a new
    # conversation, which is both confusing for the recipient and worse for deliverability:
    # three unrelated-looking emails from one sender read as three cold emails.
    in_reply_to, references = await outbox.thread_headers(
        session, lead_id=chosen.lead.id, kind=chosen.kind
    )

    claim = await outbox.claim_send(
        session,
        org_id=org_id,
        lead_id=chosen.lead.id,
        kind=chosen.kind,
        mailbox_id=mailbox.id,
        draft_id=chosen.draft.id,
        subject=chosen.draft.subject,
        body_text=chosen.draft.body,
        in_reply_to=in_reply_to,
        references=references,
        now=now,
    )
    if not claim.claimed or claim.message is None:
        return SendDecision(
            reason=f"not eligible: {claim.reason}",
            lead_id=chosen.lead.id,
            wait=timedelta(seconds=5),
        )

    message_row = claim.message

    # The campaign's file, if this email carries one. Resolved here rather than at approval, so
    # that replacing the file updates every email not yet sent -- which is what an operator who
    # has just corrected their deck expects -- while the ones already gone keep what they had.
    #
    # Read *after* the claim, deliberately: the claim is the cheap set of refusals (suppressed,
    # capped, already in touch) and there is no reason to move several megabytes out of the
    # database for a message that is not going to be sent.
    carried = await attachment_store.load_for_send(
        session,
        org_id=org_id,
        campaign=chosen.campaign,
        draft=chosen.draft,
        settings=settings,
    )

    built = build_message(
        mailbox=MailboxIdentity(
            from_name=mailbox.from_name,
            from_email=mailbox.from_email,
            reply_to=mailbox.reply_to,
        ),
        to_email=chosen.lead.email_norm,
        subject=chosen.draft.subject,
        body=chosen.draft.body,
        identity=FooterIdentity(
            sender_name=persona.sender_name,
            agency_name=persona.agency_name,
            agency_url=str(persona.agency_url) if persona.agency_url else None,
            postal_address=persona.postal_address or None,
        ),
        message_id=message_row.message_id_hdr,
        in_reply_to=message_row.in_reply_to,
        references=message_row.references_hdr,
        attachments=(
            [
                OutgoingAttachment(
                    filename=carried.filename,
                    content_type=carried.content_type,
                    content=carried.content,
                )
            ]
            if carried is not None
            else []
        ),
        now=now,
    )

    # Pace the *next* send before this one leaves, so a crash mid-send cannot cause a burst on
    # restart.
    gap = pacing_gap(now=now, settings=settings, remaining=remaining, rng=rng)
    mailbox.next_send_at = now + gap
    await session.commit()

    # Step 3: the one SMTP call. Nothing else in the system sends mail.
    # The org's chosen mode travels with the send. `org` was already loaded to check `paused`,
    # so this costs no query, and the decision stays inside the guard rather than here (ADR-010).
    report = await sender.send(
        built, to_email=chosen.lead.email_norm, send_mode=org.send_mode, login=login
    )

    # Step 4: record the outcome in its own transaction.
    if report.outcome is SmtpOutcome.SENT:
        follow_at = (
            followup_due_at(first_sent_at=now, settings=settings)
            if chosen.kind == outbox.KIND_INITIAL and settings.max_followups > 0
            else None
        )
        await outbox.mark_sent(
            session,
            org_id=org_id,
            message_id=message_row.id,
            smtp_response=report.response,
            next_action_at=follow_at,
            now=now,
        )
        chosen.draft.status = "sent"
        await session.commit()
        return SendDecision(
            sent=True,
            message_id=message_row.id,
            lead_id=chosen.lead.id,
            reason="sent",
            wait=gap,
        )

    if report.outcome is SmtpOutcome.BLOCKED:
        # SEND_MODE refused it. Not a failure — the lead is parked, not broken.
        await outbox.mark_failed(
            session, org_id=org_id, message_id=message_row.id, error=report.detail, now=now
        )
        await _skip_lead(session, org_id=org_id, candidate=chosen, reason=report.detail, now=now)
        await session.commit()
        return SendDecision(
            lead_id=chosen.lead.id, reason=f"blocked: {report.detail}", wait=timedelta(seconds=1)
        )

    if report.outcome is SmtpOutcome.REJECTED:
        # A 5xx on the recipient is a hard bounce discovered at send time: suppress immediately
        # rather than discovering it again tomorrow.
        await outbox.mark_failed(
            session, org_id=org_id, message_id=message_row.id, error=report.detail, now=now
        )
        await _bounce_lead(session, org_id=org_id, candidate=chosen, reason=report.detail, now=now)
        await session.commit()
        return SendDecision(
            lead_id=chosen.lead.id, reason=f"rejected: {report.detail}", wait=timedelta(seconds=2)
        )

    await outbox.mark_failed(
        session, org_id=org_id, message_id=message_row.id, error=report.detail, now=now
    )
    if report.should_pause_mailbox:
        # §9.8: never retry-spam an auth failure. Retrying a wrong password looks like a
        # brute-force attempt and gets the mailbox locked.
        mailbox.paused = True
        mailbox.pause_reason = f"SMTP authentication failed: {report.detail[:200]}"
        await record_event(
            session,
            org_id=org_id,
            lead_id=chosen.lead.id,
            type="mailbox.paused",
            data={"reason": mailbox.pause_reason},
        )
    await session.commit()
    return SendDecision(
        lead_id=chosen.lead.id,
        reason=f"failed: {report.detail}",
        wait=timedelta(minutes=5) if report.should_pause_mailbox else timedelta(seconds=30),
    )


async def _skip_lead(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    candidate: Candidate,
    reason: str,
    now: datetime,
) -> None:
    """Park a lead that could not be sent to, without losing it."""
    from app.db.state import transition
    from app.domain.state import LeadState

    if candidate.kind == outbox.KIND_INITIAL:
        await transition(
            session,
            org_id=org_id,
            lead_id=candidate.lead.id,
            to_state=LeadState.SKIPPED,
            data={"reason": reason},
            flags_add=["not_allowlisted"],
            now=now,
        )


async def _bounce_lead(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    candidate: Candidate,
    reason: str,
    now: datetime,
) -> None:
    """Suppress and close a lead the server refused outright."""
    from app.db.repo import OrgContext, add_suppression
    from app.db.state import transition
    from app.domain.state import LeadState

    await add_suppression(
        OrgContext(org_id=org_id),
        session,
        value=candidate.lead.email_norm,
        reason="bounce",
    )
    await transition(
        session,
        org_id=org_id,
        lead_id=candidate.lead.id,
        to_state=LeadState.BOUNCED,
        data={"reason": reason[:300]},
        clear_next_action=True,
        now=now,
    )
