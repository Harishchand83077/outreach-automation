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
from app.api.pagination import Cursor, clamp_limit
from app.db import drafts as draft_store

router = APIRouter(tags=["review"])


@router.get("/review/drafts")
async def review_queue(
    user: CurrentUserDep,
    session: SessionDep,
    campaign_id: Annotated[uuid.UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    """One page of the queue, oldest first, with the total still waiting.

    **Paged because each item is large.** A draft carries its subject, its body and its research
    hooks, so a hundred of them is a response measured in hundreds of kilobytes and a screen
    nobody can work through. The reviewer sees a few at a time and the count tells them how much
    is left.

    **`total` as well as `next_cursor`.** The cursor answers "is there more", which is not the
    question a reviewer has; "47 waiting" is what decides whether they start now.
    """
    size = clamp_limit(limit)
    position = Cursor.decode(cursor)
    items = await draft_store.review_queue(
        session,
        user.ctx,
        campaign_id=campaign_id,
        limit=size + 1,
        after=(position.created_at, position.row_id) if position else None,
    )
    has_more = len(items) > size
    items = items[:size]
    next_cursor = (
        Cursor(created_at=items[-1].draft.created_at, row_id=items[-1].draft.id).encode()
        if has_more and items
        else None
    )
    total = await draft_store.review_queue_size(session, user.ctx, campaign_id=campaign_id)
    return {
        "total": total,
        "next_cursor": next_cursor,
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
                # What this email carries, and whether it is on for this one. `None` for a
                # campaign with no uploaded file, which is how the UI knows not to draw the
                # control at all rather than drawing a disabled one.
                "attachment_filename": item.attachment_filename,
                "attached": item.attached,
                "validation": item.draft.validation,
            }
            for item in items
        ],
    }


#: The most drafts one request may approve. A request is not a way to start an unbounded amount
#: of work, and a reviewer who meant to select ten should not be able to approve ten thousand by
#: mistyping a filter.
MAX_BULK = 200


@router.post("/review/bulk-approve")
async def bulk_approve_drafts(
    user: CurrentUserDep,
    session: SessionDep,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Approve several drafts at once, refusing the ones that still need a person (E8).

    **This exists to make the easy case fast, not to wave the doubtful one through.** A draft
    whose validation failed, whose research was thin, or whose confidence is below the campaign
    threshold is **skipped**, and the reason comes back with it. That asymmetry is the whole
    design: the failure mode of a bulk button is that somebody clears a queue of a hundred
    without reading any of them, and the ones most worth reading are exactly the weak ones.

    Approving still sends nothing. The send loop re-checks suppression, the window, the daily
    cap and the lead's state before anything leaves.
    """
    raw = payload.get("draft_ids")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "draft_ids must be a non-empty list."
        )
    if len(raw) > MAX_BULK:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"At most {MAX_BULK} drafts can be approved in one request.",
        )
    try:
        draft_ids = [uuid.UUID(str(value)) for value in raw]
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "draft_ids must all be uuids."
        ) from exc

    threshold = payload.get("min_confidence")
    try:
        min_confidence = float(threshold) if threshold is not None else 0.6
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "min_confidence must be a number."
        ) from exc
    if not 0.0 <= min_confidence <= 1.0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "min_confidence must be between 0 and 1."
        )

    approved, skipped = await draft_store.bulk_approve(
        session,
        user.ctx,
        draft_ids=draft_ids,
        approved_by=user.user_id,
        min_confidence=min_confidence,
    )
    await session.commit()
    return {
        "approved": [str(draft_id) for draft_id in approved],
        # Keyed by draft id so the UI can mark the individual rows that still need reading,
        # rather than showing one number that says nothing about which.
        "skipped": skipped,
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


@router.post("/drafts/{draft_id}/attach")
async def set_draft_attachment(
    draft_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    attach: Annotated[bool, Body(embed=True)],
) -> dict[str, Any]:
    """Turn the campaign's attachment on or off for this one email.

    The campaign decides by default -- the same file for everybody is the point of the feature
    -- and this is the exception for the one lead where it does not fit: somebody who asked for
    the deck already, or a reply where sending a pitch document would be tone-deaf.

    **It is stored on the draft, not held in the browser**, because the send loop reads it
    minutes or days later, and because a decision a reviewer made has to be auditable. It is
    refused once the decision has been acted on: a draft that is `approved`, `sent` or
    `superseded` is no longer the thing that will be sent, and a control that silently did
    nothing would be worse than one that says no.
    """
    changed = await draft_store.set_attach(session, user.ctx, draft_id=draft_id, attach=attach)
    if changed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Draft not found.")
    if changed is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This draft has already been judged, so what it carries is settled. "
                "Reject it to make a new version."
            ),
        )
    await session.commit()
    return {"draft_id": str(draft_id), "attach": attach}
