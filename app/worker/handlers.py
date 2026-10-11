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
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import drafts as draft_store
from app.db.models import Campaign, Job, Lead
from app.db.queue import JobKind, enqueue
from app.db.repo import OrgContext
from app.db.state import transition
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.domain.reply import ReplyLabel
from app.domain.state import LeadState
from app.services.drafting.generator import generate_draft
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


async def handle_draft_lead(ctx: WorkerContext, session: AsyncSession, job: Job) -> None:
    """Generate the first email for a researched lead and queue it for review."""
    lead, campaign = await _lead_and_campaign(session, job)
    persona = _persona(campaign)
    router = ctx.router_for(session, job.org_id)
    hooks = await _hooks_for(session, job.org_id, lead.id)

    result = await generate_draft(
        router=router,
        persona=persona,
        hooks=hooks,
        lead_id=lead.id,
        lead_name=lead.full_name,
        lead_company=lead.company_name,
        job_title=lead.job_title,
        kind="initial",
    )

    ctx_org = OrgContext(org_id=job.org_id)
    await draft_store.save_draft(session, ctx_org, lead=lead, kind="initial", result=result)

    # A draft that failed validation is stored but never becomes sendable, so the lead waits for
    # a person either way — the difference is only which queue a human finds it in.
    to_state = LeadState.AWAITING_APPROVAL if result.accepted else LeadState.DRAFTED
    await transition(
        session,
        org_id=job.org_id,
        lead_id=lead.id,
        to_state=to_state,
        data={"accepted": result.accepted, "attempts": result.attempt_count},
    )


async def handle_draft_reply(ctx: WorkerContext, session: AsyncSession, job: Job) -> None:
    """Propose an answer to an inbound reply. Never sends; produces a draft for review."""
    lead_id = _payload_uuid(job, "lead_id")
    message_id = _payload_uuid(job, "message_id")
    raw_label = (job.payload or {}).get("label")
    try:
        label = ReplyLabel(str(raw_label))
    except ValueError as exc:
        raise PermanentFailure(f"unknown reply label {raw_label!r}") from exc

    await draft_reply(
        session,
        OrgContext(org_id=job.org_id),
        router=ctx.router_for(session, job.org_id),
        lead_id=lead_id,
        message_id=message_id,
        label=label,
    )


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
    JobKind.DRAFT_REPLY: handle_draft_reply,
}
