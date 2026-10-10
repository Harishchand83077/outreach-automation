"""Operational state: worker heartbeats and alerts.

Separate from `repo.py` because neither is tenant data in the way a lead is. A heartbeat belongs
to a process, and an alert is read by an operator rather than by the outreach flow — so these
functions deliberately do **not** take an `OrgContext`: `record_heartbeat` has no org to take,
and `raise_alert` is called from the worker's own loops, which run for every org at once.
Reading alerts back *is* scoped, and `list_alerts` takes the org explicitly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Alert, WorkerHeartbeat

#: A heartbeat older than this means the worker is gone, not busy. The worker beats once a
#: minute, so this tolerates four missed beats before `/ready` fails — enough to survive a slow
#: cycle or a restart, short enough that a dead worker is noticed within the hour it died.
HEARTBEAT_STALE_AFTER = timedelta(minutes=5)

#: The same condition is not worth a second row, or a second webhook, inside this window.
ALERT_COOLDOWN = timedelta(hours=1)

SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True)
class WorkerStatus:
    """What the api can say about the worker without being the worker."""

    worker_id: str
    last_seen_at: datetime
    started_at: datetime
    info: dict[str, Any]
    stale: bool

    @property
    def age_seconds(self) -> int:
        return max(0, int((datetime.now(tz=UTC) - self.last_seen_at).total_seconds()))


async def record_heartbeat(
    session: AsyncSession,
    *,
    worker_id: str,
    started_at: datetime,
    info: dict[str, Any],
    now: datetime | None = None,
    role: str = "worker",
) -> None:
    """Upsert this process's row. One row per `worker_id`, rewritten every beat."""
    now = now or datetime.now(tz=UTC)
    statement = (
        insert(WorkerHeartbeat)
        .values(
            worker_id=worker_id,
            role=role,
            started_at=started_at,
            last_seen_at=now,
            info=info,
        )
        .on_conflict_do_update(
            index_elements=[WorkerHeartbeat.worker_id],
            # `started_at` is updated too: the pid can be reused after a restart, and a row
            # claiming an uptime of three days for a process started a minute ago is a lie that
            # would survive every restart.
            set_={"last_seen_at": now, "info": info, "started_at": started_at, "role": role},
        )
    )
    await session.execute(statement)


async def latest_heartbeat(
    session: AsyncSession, *, now: datetime | None = None, role: str = "worker"
) -> WorkerStatus | None:
    """The freshest heartbeat, or None when no worker has ever run.

    The freshest rather than all of them: with one worker the distinction is academic, and if a
    second is ever added, readiness should ask "is at least one alive", not "are all rows fresh" —
    a stale row from a container that was removed would otherwise fail readiness forever.
    """
    now = now or datetime.now(tz=UTC)
    row = (
        await session.execute(
            select(WorkerHeartbeat)
            .where(WorkerHeartbeat.role == role)
            .order_by(WorkerHeartbeat.last_seen_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    last_seen = row.last_seen_at
    if last_seen.tzinfo is None:  # pragma: no cover - the column is timestamptz
        last_seen = last_seen.replace(tzinfo=UTC)
    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)

    return WorkerStatus(
        worker_id=row.worker_id,
        last_seen_at=last_seen,
        started_at=started,
        info=dict(row.info or {}),
        stale=(now - last_seen) > HEARTBEAT_STALE_AFTER,
    )


async def prune_heartbeats(
    session: AsyncSession, *, older_than: timedelta, now: datetime | None = None
) -> int:
    """Delete heartbeats from processes long gone, so the table stays one row per live worker."""
    now = now or datetime.now(tz=UTC)
    rows = (
        (
            await session.execute(
                select(WorkerHeartbeat).where(WorkerHeartbeat.last_seen_at < now - older_than)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await session.delete(row)
    return len(rows)


async def raise_alert(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    kind: str,
    message: str,
    severity: str = "warning",
    detail: dict[str, Any] | None = None,
    cooldown: timedelta | None = ALERT_COOLDOWN,
    now: datetime | None = None,
) -> Alert | None:
    """Record an alert, unless the same kind was already raised inside the cool-down.

    Returns the row, or None when it was suppressed as a repeat. The caller uses that to decide
    whether to push the webhook, so a condition that persists for a day produces one message
    rather than one thousand four hundred and forty.
    """
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity: {severity}")

    now = now or datetime.now(tz=UTC)
    if cooldown is not None:
        recent = (
            await session.execute(
                select(Alert.id)
                .where(
                    Alert.org_id.is_(None) if org_id is None else Alert.org_id == org_id,
                    Alert.kind == kind,
                    Alert.created_at > now - cooldown,
                    # An acknowledged alert does not suppress a new one: a human saying "seen"
                    # means they want to be told if it happens again.
                    Alert.acked_at.is_(None),
                )
                .limit(1)
            )
        ).first()
        if recent is not None:
            return None

    alert = Alert(
        org_id=org_id,
        severity=severity,
        kind=kind,
        message=message,
        detail=detail,
        created_at=now,
    )
    session.add(alert)
    await session.flush()
    return alert


async def list_alerts(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    unacked_only: bool = False,
    limit: int = 50,
) -> list[Alert]:
    """Newest first: this org's alerts, plus the system-wide ones that affect it."""
    statement = select(Alert).where((Alert.org_id == org_id) | Alert.org_id.is_(None))
    if unacked_only:
        statement = statement.where(Alert.acked_at.is_(None))
    statement = statement.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit)
    return list((await session.execute(statement)).scalars().all())


async def ack_alert(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    alert_id: uuid.UUID,
    user_id: str,
    now: datetime | None = None,
) -> Alert:
    """Acknowledge one alert.

    Raises `LookupError` when the id is not this org's — the same answer as for an id that does
    not exist, because confirming an id belongs to somebody else is itself a leak.
    """
    row = (
        await session.execute(
            select(Alert).where(
                Alert.id == alert_id,
                (Alert.org_id == org_id) | Alert.org_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"no alert {alert_id}")
    row.acked_at = now or datetime.now(tz=UTC)
    row.acked_by = user_id
    await session.flush()
    return row
