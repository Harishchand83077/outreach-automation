"""Preparing a reply for a human to approve. It prepares; it never sends.

`ARCHITECTURE_TARGET.md` §10.4. This is the whole of what a `needs_human` label causes: a draft
row with status `pending_review`, sitting in the same queue as a first email, waiting for
somebody to click approve. `approval_mode = review_all` covers every reply, with no
`auto_send_if_confidence` path enabled for them (and the product rules say there never will be).

The prototype did the opposite: `handle_positive` classified a reply and sent an answer in the
same function, with no human and no record of the decision (issues B4, B5, C5).

Two things here are worth stating plainly because they are easy to lose later:

- **The lead's reply becomes a citable hook, `hook:reply`.** A reply that answers a question
  usually has to refer to what they said, and the grounding rules would otherwise reject every
  such sentence as an unsupported claim. Their own words are the one new evidence source a
  reply gets — and because it arrives as a hook, every existing validation rule applies to it
  unchanged.
- **The subject is ours, not the model's.** It is overwritten with `Re: <thread subject>` after
  generation, so a model that gets creative with the subject cannot break threading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import drafts as drafts_repo
from app.db.models import Campaign, Lead, Message, Research
from app.db.outbox import is_suppressed
from app.db.repo import OrgContext
from app.db.state import record_event
from app.domain.persona import Persona
from app.domain.reply import ReplyAction, ReplyLabel, policy_for
from app.domain.state import TERMINAL, LeadState
from app.services.drafting.generator import DraftResult, Router, generate_draft
from app.services.drafting.prompts import reply_subject
from app.services.mail.parse import strip_quoted

#: How much of their reply to carry into the prompt and the evidence corpus.
MAX_REPLY_CHARS = 2000


@dataclass
class ReplyDraftOutcome:
    """What happened. `draft_id` is None when there was nothing to draft."""

    draft_id: uuid.UUID | None = None
    accepted: bool = False
    skipped_reason: str | None = None


def reply_hook(their_reply: str) -> dict[str, object]:
    """Their own words, shaped as a research hook so the validator can resolve them.

    `source_url` says where it came from in words rather than as a link, because a reviewer
    reading the evidence panel beside a draft should see "their reply" and not a URL that goes
    nowhere.
    """
    text = (their_reply or "").strip()[:MAX_REPLY_CHARS]
    return {
        "id": "hook:reply",
        "text": "what they said in their reply",
        "snippet": text,
        "source_url": "their reply",
        "verified": True,
    }


async def draft_reply(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    router: Router,
    lead_id: uuid.UUID,
    message_id: uuid.UUID,
    label: ReplyLabel,
    created_by: str = "system",
    now: datetime | None = None,
) -> ReplyDraftOutcome:
    """Generate and store a proposed reply. Returns without drafting when it should not.

    Refuses in three cases, each of which is a correctness requirement rather than an
    optimisation:

    1. **The label's policy does not call for a draft.** An opt-out must not produce a drafted
       acknowledgement, because an acknowledgement is another unwanted email.
    2. **The lead is suppressed or terminal.** A draft for somebody we have stopped contacting
       is a loaded gun sitting in a review queue.
    3. **There is no campaign persona.** Without proof points there is nothing the drafter is
       allowed to assert, and a draft asserting nothing is not worth a reviewer's time.
    """
    now = now or datetime.now(tz=UTC)

    lead = await session.get(Lead, lead_id)
    if lead is None or lead.org_id != ctx.org_id:
        return ReplyDraftOutcome(skipped_reason="lead not found in this org")

    # **Why no draft appeared has to be on the lead, not only in this return value.** Several
    # labels deliberately produce no proposed reply -- `other` is "unclear, a person decides",
    # and `not_interested` is a clear no -- but nothing recorded that, so the lead sat in
    # `REPLIED` with no draft, no job and no explanation anywhere an operator could see. From
    # the dashboard that is indistinguishable from drafting being broken, which it also was at
    # the time, so the two were impossible to tell apart.
    policy = policy_for(label)
    skip = ""
    if not policy.draft_reply:
        skip = (
            f"the policy for {label.value} is {policy.action.value}, which does not send "
            "or draft anything"
        )
    elif policy.action is not ReplyAction.NEEDS_HUMAN:
        # Belt and braces: the only action that may reach here is the one that asks for a human.
        skip = f"{label.value} does not need a human reply"

    if skip:
        await record_event(
            session,
            org_id=ctx.org_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            actor="system",
            type="reply.no_draft_by_policy",
            data={"label": label.value, "action": policy.action.value, "why": skip},
        )
        return ReplyDraftOutcome(skipped_reason=skip)
    if await is_suppressed(session, org_id=ctx.org_id, email=lead.email_norm):
        # Suppression is checked again at send time, but a suppressed address must not even
        # reach a review queue: a reviewer should never be shown an approve button for someone
        # who asked us to stop.
        return ReplyDraftOutcome(skipped_reason="the address is suppressed")
    if LeadState(lead.state) in TERMINAL:
        return ReplyDraftOutcome(skipped_reason=f"the lead is in the terminal state {lead.state}")

    message = await session.get(Message, message_id)
    if message is None or message.org_id != ctx.org_id or message.direction != "in":
        return ReplyDraftOutcome(skipped_reason="inbound message not found")

    campaign = await session.get(Campaign, lead.campaign_id)
    if campaign is None or not campaign.persona:
        return ReplyDraftOutcome(skipped_reason="the campaign has no persona to write as")
    persona = Persona.model_validate(campaign.persona)

    their_text = strip_quoted(message.body_text or "")[:MAX_REPLY_CHARS]
    hooks: list[dict[str, object]] = [reply_hook(their_text)]

    research = await session.scalar(
        select(Research)
        .where(Research.org_id == ctx.org_id, Research.lead_id == lead.id)
        .order_by(Research.created_at.desc())
    )
    if research is not None:
        # The research hooks still apply: a question about our experience is best answered with
        # the same verified material the first email drew on.
        hooks.extend(dict(hook) for hook in research.hooks)

    result = await generate_draft(
        router=router,
        persona=persona,
        hooks=hooks,
        lead_id=lead.id,
        lead_name=lead.full_name,
        lead_company=lead.company_name,
        job_title=lead.job_title,
        kind="reply",
        their_subject=message.subject or "",
        their_reply=their_text,
        reply_label=label.value,
    )

    _force_thread_subject(result, message.subject or "")

    if result.draft is None and not result.attempts:
        # Nothing at all came back, so there is no text to store. The lead is already `REPLIED`,
        # which is where a human will find it; the event says why no draft appeared.
        await record_event(
            session,
            org_id=ctx.org_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            actor="llm",
            type="reply.draft_unavailable",
            data={"label": label.value, "why": result.last_feedback or "no response"},
        )
        return ReplyDraftOutcome(skipped_reason="the model produced nothing")

    row = await drafts_repo.save_draft(
        session, ctx, lead=lead, kind="reply", result=result, created_by=created_by
    )
    return ReplyDraftOutcome(draft_id=row.id, accepted=result.accepted)


def _force_thread_subject(result: DraftResult, their_subject: str) -> None:
    """Replace whatever subject came back with the thread's own.

    Done after validation rather than before so that a model ignoring the instruction is still
    visible in the stored validation report, instead of being silently papered over.
    """
    subject = reply_subject(their_subject)
    for attempt in result.attempts:
        if attempt.draft is not None:
            attempt.draft.subject = subject
    if result.draft is not None:
        result.draft.subject = subject
