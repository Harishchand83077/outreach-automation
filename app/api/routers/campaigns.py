"""Campaigns and their leads.

`ARCHITECTURE_TARGET.md` §14. Every list is cursor-paginated and every list row is a *summary*.

That second rule is issue C7. The prototype's `GET /api/leads` returned each lead's full
insights, draft body and reply text, and the dashboard polled it every 2.5 seconds — so the whole
contents of the pipeline crossed the wire continuously, to anyone who asked, because there was no
authentication either. Here a list row carries what a list needs to render: who, where they are,
when they next move. The bodies live behind `GET /leads/{id}`, which is one lead at a time and
therefore one lead's worth of exposure.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, require_role
from app.api.pagination import Cursor, build_page, clamp_limit
from app.db.models import Campaign, Draft, Event, Lead, Message, Research
from app.db.repo import OrgContext
from app.services.privacy import erase_lead

router = APIRouter(tags=["campaigns"])


def campaign_summary(campaign: Campaign) -> dict[str, Any]:
    persona = campaign.persona or {}
    return {
        "id": str(campaign.id),
        "name": campaign.name,
        "status": campaign.status,
        "paused": campaign.paused,
        "mailbox_id": str(campaign.mailbox_id) if campaign.mailbox_id else None,
        # Whether a persona exists is what the UI needs to know; its contents are not list data.
        "has_persona": bool(persona),
        "sender_name": persona.get("sender_name"),
        "created_at": campaign.created_at.isoformat(),
    }


def lead_summary(lead: Lead) -> dict[str, Any]:
    """What a list row needs. Deliberately no draft, insights or reply text (C7)."""
    return {
        "id": str(lead.id),
        "email": lead.email_norm,
        "full_name": lead.full_name,
        "company_name": lead.company_name,
        "job_title": lead.job_title,
        "state": lead.state,
        "next_action_at": lead.next_action_at.isoformat() if lead.next_action_at else None,
        "created_at": lead.created_at.isoformat(),
    }


@router.get("/campaigns")
async def list_campaigns(
    user: CurrentUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    size = clamp_limit(limit)
    stmt = (
        select(Campaign)
        .where(Campaign.org_id == user.org_id)
        .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        .limit(size + 1)
    )
    position = Cursor.decode(cursor)
    if position is not None:
        stmt = stmt.where(Campaign.created_at < position.created_at)

    rows = list((await session.execute(stmt)).scalars())
    return build_page(rows, size, campaign_summary).body("campaigns")


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    campaign = await _owned_campaign(session, user, campaign_id)
    counts = await _state_counts(session, user, campaign_id)
    return {
        **campaign_summary(campaign),
        "persona": campaign.persona,
        "settings": campaign.settings,
        "lead_counts": counts,
    }


@router.get("/campaigns/{campaign_id}/leads")
async def list_leads(
    campaign_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    state: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    await _owned_campaign(session, user, campaign_id)
    size = clamp_limit(limit)

    stmt = (
        select(Lead)
        .where(Lead.org_id == user.org_id, Lead.campaign_id == campaign_id)
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(size + 1)
    )
    if state:
        stmt = stmt.where(Lead.state == state)
    if q:
        # Matched against identity only. Searching bodies would turn a search box into an export
        # of everything anybody ever wrote.
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            Lead.email_norm.ilike(pattern)
            | Lead.full_name.ilike(pattern)
            | Lead.company_name.ilike(pattern)
        )
    position = Cursor.decode(cursor)
    if position is not None:
        stmt = stmt.where(Lead.created_at < position.created_at)

    rows = list((await session.execute(stmt)).scalars())
    return build_page(rows, size, lead_summary).body("leads")


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """One lead, with its bodies and its timeline. One lead's worth of exposure at a time."""
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.org_id != user.org_id:
        # 404 rather than 403: confirming that an id exists in another org is itself a leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found.")

    research = await session.scalar(
        select(Research)
        .where(Research.org_id == user.org_id, Research.lead_id == lead.id)
        .order_by(Research.created_at.desc())
        .limit(1)
    )
    drafts = list(
        (
            await session.execute(
                select(Draft)
                .where(Draft.org_id == user.org_id, Draft.lead_id == lead.id)
                .order_by(Draft.created_at.desc())
                .limit(10)
            )
        ).scalars()
    )
    messages = list(
        (
            await session.execute(
                select(Message)
                .where(Message.org_id == user.org_id, Message.lead_id == lead.id)
                .order_by(Message.created_at)
                .limit(50)
            )
        ).scalars()
    )
    timeline = list(
        (
            await session.execute(
                select(Event)
                .where(Event.org_id == user.org_id, Event.lead_id == lead.id)
                .order_by(Event.at.desc())
                .limit(50)
            )
        ).scalars()
    )

    return {
        **lead_summary(lead),
        "notes": lead.notes,
        "linkedin_url": lead.linkedin_url,
        "company_url": lead.company_url,
        "research": {
            "hooks": list(research.hooks or []) if research else [],
            "confidence": research.confidence if research else 0.0,
            "thin": research.thin if research else True,
        },
        "drafts": [
            {
                "id": str(draft.id),
                "kind": draft.kind,
                "version": draft.version,
                "status": draft.status,
                "subject": draft.subject,
                "body": draft.body,
                "created_at": draft.created_at.isoformat(),
            }
            for draft in drafts
        ],
        "messages": [
            {
                "id": str(message.id),
                "direction": message.direction,
                "kind": message.kind,
                "status": message.status,
                "subject": message.subject,
                "body_text": message.body_text,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ],
        "timeline": [
            {
                "type": event.type,
                "at": event.at.isoformat(),
                "actor": event.actor,
                "data": event.data,
            }
            for event in timeline
        ],
    }


async def _owned_campaign(
    session: SessionDep, user: CurrentUserDep, campaign_id: uuid.UUID
) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    return campaign


async def _state_counts(
    session: SessionDep, user: CurrentUserDep, campaign_id: uuid.UUID
) -> dict[str, int]:
    from sqlalchemy import func

    rows = await session.execute(
        select(Lead.state, func.count())
        .where(Lead.org_id == user.org_id, Lead.campaign_id == campaign_id)
        .group_by(Lead.state)
    )
    return {state: count for state, count in rows.all()}


@router.delete("/leads/{lead_id}", status_code=status.HTTP_200_OK)
async def erase_lead_endpoint(
    lead_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    keep_suppressed: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Erase one lead and everything personal attached to it (§17, issue D6).

    This is the data-subject request route, and it is a real delete rather than a flag: the
    lead, its drafts, its messages, its research and its events all go.

    **`keep_suppressed` defaults to true and should almost always stay true.** It leaves a
    hashed suppression behind so the address stays unreachable without being stored. Without
    it, erasing somebody who had opted out makes them contactable again on the next import of
    the same sheet — a privacy request producing the exact harm it was meant to prevent.

    Restricted to `admin` and above. It is the one irreversible action in the API, and a
    reviewer who may approve drafts has no reason to need it.
    """
    ctx = OrgContext(org_id=user.org_id, actor_id=user.email or user.user_id)
    try:
        result = await erase_lead(session, ctx, lead_id, keep_suppressed=keep_suppressed)
    except LookupError as exc:
        # 404 rather than 403, as everywhere else: confirming an id exists elsewhere is a leak.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.") from exc

    return {
        "erased": True,
        "lead_id": str(result.lead_id),
        "rows_removed": result.rows,
        "detail": {
            "drafts": result.drafts,
            "messages": result.messages,
            "research": result.research,
            "events": result.events,
        },
        "suppression_kept": result.suppression_kept,
    }
