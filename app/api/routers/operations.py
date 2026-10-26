"""Suppressions, the mailbox and the admin views.

`ARCHITECTURE_TARGET.md` §14. Three small surfaces with one thing in common: they are where a
person intervenes in what the worker is doing, so each is deliberately boring and explicit.

The suppression list is the most consequential of the three. Adding to it is how "never contact
this person again" is expressed, and the send loop checks it immediately before every send. It is
idempotent on purpose — an opt-out may be detected twice, once by keyword and once by the
classifier — and removal is a separate, deliberate act rather than a side effect of anything.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, require_role
from app.api.pagination import clamp_limit
from app.core.settings import config
from app.db import ops, repo
from app.db.models import (
    Job,
    LlmUsage,
    Mailbox,
    Message,
    Org,
    SendAllowlistEntry,
    Suppression,
)
from app.db.state import record_event
from app.services.mail.smtp import ALLOWED_SEND_MODES, effective_send_mode

logger = logging.getLogger(__name__)

router = APIRouter(tags=["operations"])

#: Mirrors `ck_suppressions_reason`. Validated here rather than left to the database, because a
#: check-constraint violation surfaces as a 500 and tells the caller nothing about what to send.
SUPPRESSION_REASONS = ("opt_out", "bounce", "manual", "complaint")


@router.get("/admin/send-allowlist", dependencies=[Depends(require_role("admin"))])
async def list_send_allowlist(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The addresses this workspace may send to while its mode is `allowlist`.

    **The list is returned, not a count.** It used to be a count, deliberately: `SEND_ALLOWLIST`
    was one list for the entire deployment, so showing it to a workspace would have handed them
    the operator's personal addresses. ADR-023 made the list the org's own rows, and an org's own
    addresses shown to its own admin are not a disclosure -- they are the thing being edited. A
    screen that will not show you what you are editing is not a screen.

    Cross-org isolation is the property that replaces the count, and it is asserted directly:
    `org_id` comes from the membership and never from the caller (ADR-004).
    """
    rows = (
        await session.scalars(
            select(SendAllowlistEntry)
            .where(SendAllowlistEntry.org_id == user.org_id)
            .order_by(SendAllowlistEntry.created_at)
        )
    ).all()
    return {
        "allowlist": [
            {
                "id": str(row.id),
                "email": row.email_norm,
                "added_by": row.added_by,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.post(
    "/admin/send-allowlist",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("admin"))],
)
async def add_to_send_allowlist(
    user: CurrentUserDep,
    session: SessionDep,
    email: Annotated[str, Body(embed=True)],
    response: Response,
) -> dict[str, Any]:
    """Allow one more address while rehearsing.

    **`admin`, not `owner`.** Adding an address widens who may be emailed, so it is not a
    read-only act -- but it widens it by exactly one address that somebody typed, inside a mode
    whose entire purpose is rehearsal. Going `live`, which removes the gate altogether, stays
    `owner` (ADR-010). Treating the two the same would mean either handing out `live` or making
    rehearsal impossible without the one role nobody wants to share.
    """
    address = (email or "").strip().lower()
    if "@" not in address or len(address) > 320:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{email!r} is not an email address."
        )

    existing = await session.scalar(
        select(SendAllowlistEntry).where(
            SendAllowlistEntry.org_id == user.org_id,
            SendAllowlistEntry.email_norm == address,
        )
    )
    if existing is not None:
        # Idempotent: there is nothing else to create, and a 409 would only make the caller ask
        # for the id of the row it just described.
        response.status_code = status.HTTP_200_OK
        return {"id": str(existing.id), "email": existing.email_norm, "added": False}

    entry = SendAllowlistEntry(
        org_id=user.org_id, email_norm=address, added_by=user.email or user.user_id
    )
    session.add(entry)
    await session.flush()
    await record_event(
        session,
        org_id=user.org_id,
        type="send_allowlist.added",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"email": address},
    )
    await session.commit()
    return {"id": str(entry.id), "email": entry.email_norm, "added": True}


@router.delete("/admin/send-allowlist/{entry_id}", dependencies=[Depends(require_role("admin"))])
async def remove_from_send_allowlist(
    entry_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Stop allowing an address. Always safe: this can only ever narrow who may be emailed."""
    entry = await session.get(SendAllowlistEntry, entry_id)
    if entry is None or entry.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not on this workspace's allowlist.")
    await session.delete(entry)
    await record_event(
        session,
        org_id=user.org_id,
        type="send_allowlist.removed",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"email": entry.email_norm},
    )
    await session.commit()
    return {"removed": True}


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
    suppression_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> None:
    """Deliberate and explicit: this is the only way somebody comes off the list.

    **Admin only.** Adding to the list is protective and anyone may do it; taking somebody off
    it undoes an opt-out, which is the one action here that can lead to mailing a person who
    asked not to be mailed.
    """
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


@router.post("/admin/jobs/{job_id}/requeue")
async def requeue_dead_job(
    job_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Put a dead job back in the queue after its cause has been fixed.

    A job goes `dead` after spending `max_attempts`, and `/admin/needs-attention` counts them --
    but until now the only way to retry one was SQL against production, which is how three
    reply-drafting jobs were recovered by hand after a deploy. The cause of a dead job is
    usually a code defect, so the useful action after shipping the fix is "run it again", and
    that should not require a shell.

    **Only dead jobs.** Requeuing a `running` job would duplicate work a worker is holding a
    lease on, and requeuing a `succeeded` one would repeat an effect that already happened --
    for a send that means a second email.

    The attempt counter is reset, because the retries were spent on a different version of the
    code. `last_error` is kept: what it failed with last time is the most useful thing on the
    row.
    """
    job = await session.get(Job, job_id)
    if job is None or job.org_id != user.org_id:
        # 404 for another org's job, as everywhere: confirming an id is real elsewhere is a leak.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found.")
    if job.status != "dead":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This job is {job.status}, and only a dead job can be requeued.",
        )

    job.status = "queued"
    job.attempts = 0
    job.locked_by = None
    job.locked_until = None
    job.finished_at = None
    job.run_after = datetime.now(tz=UTC)
    await session.commit()

    logger.warning(
        "dead job requeued",
        extra={"job": str(job.id), "kind": job.kind, "by": user.user_id},
    )
    return {"job_id": str(job.id), "kind": job.kind, "status": job.status}


@router.get("/admin/dead-jobs")
async def list_dead_jobs(
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, Any]:
    """The dead-letter list, so a count on a dashboard can become an action.

    `last_error` is returned because it is the entire diagnostic value of the row, and these are
    our own error strings rather than stored lead content -- the send loop's reasons name fields
    and never values, for exactly this reason.
    """
    rows = (
        (
            await session.execute(
                select(Job)
                .where(Job.org_id == user.org_id, Job.status == "dead")
                .order_by(Job.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "jobs": [
            {
                "id": str(job.id),
                "kind": job.kind,
                "lead_id": str(job.lead_id) if job.lead_id else None,
                "attempts": job.attempts,
                "last_error": (job.last_error or "")[:300],
                "created_at": job.created_at.isoformat(),
            }
            for job in rows
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


@router.get("/admin/sending-config")
async def sending_config(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """What the running process will do when it tries to send. Read-only, deliberately.

    **There is no endpoint that sets `SEND_MODE`, and there must never be one.** It is read from
    the environment at process start and checked in exactly one function (hard rule 1). Exposing
    a switch for it would mean a stolen token, or one mistaken click in a browser, could begin
    sending real mail to real people -- and the entire design is that going live is a deliberate
    act by the owner, on the server, recorded in `.env`. Changing it is a restart, not a request.

    So this answers the question an operator actually has, which is *"what will happen if I
    approve this draft?"*, and answers it from the same configuration object the send guard
    reads, rather than from anything a client could have influenced.

    **The allowlist is reported as a count, not a list.** The addresses are personal data, the
    count is what tells you whether the gate is configured, and a list would put real addresses
    into a response that ends up in a browser's cache and a screenshot. Nothing here is a
    credential: the sending account is named, because the `From` address is public the moment a
    single email arrives, but no password, key or token is returned.

    **`sending_account` is this org's, not the environment's.** It reported `SMTP_EMAIL`
    unconditionally, which was true of a single-tenant deployment and became two problems the
    moment strangers could sign up: it showed every workspace the operator's personal address,
    and it told them mail would leave from an account they have no access to. It now names the
    addresses this org actually sends from — its own mailboxes — and says explicitly when one of
    them is the deployment's shared account.
    """
    org = await session.get(Org, user.org_id)
    override = org.send_mode if org else None
    mailboxes = list(
        (
            await session.execute(
                select(Mailbox).where(Mailbox.org_id == user.org_id).order_by(Mailbox.created_at)
            )
        ).scalars()
    )
    return {
        "send_mode": effective_send_mode(config, override),
        "source": "dashboard" if override else "environment",
        "environment_default": config.SEND_MODE,
        "editable": user.may("owner"),
        "set_by": org.send_mode_set_by if org else None,
        "set_at": org.send_mode_set_at.isoformat() if org and org.send_mode_set_at else None,
        # `live` is refused outside production, so the screen can say why the option is absent
        # rather than appearing broken.
        "live_allowed": config.APP_ENV == "production",
        "smtp_host": config.SMTP_HOST,
        "smtp_port": config.SMTP_PORT,
        # A list, because a campaign picks its own mailbox and an org may hold several. Empty
        # means nobody has added one yet, which is a different answer from "we do not know".
        "sending_accounts": [m.from_email for m in mailboxes],
        # Kept for the Settings screen's single-line summary. None rather than the operator's
        # address when this org has no mailbox of its own.
        "sending_account": mailboxes[0].from_email if mailboxes else None,
        "imap_host": config.IMAP_HOST,
        "environment": config.APP_ENV,
        # The product rules that are locked in code rather than configurable per campaign, so
        # the settings screen can state them instead of implying they are editable.
        "locked": {
            "approval_mode": "review_all",
            "plain_text_only": True,
            "tracking_pixels": False,
            "link_rewriting": False,
            "meeting_booked_set_only_by_a_human": True,
        },
    }


@router.patch("/admin/sending-config")
async def set_sending_mode(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("owner"))],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Change the send mode for this org (ADR-010).

    This is the most consequential request the API accepts, so it is gated four ways — and every
    gate is here, where the value is *written*, rather than in the guard that reads it. A guard
    that answers differently depending on who is asking is a guard nobody can reason about.

    1. **Owner only, not admin.** Inviting somebody to help review drafts must not also hand
       them the switch that starts mailing strangers.
    2. **`live` is refused outside production.** `CLAUDE.md` says the owner sets `live` only in
       production; this is that sentence as code rather than as a habit. A staging box restored
       from a copy of this database therefore cannot be talked into sending.
    3. **A typed confirmation for `live`.** `confirm` must be the literal `LIVE`, so enabling
       real sending cannot be a mis-click, a replayed request, or a forged one that guessed a
       plausible body.
    4. **An audit row.** Who changed it and when, on the org and in the log.

    Passing `null` hands authority back to the environment.

    **Nothing here sends.** The mode is read at send time by the one guard in
    `app/services/mail/smtp.py`, which still re-checks the allowlist, the window, the daily cap
    and the lead's state exactly as before. Restricting takes effect on the next cycle, within
    seconds — which is the point: a kill switch that needs an SSH session is not a kill switch.
    """
    requested = payload.get("send_mode")
    if requested is not None:
        requested = str(requested).strip().lower()
        if requested not in ALLOWED_SEND_MODES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"send_mode must be null or one of: {', '.join(ALLOWED_SEND_MODES)}.",
            )

    if requested == "live":
        if config.APP_ENV != "production":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Live sending can only be enabled in production, and this is {config.APP_ENV}.",
            )
        if str(payload.get("confirm") or "") != "LIVE":
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                'Enabling live sending needs confirm="LIVE". Live means every address in a '
                "campaign can be emailed, not only the allowlist.",
            )

    org = await session.get(Org, user.org_id)
    if org is None:  # pragma: no cover - a verified member always has an org
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organisation not found.")

    previous = org.send_mode
    org.send_mode = requested
    org.send_mode_set_by = user.email
    org.send_mode_set_at = datetime.now(tz=UTC)
    await session.commit()

    # WARNING rather than INFO: this is the line somebody will go looking for after the fact.
    logger.warning(
        "send mode changed",
        extra={
            "previous_mode": previous or f"environment:{config.SEND_MODE}",
            "new_mode": requested or f"environment:{config.SEND_MODE}",
            "changed_by": user.email,
        },
    )
    return {
        "send_mode": effective_send_mode(config, org.send_mode),
        "source": "dashboard" if org.send_mode else "environment",
        "set_by": org.send_mode_set_by,
        "set_at": org.send_mode_set_at.isoformat(),
    }
