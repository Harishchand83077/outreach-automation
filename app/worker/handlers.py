"""What the `JobRunner` actually does for each kind of job.

`ARCHITECTURE_TARGET.md` §8. A handler takes the context and a claimed job, does the work, and
either returns (the runner marks the job succeeded) or raises. Raising `PermanentFailure` sends
the job straight to `dead` without burning retries, because retrying a bad payload only produces
the same error more slowly.

Two kinds are deliberately **not** registered here:

- `send_message`. The `SendLoop` is the only code that calls SMTP (hard rule 1), and it decides
  what to send by querying approved drafts, not by draining a queue. A handler that sent mail
  would be a second sender, and the daily cap would become a race between them.
- `classify_reply`. The ingest loop classifies each message inside the transaction that stores
  it, so the label is persisted before anything acts on it (§10.3). Queueing the classification
  separately would reintroduce the window where a reply exists with no label.

Both constants stay in `JobKind` because the queue may still carry rows written before this was
settled; the runner simply never claims them.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import drafts as draft_store
from app.db.models import Campaign, Classification, Job, Lead, Message
from app.db.queue import JobKind, enqueue
from app.db.repo import OrgContext
from app.db.state import transition
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.domain.reply import ReplyLabel
from app.domain.state import LeadState
from app.domain.template import CampaignTemplates
from app.services.drafting.from_template import draft_from_template
from app.services.drafting.generator import DraftResult, generate_draft
from app.services.lifecycle import followup_number
from app.services.reply.drafter import draft_reply
from app.services.research.fetcher import Fetcher
from app.services.research.service import research_lead
from app.worker.context import WorkerContext


class PermanentFailure(Exception):
    """This job will never succeed. Do not retry it."""


class Handler(Protocol):
    async def __call__(self, ctx: WorkerContext, session: AsyncSession, job: Job) -> None: ...


def _payload_uuid(job: Job, key: str) -> uuid.UUID:
    raw = (job.payload or {}).get(key)
    if raw is None:
        raise PermanentFailure(f"job payload has no {key}")
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise PermanentFailure(f"job payload {key} is not a uuid: {raw!r}") from exc


async def _lead_and_campaign(session: AsyncSession, job: Job) -> tuple[Lead, Campaign]:
    lead_id = _payload_uuid(job, "lead_id")
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.org_id != job.org_id:
        # Not a retryable condition: a lead deleted or belonging to another org will not appear.
        raise PermanentFailure(f"lead {lead_id} not found in this org")
    campaign = await session.get(Campaign, lead.campaign_id)
    if campaign is None:
        raise PermanentFailure(f"campaign {lead.campaign_id} not found")
    return lead, campaign


def _persona(campaign: Campaign) -> Persona:
    if not campaign.persona:
        raise PermanentFailure("campaign has no persona; nothing may be asserted without one")
    return Persona.model_validate(campaign.persona)


async def handle_research_lead(ctx: WorkerContext, session: AsyncSession, job: Job) -> None:
    """Research one lead from real sources and store the verified hooks."""
    lead, campaign = await _lead_and_campaign(session, job)
    persona = _persona(campaign)
    router = ctx.router_for(session, job.org_id)

    # `NEW -> RESEARCHED` is not a legal transition, and nothing else ever set `ENRICHING`, so
    # every research job died on the final transition after doing all of its work. The state is
    # claimed *before* the work rather than after it, which is also what makes a crash visible:
    # a lead left in `ENRICHING` says "this was being worked on", where `NEW` would say nothing
    # happened at all.
    #
    # Guarded, because a retried job arrives with the lead already `ENRICHING`, and
    # `ENRICHING -> ENRICHING` is not legal either.
    if lead.state == LeadState.NEW.value:
        await transition(
            session,
            org_id=job.org_id,
            lead_id=lead.id,
            to_state=LeadState.ENRICHING,
            data={"job": str(job.id)},
        )

    async with httpx.AsyncClient(follow_redirects=True) as client:
        outcome = await research_lead(
            session,
            org_id=job.org_id,
            lead=lead,
            offer=persona.offer,
            router=router,
            fetcher=Fetcher(client=client),
        )

    if outcome.skipped_reason:
        # A lead we cannot research is not a failed job — it is a lead a human should look at.
        await transition(
            session,
            org_id=job.org_id,
            lead_id=lead.id,
            to_state=LeadState.SKIPPED,
            data={"reason": outcome.skipped_reason, "job": str(job.id)},
        )
        return

    await transition(
        session,
        org_id=job.org_id,
        lead_id=lead.id,
        to_state=LeadState.RESEARCHED,
        data={"hooks": len(outcome.verified.hooks) if outcome.verified else 0},
    )

    # Queue the draft in the same transaction as the state change that earned it, exactly as the
    # importer queues research beside the insert. Without this the pipeline simply stopped: four
    # leads reached RESEARCHED with findings stored and nothing ever asked for an email, so the
    # review queue stayed empty and looked idle rather than broken.
    #
    # `dedupe_key` makes a retried research job harmless: the second attempt does not queue a
    # second draft for the same lead.
    await enqueue(
        session,
        org_id=job.org_id,
        kind=JobKind.DRAFT_LEAD,
        lead_id=lead.id,
        payload={"lead_id": str(lead.id)},
        dedupe_key=f"draft:{lead.id}",
    )


def _render_for(campaign: Campaign, lead: Lead, persona: Persona) -> DraftResult:
    """Render a `template`-mode campaign's first email for one lead.

    **An unreadable template fails the job, and that is the deliberate choice.** The two
    alternatives are both worse. Skipping the lead would reach a terminal state, so fixing the
    template later would mean hand-recovering every lead it touched. Storing an empty failed
    draft would put a row in front of a reviewer that no editing can rescue, and `save_draft`
    refuses it anyway.

    Failing is contained and it is loud in the right way: the job retries, eventually goes
    `dead`, and the scheduler already alerts on dead jobs -- while the lead stays `NEW`, so
    correcting the template and re-queueing is all the recovery needed. The route refuses to
    create a campaign in this state, so reaching here means the row was edited by hand, which is
    exactly how a hand-edited `send_days` once stopped sending for every mailbox.

    The field names are named and the stored *values* never are: this message reaches the logs.
    """
    try:
        templates = CampaignTemplates.model_validate(campaign.templates or {})
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise ValueError(
            f"campaign {campaign.id} is in template mode and its templates are not readable "
            f"({', '.join(fields)}); no email can be rendered until they are fixed"
        ) from exc

    return draft_from_template(
        template=templates.initial,
        persona=persona,
        lead_id=lead.id,
        full_name=lead.full_name,
        email=lead.email_norm,
        company_name=lead.company_name,
        company_url=lead.company_url,
        domain=lead.domain,
        job_title=lead.job_title,
        notes=lead.notes,
        extra=lead.extra,
        kind="initial",
    )


async def handle_draft_lead(ctx: WorkerContext, session: AsyncSession, job: Job) -> None:
    """Write the first email for a lead and queue it for review.

    Who writes it is the campaign's `mode`. `research` asks a model, grounded in the hooks the
    research step verified. `template` renders the operator's own text with this lead's details
    merged in, and calls no model at all. Everything after this point is identical -- the same
    stored draft, the same human approval, the same send loop.
    """
    lead, campaign = await _lead_and_campaign(session, job)
    persona = _persona(campaign)
    ctx_org = OrgContext(org_id=job.org_id)

    if campaign.mode == "template":
        result = _render_for(campaign, lead, persona)
    else:
        # A model is about to be asked to write on our behalf, so it has to be told who that is.
        # The route refuses to create a research campaign without this and refuses to patch one
        # into that state, so reaching here means the row was edited by hand -- and an email
        # written from a persona with no offer in it is the exact failure hard rule 6 exists to
        # prevent. Fail the job rather than send something invented.
        if undraftable := persona.missing_for_drafting():
            raise ValueError(
                f"campaign {campaign.id} researches and drafts with a model, and its persona "
                "does not say enough for that: " + ", ".join(field for field, _ in undraftable)
            )
        router = ctx.router_for(session, job.org_id)
        hooks = await _hooks_for(session, job.org_id, lead.id)
        # If this is a regeneration, the reviewer said why. Carry it, or they will reject the
        # same draft twice and conclude the feedback box does nothing.
        reviewer_feedback = await draft_store.latest_rejection_feedback(
            session, ctx_org, lead_id=lead.id, kind="initial"
        )
        result = await generate_draft(
            router=router,
            persona=persona,
            hooks=hooks,
            lead_id=lead.id,
            lead_name=lead.full_name,
            lead_company=lead.company_name,
            job_title=lead.job_title,
            kind="initial",
            reviewer_feedback=reviewer_feedback,
        )

    await draft_store.save_draft(session, ctx_org, lead=lead, kind="initial", result=result)

    # DRAFTED first, always — unless the lead is already there, which is what a regeneration
    # looks like. The table is RESEARCHED -> DRAFTED -> AWAITING_APPROVAL, and jumping straight
    # to AWAITING_APPROVAL was refused after the model had been paid for and the draft written.
    # Two steps also leave an honest audit trail: the events row says a draft was produced, and
    # then that it passed validation.
    #
    # **The skip is deliberate, and so is not relaxing the state machine instead.** A rejected
    # lead is put back into DRAFTED by the rejection, so the replacement draft arrives with the
    # lead already in that state and the worker raised `DRAFTED -> DRAFTED is not a legal
    # transition` — after paying for the model. Allowing self-transitions would have fixed this
    # one case and quietly made `SENT -> SENT` legal, which is a second email. The audit trail
    # loses nothing: `save_draft` writes a `draft.created` event for every attempt, which is
    # what actually shows a second draft in the timeline.
    if LeadState(lead.state) is not LeadState.DRAFTED:
        await transition(
            session,
            org_id=job.org_id,
            lead_id=lead.id,
            to_state=LeadState.DRAFTED,
            data={"accepted": result.accepted, "attempts": result.attempt_count},
        )

    # A draft that failed validation is stored but never becomes sendable, so the lead waits for
    # a person either way — the difference is only which queue a human finds it in.
    if result.accepted:
        await transition(
            session,
            org_id=job.org_id,
            lead_id=lead.id,
            to_state=LeadState.AWAITING_APPROVAL,
            data={"attempts": result.attempt_count},
        )


async def handle_draft_followup(ctx: WorkerContext, session: AsyncSession, job: Job) -> None:
    """Write the follow-up for a lead whose follow-up has come due.

    The same split as a first email: `template` mode renders the operator's own follow-up text,
    `research` mode asks a model, reusing the hooks the research step already verified so the
    follow-up can refer to the same specific thing the first email did.

    **The lead stays `SENT`.** A follow-up is sent *from* `SENT` -- `outbox._VALID_FROM` says so
    -- and approving the draft is what makes it eligible, not a state change.
    """
    lead, campaign = await _lead_and_campaign(session, job)
    persona = _persona(campaign)
    ctx_org = OrgContext(org_id=job.org_id)
    number = await followup_number(session, lead_id=lead.id)

    if campaign.mode == "template":
        result = _render_followup_for(campaign, lead, persona, number=number)
    else:
        if undraftable := persona.missing_for_drafting():
            raise ValueError(
                f"campaign {campaign.id} drafts with a model and its persona does not say "
                "enough for that: " + ", ".join(field for field, _ in undraftable)
            )
        result = await generate_draft(
            router=ctx.router_for(session, job.org_id),
            persona=persona,
            hooks=await _hooks_for(session, job.org_id, lead.id),
            lead_id=lead.id,
            lead_name=lead.full_name,
            lead_company=lead.company_name,
            job_title=lead.job_title,
            kind="followup",
            reviewer_feedback=await draft_store.latest_rejection_feedback(
                session, ctx_org, lead_id=lead.id, kind="followup"
            ),
        )

    await draft_store.save_draft(session, ctx_org, lead=lead, kind="followup", result=result)


def _render_followup_for(
    campaign: Campaign, lead: Lead, persona: Persona, *, number: int
) -> DraftResult:
    """Render a template campaign's follow-up number `number` (one-based).

    A campaign may allow more follow-ups than it has written templates for -- `max_followups`
    and the template list are set in two different places -- and the smaller has to win, or the
    campaign tries to send an email nobody wrote.
    """
    try:
        templates = CampaignTemplates.model_validate(campaign.templates or {})
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise ValueError(
            f"campaign {campaign.id} is in template mode and its templates are not readable "
            f"({', '.join(fields)}); no follow-up can be rendered"
        ) from exc

    template = templates.for_followup(number - 1)
    if template is None:
        raise PermanentFailure(
            f"campaign {campaign.id} has no template for follow-up {number}; "
            "nothing to send, so this is not retryable"
        )

    return draft_from_template(
        template=template,
        persona=persona,
        lead_id=lead.id,
        full_name=lead.full_name,
        email=lead.email_norm,
        company_name=lead.company_name,
        company_url=lead.company_url,
        domain=lead.domain,
        job_title=lead.job_title,
        notes=lead.notes,
        extra=lead.extra,
        kind="followup",
    )


async def handle_draft_reply(ctx: WorkerContext, session: AsyncSession, job: Job) -> None:
    """Propose an answer to an inbound reply. Never sends; produces a draft for review.

    **Both of this job's callers disagreed with this handler, in opposite directions**, and the
    result was that a proposed reply was never written at all:

    - `reply.service` enqueues on ingest with `payload={"message_id", "label"}` and puts the
      lead in the `jobs.lead_id` *column*. The handler read `payload["lead_id"]`, so every
      reply from a real inbound message died on `job payload has no lead_id` -- permanently,
      because a missing key is not retryable. The lead reached `REPLIED` and then nothing
      proposed an answer, which looks from the dashboard exactly like a reply nobody has got to
      yet.
    - `drafts.reject` enqueues a regeneration with `payload={"lead_id"}` and no `message_id`,
      so rejecting a reply draft would have died the same way on the other key.

    So the job is resolved from whatever each caller does provide: the lead from the column
    first (every caller sets it, and `jobs.lead_id` is the FK that makes a deleted lead cascade
    its work away), and the message from the payload or, for a regeneration, the latest inbound
    message for that lead -- which is the message a regenerated reply is answering.
    """
    lead_id = job.lead_id or _payload_uuid(job, "lead_id")
    message_id = await _reply_message_id(session, job, lead_id)
    label = await _reply_label(session, job, message_id)

    await draft_reply(
        session,
        OrgContext(org_id=job.org_id),
        router=ctx.router_for(session, job.org_id),
        lead_id=lead_id,
        message_id=message_id,
        label=label,
    )


async def _reply_message_id(session: AsyncSession, job: Job, lead_id: uuid.UUID) -> uuid.UUID:
    """The inbound message this reply answers.

    From the payload when the caller named one. A regeneration does not: a reviewer rejecting a
    proposed reply is asking for a better answer to the *same* message, so the newest inbound
    one for the lead is the right answer and the only one that is always available.
    """
    raw = (job.payload or {}).get("message_id")
    if raw is not None:
        return _payload_uuid(job, "message_id")

    latest = await session.scalar(
        select(Message.id)
        .where(
            Message.org_id == job.org_id,
            Message.lead_id == lead_id,
            Message.direction == "in",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if latest is None:
        # Nothing to answer. Not retryable: an inbound message does not appear later for a job
        # that was queued because one had already arrived.
        raise PermanentFailure(f"lead {lead_id} has no inbound message to reply to")
    return latest


async def _reply_label(session: AsyncSession, job: Job, message_id: uuid.UUID) -> ReplyLabel:
    """What we decided this inbound message was.

    From the payload when the caller named one. Otherwise from `classifications`, which is the
    authoritative record -- written before anything acts on it, precisely so that the answer
    does not depend on re-running a model whose weights may have changed (ADR for issue B17).
    Reading it back beats storing a second copy on the draft, which could disagree with it.

    A regeneration has no label of its own, and there is no safe default: the label decides
    whether a reply may be drafted at all, so guessing could propose an answer to somebody who
    asked to be left alone.
    """
    raw = (job.payload or {}).get("label")
    if raw is None:
        raw = await session.scalar(
            select(Classification.label)
            .where(
                Classification.org_id == job.org_id,
                Classification.message_id == message_id,
            )
            .order_by(Classification.created_at.desc())
            .limit(1)
        )
    if raw is None:
        raise PermanentFailure(f"message {message_id} has no classification to reply from")
    try:
        return ReplyLabel(str(raw))
    except ValueError as exc:
        raise PermanentFailure(f"unknown reply label {raw!r}") from exc


async def _hooks_for(
    session: AsyncSession, org_id: uuid.UUID, lead_id: uuid.UUID
) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.db.models import Research

    row = await session.scalar(
        select(Research)
        .where(Research.org_id == org_id, Research.lead_id == lead_id)
        .order_by(Research.created_at.desc())
        .limit(1)
    )
    return list(row.hooks or []) if row is not None else []


def campaign_settings(campaign: Campaign) -> CampaignSettings:
    return CampaignSettings.model_validate(campaign.settings or {})


#: The kinds the runner claims. Anything absent here is never picked up — see the module
#: docstring for why `send_message` and `classify_reply` are not in it.
HANDLERS: dict[str, Handler] = {
    JobKind.RESEARCH_LEAD: handle_research_lead,
    JobKind.DRAFT_LEAD: handle_draft_lead,
    JobKind.DRAFT_FOLLOWUP: handle_draft_followup,
    JobKind.DRAFT_REPLY: handle_draft_reply,
}
