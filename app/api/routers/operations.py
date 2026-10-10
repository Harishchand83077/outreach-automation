"""Suppressions, the mailbox and the admin views.

`ARCHITECTURE_TARGET.md` §14. Three small surfaces with one thing in common: they are where a
person intervenes in what the worker is doing, so each is deliberately boring and explicit.

The suppression list is the most consequential of the three. Adding to it is how "never contact
this person again" is expressed, and the send loop checks it immediately before every send. It is
idempotent on purpose — an opt-out may be detected twice, once by keyword and once by the
classifier — and removal is a separate, deliberate act rather than a side effect of anything.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUserDep, SessionDep
from app.api.pagination import clamp_limit
from app.db import ops, repo
from app.db.models import Job, LlmUsage, Mailbox, Message, Suppression

router = APIRouter(tags=["operations"])

#: Mirrors `ck_suppressions_reason`. Validated here rather than left to the database, because a
#: check-constraint violation surfaces as a 500 and tells the caller nothing about what to send.
SUPPRESSION_REASONS = ("opt_out", "bounce", "manual", "complaint")


@router.get("/suppressions")
async def list_suppressions(
    user: CurrentUserDep,
    session: SessionDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    rows = await repo.list_suppressions(user.ctx, session, limit=clamp_limit(limit))
    return {
        "suppressions": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "value": row.value,
                "reason": row.reason,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.post("/suppressions", status_code=status.HTTP_201_CREATED)
async def add_suppression(
    user: CurrentUserDep,
    session: SessionDep,
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    value = str(payload.get("value") or "").strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An address or domain is required.",
        )
    kind = str(payload.get("kind") or "email")
    if kind not in ("email", "domain"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="kind must be 'email' or 'domain'.",
        )
    reason = str(payload.get("reason") or "manual")
    if reason not in SUPPRESSION_REASONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"reason must be one of: {', '.join(SUPPRESSION_REASONS)}.",
        )

    row = await repo.add_suppression(user.ctx, session, value=value, reason=reason, kind=kind)
    await session.commit()
    return {"id": str(row.id), "kind": row.kind, "value": row.value, "reason": row.reason}


@router.delete("/suppressions/{suppression_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_suppression(
    suppression_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> None:
    """Deliberate and explicit: this is the only way somebody comes off the list."""
    row = await session.get(Suppression, suppression_id)
    if row is None or row.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    await session.delete(row)
    await session.commit()


@router.get("/mailbox")
async def get_mailbox(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The sending identity and today's position against the cap.

    No secrets: the SMTP password lives in the environment and is never read by the API, so
    there is nothing here that could be leaked by this endpoint.
    """
    mailbox = await session.scalar(
        select(Mailbox).where(Mailbox.org_id == user.org_id).order_by(Mailbox.created_at).limit(1)
    )
    if mailbox is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No mailbox is configured."
        )

    sent_today = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.org_id == user.org_id,
            Message.direction == "out",
            Message.status == "sent",
            Message.created_at >= func.date_trunc("day", func.now()),
        )
    )
    return {
        "id": str(mailbox.id),
        "from_name": mailbox.from_name,
        "from_email": mailbox.from_email,
        "reply_to": mailbox.reply_to,
        "daily_cap": mailbox.daily_cap,
        "paused": mailbox.paused,
        "pause_reason": mailbox.pause_reason,
        "warmup_started_on": (
            mailbox.warmup_started_on.isoformat() if mailbox.warmup_started_on else None
        ),
        "next_send_at": mailbox.next_send_at.isoformat() if mailbox.next_send_at else None,
        "sent_today": int(sent_today or 0),
    }


@router.post("/mailbox/{mailbox_id}/pause")
async def pause_mailbox(
    mailbox_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Stop this mailbox sending. Takes effect on the send loop's next cycle."""
    mailbox = await _owned_mailbox(session, user, mailbox_id)
    mailbox.paused = True
    mailbox.pause_reason = str((payload or {}).get("reason") or "paused by a person")
    await session.commit()
    return {"id": str(mailbox.id), "paused": True, "pause_reason": mailbox.pause_reason}


@router.post("/mailbox/{mailbox_id}/resume")
async def resume_mailbox(
    mailbox_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    mailbox = await _owned_mailbox(session, user, mailbox_id)
    mailbox.paused = False
    mailbox.pause_reason = None
    await session.commit()
    return {"id": str(mailbox.id), "paused": False}


async def _owned_mailbox(
    session: SessionDep, user: CurrentUserDep, mailbox_id: uuid.UUID
) -> Mailbox:
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None or mailbox.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found.")
    return mailbox


@router.get("/admin/usage")
async def llm_usage(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """What the models have cost today, per entry. The free tiers are a real constraint."""
    rows = await session.execute(
        select(
            LlmUsage.entry_name,
            func.count(),
            func.coalesce(func.sum(LlmUsage.tokens_in + LlmUsage.tokens_out), 0),
        )
        .where(
            LlmUsage.org_id == user.org_id,
            LlmUsage.at >= func.date_trunc("day", func.now()),
        )
        .group_by(LlmUsage.entry_name)
    )
    return {
        "today": [
            {"entry": entry, "requests": int(requests), "tokens": int(tokens)}
            for entry, requests, tokens in rows.all()
        ]
    }


@router.get("/admin/jobs")
async def jobs(
    user: CurrentUserDep,
    session: SessionDep,
    job_status: Annotated[str, Query(alias="status")] = "dead",
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    """Jobs needing a person — dead by default, the only list worth a daily glance."""
    rows = await session.execute(
        select(Job)
        .where(Job.org_id == user.org_id, Job.status == job_status)
        .order_by(Job.created_at.desc())
        .limit(clamp_limit(limit))
    )
    return {
        "jobs": [
            {
                "id": str(job.id),
                "kind": job.kind,
                "status": job.status,
                "attempts": job.attempts,
                "last_error": job.last_error,
                "lead_id": str(job.lead_id) if job.lead_id else None,
                "created_at": job.created_at.isoformat(),
            }
            for job in rows.scalars()
        ]
    }


@router.get("/admin/needs-attention")
async def needs_attention(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The three counts worth a daily look, in one call so the UI need not poll three.

    `unknown` sends are the important one: a send whose outcome could not be determined, which
    ADR-002 deliberately leaves for a person rather than retrying into a possible duplicate.
    """
    unknown = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.org_id == user.org_id, Message.status == "unknown")
    )
    dead = await session.scalar(
        select(func.count()).select_from(Job).where(Job.org_id == user.org_id, Job.status == "dead")
    )
    paused = await session.scalar(
        select(func.count())
        .select_from(Mailbox)
        .where(Mailbox.org_id == user.org_id, Mailbox.paused.is_(True))
    )
    return {
        "unknown_sends": int(unknown or 0),
        "dead_jobs": int(dead or 0),
        "paused_mailboxes": int(paused or 0),
    }


@router.get("/admin/alerts")
async def alerts(
    user: CurrentUserDep,
    session: SessionDep,
    unacked: Annotated[bool, Query()] = False,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    """Alerts for this org, plus the system-wide ones (§18).

    A system alert — a stalled worker, an unreachable database — carries no `org_id` because it
    belongs to no tenant and stops everyone's work, so it is listed for whoever looks first.
    """
    rows = await ops.list_alerts(
        session, org_id=user.org_id, unacked_only=unacked, limit=clamp_limit(limit)
    )
    return {
        "alerts": [
            {
                "id": str(row.id),
                "severity": row.severity,
                "kind": row.kind,
                "message": row.message,
                "detail": row.detail or {},
                "scope": "org" if row.org_id else "system",
                "created_at": row.created_at.isoformat(),
                "acked_at": row.acked_at.isoformat() if row.acked_at else None,
            }
            for row in rows
        ]
    }


@router.post("/admin/alerts/{alert_id}/ack")
async def ack_alert(
    user: CurrentUserDep, session: SessionDep, alert_id: uuid.UUID
) -> dict[str, Any]:
    """Acknowledge an alert, so it stops being counted and can be raised again if it recurs."""
    try:
        row = await ops.ack_alert(
            session, org_id=user.org_id, alert_id=alert_id, user_id=user.user_id
        )
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such alert.") from exc
    return {"id": str(row.id), "acked_at": row.acked_at.isoformat() if row.acked_at else None}


@router.get("/admin/worker")
async def worker(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """What the worker last said about itself.

    Authenticated, unlike `/ready`: this returns the per-task detail, which says how the inside
    of the system is put together and how it is currently failing.
    """
    status_row = await ops.latest_heartbeat(session)
    if status_row is None:
        return {"running": False, "detail": "The worker has never recorded a heartbeat."}
    return {
        "running": not status_row.stale,
        "worker_id": status_row.worker_id,
        "started_at": status_row.started_at.isoformat(),
        "last_seen_at": status_row.last_seen_at.isoformat(),
        "seen_seconds_ago": status_row.age_seconds,
        "info": status_row.info,
    }
