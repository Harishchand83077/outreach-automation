"""Opportunities CRUD and status transitions (S19.2b, ADR-069).

Status changes go through POST /opportunities/{id}/status only.
The timeline (opportunity_events) is append-only — no PUT/PATCH/DELETE.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep
from app.db.models import Note, Opportunity, OpportunityEvent, Task
from app.db.opportunity import (
    MissingClosedReason,
    OpportunityNotFound,
    transition,
)
from app.domain.opportunity import (
    IllegalTransition,
    OpportunityStatus,
)

router = APIRouter(tags=["opportunities"])


def _opp(row: Opportunity) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "job_post_id": str(row.job_post_id) if row.job_post_id else None,
        "company_domain": row.company_domain,
        "title": row.title,
        "status": row.status,
        "strategy_note": row.strategy_note,
        "resume_version_id": str(row.resume_version_id) if row.resume_version_id else None,
        "closed_reason": row.closed_reason,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _event(row: OpportunityEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "occurred_at": row.occurred_at.isoformat(),
        "actor_kind": row.actor_kind,
        "event_kind": row.event_kind,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "payload": row.payload,
    }


def _task(row: Task) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "title": row.title,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "status": row.status,
        "source": row.source,
        "created_at": row.created_at.isoformat(),
    }


def _note(row: Note) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "body": row.body,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Today — declared before /{opportunity_id} so the literal path is not eaten
# ---------------------------------------------------------------------------

@router.get("/opportunities/today")
async def today(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """Tasks due today or overdue (and not done/dismissed), across all opportunities."""
    now = datetime.now(tz=UTC)
    rows = list(
        (
            await session.execute(
                select(Task)
                .where(
                    Task.org_id == user.org_id,
                    Task.status == "pending",
                    Task.due_at <= now,
                )
                .order_by(Task.due_at)
            )
        ).scalars()
    )
    return {"tasks": [_task(row) for row in rows], "as_of": now.isoformat()}


# ---------------------------------------------------------------------------
# Opportunities CRUD
# ---------------------------------------------------------------------------

@router.get("/opportunities")
async def list_opportunities(
    user: CurrentUserDep,
    session: SessionDep,
    status_filter: str | None = None,
) -> dict[str, Any]:
    q = select(Opportunity).where(Opportunity.org_id == user.org_id)
    if status_filter:
        q = q.where(Opportunity.status == status_filter)
    q = q.order_by(Opportunity.updated_at.desc())
    rows = list((await session.execute(q)).scalars())
    return {"opportunities": [_opp(row) for row in rows]}


@router.post("/opportunities", status_code=status.HTTP_201_CREATED)
async def create_opportunity(
    user: CurrentUserDep,
    session: SessionDep,
    title: Annotated[str, Body(embed=True)],
    job_post_id: Annotated[uuid.UUID | None, Body(embed=True)] = None,
    company_domain: Annotated[str | None, Body(embed=True)] = None,
    strategy_note: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    opp = Opportunity(
        org_id=user.org_id,
        job_post_id=job_post_id,
        company_domain=company_domain,
        title=title.strip(),
        status=OpportunityStatus.SAVED.value,
        strategy_note=strategy_note,
        created_at=now,
        updated_at=now,
    )
    session.add(opp)
    await session.flush()

    event = OpportunityEvent(
        org_id=user.org_id,
        opportunity_id=opp.id,
        occurred_at=now,
        actor_kind="user",
        event_kind="opportunity.created",
        payload={"title": title},
    )
    session.add(event)
    await session.commit()
    await session.refresh(opp)
    return {"opportunity": _opp(opp)}


@router.get("/opportunities/{opportunity_id}")
async def get_opportunity(
    opportunity_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(Opportunity).where(
                Opportunity.id == opportunity_id, Opportunity.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found.")
    return {"opportunity": _opp(row)}


@router.patch("/opportunities/{opportunity_id}")
async def update_opportunity(
    opportunity_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    title: Annotated[str | None, Body(embed=True)] = None,
    strategy_note: Annotated[str | None, Body(embed=True)] = None,
    company_domain: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(Opportunity).where(
                Opportunity.id == opportunity_id, Opportunity.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found.")
    if title is not None:
        row.title = title.strip()
    if strategy_note is not None:
        row.strategy_note = strategy_note
    if company_domain is not None:
        row.company_domain = company_domain
    row.updated_at = datetime.now(tz=UTC)
    await session.commit()
    await session.refresh(row)
    return {"opportunity": _opp(row)}


# ---------------------------------------------------------------------------
# Status transition — the only writer of opportunities.status
# ---------------------------------------------------------------------------

@router.post("/opportunities/{opportunity_id}/status")
async def change_status(
    opportunity_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    to_status: Annotated[str, Body(embed=True)],
    closed_reason: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    try:
        to = OpportunityStatus(to_status)
    except ValueError as exc:
        valid = ", ".join(s.value for s in OpportunityStatus)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status {to_status!r}. Valid: {valid}",
        ) from exc

    try:
        opp = await transition(
            session,
            org_id=user.org_id,
            opportunity_id=opportunity_id,
            to_status=to,
            actor_kind="user",
            actor_id=user.user_id,
            closed_reason=closed_reason,
        )
    except OpportunityNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found."
        ) from exc
    except (IllegalTransition, MissingClosedReason, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    await session.commit()
    await session.refresh(opp)
    return {"opportunity": _opp(opp)}


# ---------------------------------------------------------------------------
# Timeline (read-only)
# ---------------------------------------------------------------------------

@router.get("/opportunities/{opportunity_id}/events")
async def list_events(
    opportunity_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    # Verify the opportunity belongs to this org.
    exists = (
        await session.execute(
            select(Opportunity.id).where(
                Opportunity.id == opportunity_id, Opportunity.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found.")
    rows = list(
        (
            await session.execute(
                select(OpportunityEvent)
                .where(OpportunityEvent.opportunity_id == opportunity_id)
                .order_by(OpportunityEvent.occurred_at)
            )
        ).scalars()
    )
    return {"events": [_event(row) for row in rows]}


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@router.get("/opportunities/{opportunity_id}/tasks")
async def list_tasks(
    opportunity_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Task)
                .where(Task.opportunity_id == opportunity_id, Task.org_id == user.org_id)
                .order_by(Task.due_at.nullslast())
            )
        ).scalars()
    )
    return {"tasks": [_task(row) for row in rows]}


@router.post("/opportunities/{opportunity_id}/tasks", status_code=status.HTTP_201_CREATED)
async def add_task(
    opportunity_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    title: Annotated[str, Body(embed=True)],
    due_at: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    # Verify opportunity ownership.
    exists = (
        await session.execute(
            select(Opportunity.id).where(
                Opportunity.id == opportunity_id, Opportunity.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found.")

    due = None
    if due_at:
        try:
            due = datetime.fromisoformat(due_at)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="due_at must be an ISO 8601 datetime string",
            ) from exc

    now = datetime.now(tz=UTC)
    task = Task(
        org_id=user.org_id,
        opportunity_id=opportunity_id,
        title=title.strip(),
        due_at=due,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return {"task": _task(task)}


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    task_status: Annotated[str | None, Body(embed=True)] = None,
    title: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(Task).where(Task.id == task_id, Task.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
    if task_status is not None:
        if task_status not in ("pending", "done", "dismissed"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="status must be pending, done or dismissed",
            )
        row.status = task_status
    if title is not None:
        row.title = title.strip()
    row.updated_at = datetime.now(tz=UTC)
    await session.commit()
    await session.refresh(row)
    return {"task": _task(row)}


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

@router.post("/opportunities/{opportunity_id}/notes", status_code=status.HTTP_201_CREATED)
async def add_note(
    opportunity_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    body: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    exists = (
        await session.execute(
            select(Opportunity.id).where(
                Opportunity.id == opportunity_id, Opportunity.org_id == user.org_id
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found.")

    note = Note(
        org_id=user.org_id,
        opportunity_id=opportunity_id,
        body=body.strip(),
        created_by=user.user_id,
        created_at=datetime.now(tz=UTC),
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return {"note": _note(note)}
