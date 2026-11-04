"""The watchlist and the signals it produces (F11, ADR-049).

Four things a person does: watch a company, see what has happened at the ones they watch, act on
one, and stop watching.

**No route here creates a lead, drafts anything or sends anything**, and none of them is
networked: a signal arrives from the daily poll the worker already runs, so a refresh of this
screen costs a query and nothing else. Acting on a signal means going to the lead it matched, or
pressing the contact-lookup button on the Job hunt screen, which spends the user's own credit
and is a separate, explicit act.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, require_role
from app.db.models import SIGNAL_KINDS, SIGNAL_STATES, Lead, SignalEvent, WatchlistEntry
from app.services.signals import intake, watchlist

router = APIRouter(tags=["signals"])


def _entry(row: WatchlistEntry) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "domain": row.domain,
        "label": row.label,
        "ats_source": row.ats_source,
        "ats_slug": row.ats_slug,
        "campaign_id": str(row.campaign_id) if row.campaign_id else None,
        "paused": row.paused,
        # The one field that tells "nothing has happened at this company" from "this was never
        # wired up", which is the same job `booking_webhooks.last_used_at` does.
        "last_signal_at": row.last_signal_at.isoformat() if row.last_signal_at else None,
        "created_at": row.created_at.isoformat(),
    }


def _signal(row: SignalEvent, *, lead_name: str | None = None) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "domain": row.domain,
        "kind": row.kind,
        "source": row.source,
        "title": row.title,
        "url": row.url,
        "state": row.state,
        "lead_id": str(row.lead_id) if row.lead_id else None,
        # So the screen can say who to write to rather than printing an id, and say plainly when
        # there is nobody -- which is the case the contact lookup exists for.
        "lead_name": lead_name,
        "job_post_id": str(row.job_post_id) if row.job_post_id else None,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/watchlist")
async def list_watchlist(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The companies this workspace watches."""
    rows = await watchlist.entries(session, user.ctx)
    return {
        "watchlist": [_entry(row) for row in rows],
        "sources": list(watchlist.POLLABLE),
        "note": (
            "A watched company's new job postings become a reason to write to them. Nothing is "
            "drafted or sent because of a signal: it is an entry on a list you read."
        ),
    }


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
async def watch_a_company(
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    session: SessionDep,
    domain: Annotated[str, Body(embed=True)],
    label: Annotated[str | None, Body(embed=True)] = None,
    ats_source: Annotated[str | None, Body(embed=True)] = None,
    ats_slug: Annotated[str | None, Body(embed=True)] = None,
    campaign_id: Annotated[uuid.UUID | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Watch a company, and poll its board if it has one we can read.

    Idempotent on the domain, for the reason `POST /mailboxes` is idempotent on the address: the
    domain is the company's identity here, so a second attempt is somebody correcting a label or
    adding a board rather than naming a different company.
    """
    try:
        row, created = await watchlist.add(
            session,
            user.ctx,
            domain=domain,
            label=label,
            ats_source=ats_source,
            ats_slug=ats_slug,
            campaign_id=campaign_id,
        )
    except watchlist.WatchRefused as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {
        "watch": _entry(row),
        "created": created,
        "note": (
            "Its board will be read once a day."
            if row.ats_source
            else "No board given, so nothing will produce signals for this company yet."
        ),
    }


@router.post("/watchlist/{entry_id}/pause")
async def pause_a_watch(
    entry_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    session: SessionDep,
    paused: Annotated[bool, Body(embed=True)] = True,
) -> dict[str, Any]:
    """Stop producing signals for a company without forgetting it or its history."""
    row = await watchlist.set_paused(session, user.ctx, entry_id=entry_id, paused=paused)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such watch.")
    await session.commit()
    return {"watch": _entry(row)}


@router.delete("/watchlist/{entry_id}")
async def stop_watching(
    entry_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    session: SessionDep,
) -> dict[str, Any]:
    """Forget a company. Its signals stay, and so does the board in the poll.

    The signals stay because each one carries its own domain and the foreign key is
    `SET NULL`: deleting the record that a company was hiring in March would quietly rewrite a
    funnel. The board stays because it may be polled for job hunting too, and a watch is not its
    owner.
    """
    removed = await watchlist.remove(session, user.ctx, entry_id=entry_id)
    await session.commit()
    return {
        "removed": removed,
        "note": "Signals already recorded are kept, and the board stays in the daily poll.",
    }


@router.get("/signals")
async def list_signals(
    user: CurrentUserDep,
    session: SessionDep,
    state: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """What has happened at the companies this workspace watches.

    Not networked: signals arrive from the daily poll, so this is a read of our own rows.
    """
    query = select(SignalEvent).where(SignalEvent.org_id == user.org_id)
    if state:
        query = query.where(SignalEvent.state == state)
    rows = list(
        (
            await session.scalars(
                query.order_by(SignalEvent.created_at.desc()).limit(max(1, min(limit, 200)))
            )
        ).all()
    )

    names: dict[uuid.UUID, str] = {}
    lead_ids = [row.lead_id for row in rows if row.lead_id]
    if lead_ids:
        for lead in (
            await session.scalars(
                select(Lead).where(Lead.org_id == user.org_id, Lead.id.in_(lead_ids))
            )
        ).all():
            names[lead.id] = lead.full_name or lead.email_norm

    return {
        "signals": [
            _signal(row, lead_name=names.get(row.lead_id) if row.lead_id else None) for row in rows
        ],
        "kinds": list(SIGNAL_KINDS),
        "states": list(SIGNAL_STATES),
        # The number worth opening the screen for: a company with a reason to be written to and
        # nobody to write to is what the contact lookup is for, and a count makes that visible.
        "without_a_lead": sum(1 for row in rows if row.lead_id is None),
    }


@router.post("/signals/{signal_id}/state")
async def move_a_signal(
    signal_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    state: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    """Mark a signal acted on or dismissed.

    There is no way back to `new`: "I have looked at this" is not something somebody un-does,
    and a list that can be reset is one nobody trusts to shrink.
    """
    if state not in SIGNAL_STATES or state == "new":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A signal can be moved to acted or dismissed.",
        )
    row = await intake.set_state(session, user.ctx, signal_id=signal_id, state=state)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such signal.")
    await session.commit()
    return {"signal": _signal(row)}
