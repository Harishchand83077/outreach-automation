"""Draft persistence and the review queue.

`ARCHITECTURE_TARGET.md` §7C and §13. Two rules shape this module:

- **A draft is never overwritten.** Every edit or regeneration is a new version, and the old one
  is marked `superseded`. The prototype kept one mutable column, so an edit erased what the
  model had actually produced and there was no way to answer "what did we nearly send?"
  (issue E3).
- **The review queue carries its evidence.** A reviewer who has to go and look up whether a
  claim is true will stop checking by the thirtieth draft. §20 puts review at 50-100 minutes a
  day, and it is the real throughput limit, so the evidence travels with the draft.

Approving does not send. It moves the lead to `APPROVED`, which only makes it eligible for the
send loop — which re-checks everything again before anything leaves (§9.1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Draft, Lead, Research
from app.db.repo import OrgContext, _page_size
from app.db.state import record_event, transition
from app.domain.state import LeadState
from app.services.drafting.generator import DraftResult

#: A draft in one of these states is still live; anything else has been resolved.
LIVE_STATUSES = ("generated", "validated", "pending_review")


@dataclass
class ReviewItem:
    """One row of the review queue: the draft, plus what a reviewer needs to judge it."""

    draft: Draft
    lead: Lead
    #: Verified hooks with their source URLs, so a claim can be checked in one click.
    hooks: list[dict[str, Any]]
    research_confidence: float
    thin_research: bool

    @property
    def needs_individual_review(self) -> bool:
        """Whether this draft may be bulk-approved.

        Thin research or a failed validation means somebody should read this one properly. Bulk
        approval exists to make the common case fast, not to wave through the doubtful case.
        """
        return self.thin_research or bool(self.draft.validation.get("failures"))


async def next_version(session: AsyncSession, lead_id: uuid.UUID, kind: str) -> int:
    highest = await session.scalar(
        select(func.max(Draft.version)).where(Draft.lead_id == lead_id, Draft.kind == kind)
    )
    return int(highest or 0) + 1


async def save_draft(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    lead: Lead,
    kind: str,
    result: DraftResult,
    created_by: str = "system",
) -> Draft:
    """Persist a generated draft as a new version, superseding any earlier live one.

    A failed result is stored too, with `validation_failed`, because a reviewer needs to see
    what went wrong — but its status keeps it out of the approval path, so error text can never
    become email content (issue B2).
    """
    await _supersede_live(session, lead_id=lead.id, kind=kind)

    accepted = result.accepted
    draft_output = result.draft or (result.attempts[-1].draft if result.attempts else None)
    if draft_output is None:
        raise ValueError("cannot save a draft result that produced no text at all")

    row = Draft(
        org_id=ctx.org_id,
        lead_id=lead.id,
        kind=kind,
        version=await next_version(session, lead.id, kind),
        subject=draft_output.subject,
        body=draft_output.body,
        claims=[claim.model_dump() for claim in draft_output.claims],
        validation=result.validation_payload(),
        status="pending_review" if accepted else "validation_failed",
        prompt_version=result.prompt_version,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()

    await record_event(
        session,
        org_id=ctx.org_id,
        campaign_id=lead.campaign_id,
        lead_id=lead.id,
        actor="llm",
        type="draft.created",
        data={
            "kind": kind,
            "version": row.version,
            "status": row.status,
            "attempts": result.attempt_count,
        },
    )
    return row


async def _supersede_live(session: AsyncSession, *, lead_id: uuid.UUID, kind: str) -> None:
    await session.execute(
        update(Draft)
        .where(
            Draft.lead_id == lead_id,
            Draft.kind == kind,
            Draft.status.in_(LIVE_STATUSES),
        )
        .values(status="superseded")
    )


async def approve(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    draft_id: uuid.UUID,
    approved_by: str,
    edited_subject: str | None = None,
    edited_body: str | None = None,
    now: datetime | None = None,
) -> Draft:
    """Approve a draft, optionally with edits, and move the lead to `APPROVED`.

    An edit creates a **new version** rather than mutating this one, so the record of what the
    model produced survives alongside what the human decided to send.

    This does not send. It makes the lead eligible for the send loop, which re-checks
    suppression, the window, the cap and the lead's state before anything leaves.
    """
    now = now or datetime.now(tz=UTC)
    draft = await _locked_draft(session, ctx, draft_id)

    if draft.status not in LIVE_STATUSES:
        raise ValueError(f"draft {draft_id} is {draft.status}, so it cannot be approved")

    lead = await session.get(Lead, draft.lead_id)
    if lead is None or lead.org_id != ctx.org_id:
        raise ValueError("the draft's lead is missing")

    target = draft
    edited = (edited_subject is not None and edited_subject != draft.subject) or (
        edited_body is not None and edited_body != draft.body
    )

    if edited:
        subject = (edited_subject if edited_subject is not None else draft.subject).strip()
        body = (edited_body if edited_body is not None else draft.body).strip()
        if not subject or not body:
            # The prototype's UI could approve an empty edit, and the empty text was sent
            # (part of issue B13).
            raise ValueError("an edited draft must have a non-empty subject and body")

        draft.status = "superseded"
        target = Draft(
            org_id=ctx.org_id,
            lead_id=draft.lead_id,
            kind=draft.kind,
            version=await next_version(session, draft.lead_id, draft.kind),
            subject=subject,
            body=body,
            claims=list(draft.claims),
            # Human-edited text has not been through the validator. Recorded plainly rather
            # than implied, so the audit trail does not overstate what was checked.
            validation={**dict(draft.validation), "human_edited": True},
            status="approved",
            prompt_version=draft.prompt_version,
            created_by=approved_by,
        )
        session.add(target)
        await session.flush()
    else:
        target.status = "approved"

    target.approved_by = approved_by
    target.approved_at = now

    await transition(
        session,
        org_id=ctx.org_id,
        lead_id=lead.id,
        to_state=LeadState.APPROVED,
        actor="user",
        actor_id=approved_by,
        data={"draft_id": str(target.id), "version": target.version, "edited": edited},
        now=now,
    )
    await session.flush()
    return target


async def reject(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    draft_id: uuid.UUID,
    rejected_by: str,
    feedback: str,
    now: datetime | None = None,
) -> Draft:
    """Reject with feedback, sending the lead back to `DRAFTED` for regeneration.

    The feedback is stored on the draft and fed to the next attempt, so a reviewer's objection
    actually reaches the model rather than being lost in a UI.
    """
    now = now or datetime.now(tz=UTC)
    draft = await _locked_draft(session, ctx, draft_id)

    if draft.status not in LIVE_STATUSES:
        raise ValueError(f"draft {draft_id} is {draft.status}, so it cannot be rejected")

    draft.status = "rejected"
    draft.feedback = feedback.strip()[:2000]

    lead = await session.get(Lead, draft.lead_id)
    if lead is None or lead.org_id != ctx.org_id:
        raise ValueError("the draft's lead is missing")

    if LeadState(lead.state) is LeadState.AWAITING_APPROVAL:
        await transition(
            session,
            org_id=ctx.org_id,
            lead_id=lead.id,
            to_state=LeadState.DRAFTED,
            actor="user",
            actor_id=rejected_by,
            data={"draft_id": str(draft.id), "feedback": draft.feedback},
            now=now,
        )

    await session.flush()
    return draft


async def review_queue(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID | None = None,
    draft_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
    include_failed: bool = True,
) -> list[ReviewItem]:
    """Drafts awaiting a decision, each with the evidence needed to judge it.

    One query joined to research rather than N+1 lookups: a reviewer opening the queue should
    not wait, and the evidence is the whole reason the queue is usable.

    **Oldest first, and paged.** FIFO so nothing waits indefinitely while newer drafts are
    judged ahead of it. Paged because each item carries a subject, a body and its hooks — a
    hundred of them is a response measured in hundreds of kilobytes and a screen nobody can
    work through. `after` is the `(created_at, id)` of the last row the caller saw.
    """
    statuses = list(LIVE_STATUSES) + (["validation_failed"] if include_failed else [])

    stmt = (
        select(Draft, Lead, Research)
        .join(Lead, Lead.id == Draft.lead_id)
        .outerjoin(Research, Research.lead_id == Draft.lead_id)
        .where(Draft.org_id == ctx.org_id, Draft.status.in_(statuses))
        # `id` breaks the tie, or two drafts created in the same millisecond page unstably and
        # one of them is served twice while the other is never seen.
        .order_by(Draft.created_at, Draft.id)
        .limit(_page_size(limit))
    )
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    if after is not None:
        moment, row_id = after
        stmt = stmt.where(
            tuple_(Draft.created_at, Draft.id) > tuple_(literal(moment), literal(row_id))
        )
    if draft_ids is not None:
        # Asking for specific drafts must return exactly those, not the first page of the
        # queue — otherwise a bulk approve silently skips anything that sorts later.
        stmt = stmt.where(Draft.id.in_(draft_ids)).limit(len(draft_ids) or 1)

    rows = (await session.execute(stmt)).all()
    return [
        ReviewItem(
            draft=draft,
            lead=lead,
            hooks=list(research.hooks) if research else [],
            research_confidence=research.confidence if research else 0.0,
            thin_research=research.thin if research else True,
        )
        for draft, lead, research in rows
    ]


async def review_queue_size(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID | None = None,
    include_failed: bool = True,
) -> int:
    """How many drafts are waiting, in total.

    A cursor answers "is there another page", which is not the question a reviewer has. "47
    drafts waiting" is what decides whether somebody starts now or after lunch, and a queue
    that only ever says "more" gives no sense of whether it is being cleared.

    One `COUNT` over an indexed status column, computed only for the page the operator is
    looking at -- not per row.
    """
    statuses = list(LIVE_STATUSES) + (["validation_failed"] if include_failed else [])
    stmt = (
        select(func.count())
        .select_from(Draft)
        .join(Lead, Lead.id == Draft.lead_id)
        .where(Draft.org_id == ctx.org_id, Draft.status.in_(statuses))
    )
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    return int(await session.scalar(stmt) or 0)


async def bulk_approve(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    draft_ids: list[uuid.UUID],
    approved_by: str,
    min_confidence: float = 0.6,
    now: datetime | None = None,
) -> tuple[list[uuid.UUID], dict[str, str]]:
    """Approve several drafts, refusing the ones that need a person.

    Returns `(approved, {draft_id: reason})`. A draft with thin research, a failed validation or
    confidence below the campaign threshold is **skipped rather than approved**: bulk approval
    exists to make the easy case fast, not to wave through the doubtful one.
    """
    approved: list[uuid.UUID] = []
    skipped: dict[str, str] = {}

    # Fetched by id rather than as a page of the queue: with a default page size, any draft
    # that sorted later would be reported as "not in the review queue" and silently skipped.
    items = {item.draft.id: item for item in await review_queue(session, ctx, draft_ids=draft_ids)}

    for draft_id in draft_ids:
        item = items.get(draft_id)
        if item is None:
            skipped[str(draft_id)] = "not in the review queue"
            continue
        if item.draft.status == "validation_failed":
            skipped[str(draft_id)] = "validation failed; read this one"
            continue
        if item.thin_research:
            skipped[str(draft_id)] = "research was thin; read this one"
            continue
        if item.research_confidence < min_confidence:
            skipped[str(draft_id)] = (
                f"research confidence {item.research_confidence:.2f} is below {min_confidence:.2f}"
            )
            continue

        await approve(session, ctx, draft_id=draft_id, approved_by=approved_by, now=now)
        approved.append(draft_id)

    return approved, skipped


async def _locked_draft(session: AsyncSession, ctx: OrgContext, draft_id: uuid.UUID) -> Draft:
    result = await session.execute(
        select(Draft).where(Draft.id == draft_id, Draft.org_id == ctx.org_id).with_for_update()
    )
    draft = result.scalar_one_or_none()
    if draft is None:
        raise LookupError(f"draft {draft_id} not found in this org")
    return draft
