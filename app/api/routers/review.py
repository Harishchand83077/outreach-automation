"""The review queue: the human gate before anything is sent.

`ARCHITECTURE_TARGET.md` §13 and §14. Every first email and every reply passes through here
(`approval_mode = review_all`), so these three endpoints are the point at which a person, not a
model, decides that something leaves the building.

Two properties worth stating:

- **Approving does not send.** It makes a lead eligible; the send loop then re-checks
  suppression, the window, the cap and the lead's state before anything goes. An approval made
  at 4pm on Friday for a lead who opts out at 5pm sends nothing.
- **An edit creates a new version.** What the model produced survives next to what the human
  decided to send, which is the only way to tell later whether the drafting is actually any good.

The queue carries the evidence beside each draft — the research hooks and their confidence —
because a reviewer who cannot see what a claim rests on cannot judge it, and will approve.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, status

from app.api.deps import CurrentUserDep, SessionDep
from app.api.pagination import clamp_limit
from app.db import drafts as draft_store

router = APIRouter(tags=["review"])


@router.get("/review/drafts")
async def review_queue(
    user: CurrentUserDep,
    session: SessionDep,
    campaign_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    items = await draft_store.review_queue(
        session, user.ctx, campaign_id=campaign_id, limit=clamp_limit(limit)
    )
    return {
        "drafts": [
            {
                "draft_id": str(item.draft.id),
                "lead_id": str(item.lead.id),
                "kind": item.draft.kind,
                "version": item.draft.version,
                "status": item.draft.status,
                "subject": item.draft.subject,
                "body": item.draft.body,
                "lead": {
                    "email": item.lead.email_norm,
                    "full_name": item.lead.full_name,
                    "company_name": item.lead.company_name,
                },
                # The evidence, beside the draft. Without it a reviewer cannot check a claim.
                "hooks": item.hooks,
                "research_confidence": item.research_confidence,
                "thin_research": item.thin_research,
                "validation": item.draft.validation,
            }
            for item in items
        ]
    }


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Approve, optionally with edits. Makes the lead eligible; sends nothing itself."""
    payload = payload or {}
    try:
        draft = await draft_store.approve(
            session,
            user.ctx,
            draft_id=draft_id,
            approved_by=user.user_id,
            edited_subject=_text(payload.get("subject")),
            edited_body=_text(payload.get("body")),
        )
        await session.commit()
    except LookupError as exc:
        # The repository raises this for a draft that does not exist *in this org*, which is the
        # tenancy boundary on a write. 404 for the same reason as everywhere else: confirming
        # that an id is real elsewhere is itself a leak.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found."
        ) from exc
    except ValueError as exc:
        await session.rollback()
        # A draft that is already sent, rejected or superseded is a conflict, not a server
        # error: the reviewer is looking at a stale queue and should reload.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return {"draft_id": str(draft.id), "status": draft.status, "version": draft.version}


@router.post("/drafts/{draft_id}/reject")
async def reject_draft(
    draft_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Reject with feedback, which is fed to the next attempt rather than lost in the UI."""
    payload = payload or {}
    feedback = _text(payload.get("feedback")) or ""
    if not feedback.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Rejecting needs a reason: it is what the next draft is generated from.",
        )

    try:
        draft = await draft_store.reject(
            session,
            user.ctx,
            draft_id=draft_id,
            rejected_by=user.user_id,
            feedback=feedback,
        )
        await session.commit()
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found."
        ) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return {"draft_id": str(draft.id), "status": draft.status}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
