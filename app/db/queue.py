"""The job queue: a Postgres table claimed with `FOR UPDATE SKIP LOCKED`.

No Redis, no Celery. At this volume — roughly 50 new leads a day — a table and a poll loop are
fewer moving parts than a broker, and the queue shares the database transaction with the state
machine, so enqueueing work and moving a lead either both happen or neither does.

`SKIP LOCKED` is what makes several workers safe: each claim skips rows another transaction has
already locked instead of blocking on them, so workers never queue behind each other.

The prototype had no queue at all. Parallelism was `asyncio.gather` inside one process, with no
retries, no dead-letter and no resumability, so a restart orphaned every in-flight lead
(issues B9, E4).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import config
from app.db.models import Job


class JobKind:
    """The kinds of work the runner knows about. Added to as each phase lands."""

    ENRICH_LEAD = "enrich_lead"
    RESEARCH_LEAD = "research_lead"
    DRAFT_LEAD = "draft_lead"
    #: The follow-up nobody used to write. Separate from DRAFT_LEAD so the two have their own
    #: concurrency and so a backlog of one cannot starve the other.
    DRAFT_FOLLOWUP = "draft_followup"
    VALIDATE_DRAFT = "validate_draft"
    CLASSIFY_REPLY = "classify_reply"
    DRAFT_REPLY = "draft_reply"
    SEND_MESSAGE = "send_message"


async def enqueue(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    kind: str,
    lead_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
) -> Job | None:
    """Add a job, or return `None` if an identical one is already queued or running.

    Deduplication is enforced by a partial unique index on `(kind, dedupe_key)` covering only
    the live statuses, so the same work can be enqueued again later once the first attempt has
    finished. That makes enqueueing safe to retry — which matters, because the scheduler may
    notice the same due lead twice.
    """
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org_id": org_id,
        "kind": kind,
        "lead_id": lead_id,
        "payload": payload or {},
        "priority": priority,
        "status": "queued",
        "attempts": 0,
        "max_attempts": max_attempts or config.JOB_MAX_ATTEMPTS,
        "dedupe_key": dedupe_key,
    }
    if run_after is not None:
        values["run_after"] = run_after

    stmt = insert(Job).values(**values)
    if dedupe_key is not None:
        # The predicate must match migration 1's partial index exactly, or Postgres cannot infer
        # which constraint this refers to and rejects the statement.
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["kind", "dedupe_key"],
            index_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        )
    result = await session.execute(stmt.returning(Job.id))
    job_id = result.scalar_one_or_none()
    if job_id is None:
        return None
    return await session.get(Job, job_id)


async def claim(
    session: AsyncSession,
    *,
    worker_id: str,
    kinds: list[str],
    limit: int = 1,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> list[Job]:
    """Claim up to `limit` due jobs of the given kinds, leasing each to `worker_id`.

    One statement does the whole claim, so there is no window in which a job is selected but
    not yet locked. Ordering is by priority then `run_after`, so urgent work and older work go
    first.
    """
    now = now or datetime.now(tz=UTC)
    lease = timedelta(seconds=lease_seconds or config.JOB_LEASE_SECONDS)

    claimable = (
        select(Job.id)
        .where(
            Job.status == "queued",
            Job.run_after <= now,
            Job.kind.in_(kinds),
        )
        .order_by(Job.priority, Job.run_after)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    result = await session.execute(
        update(Job)
        .where(Job.id.in_(claimable))
        .values(
            status="running",
            locked_by=worker_id,
            locked_until=now + lease,
            attempts=Job.attempts + 1,
        )
        .returning(Job),
        # Fully populate the returned instances. Without this a caller that commits before
        # reading `payload` triggers a lazy load outside the async context and gets
        # MissingGreenlet — which is a confusing way to learn about an ORM detail.
        execution_options={"populate_existing": True},
    )
    # The subquery's ORDER BY decides *which* rows are claimed, but UPDATE ... RETURNING emits
    # them in whatever order it processed them — physical order in practice, not priority. Sort
    # here so a caller that works through the batch in order still handles urgent jobs first.
    return sorted(result.scalars().all(), key=lambda job: (job.priority, job.run_after))


async def heartbeat(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Extend a lease while work is still in progress.

    Returns False if the lease was already lost — the reaper requeued it and another worker may
    hold it now — in which case the caller must abandon its work rather than finish it.
    """
    now = now or datetime.now(tz=UTC)
    lease = timedelta(seconds=lease_seconds or config.JOB_LEASE_SECONDS)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", Job.locked_by == worker_id)
            .values(locked_until=now + lease)
        ),
    )
    return result.rowcount == 1


async def succeed(session: AsyncSession, job_id: uuid.UUID, *, now: datetime | None = None) -> None:
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="succeeded",
            locked_by=None,
            locked_until=None,
            last_error=None,
            finished_at=now or datetime.now(tz=UTC),
        )
    )


async def fail(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    error: str,
    retry_in: timedelta | None = None,
    permanent: bool = False,
    now: datetime | None = None,
) -> str:
    """Record a failure and decide what happens next. Returns the new status.

    A permanent failure — bad input, an auth rejection — goes straight to `dead` without
    burning retries, because retrying it would only produce the same error more slowly. A
    transient one is requeued with backoff until `max_attempts` is spent.
    """
    now = now or datetime.now(tz=UTC)
    job = await session.get(Job, job_id, with_for_update=True)
    if job is None:
        return "missing"

    # Error text is truncated: it lands in the admin UI, and a stack trace is not a message.
    trimmed = error[:2000]

    if permanent or job.attempts >= job.max_attempts:
        job.status = "dead"
        job.locked_by = None
        job.locked_until = None
        job.last_error = trimmed
        job.finished_at = now
        return "dead"

    backoff = retry_in or _backoff_for(job.attempts)
    job.status = "queued"
    job.locked_by = None
    job.locked_until = None
    job.last_error = trimmed
    job.run_after = now + backoff
    return "queued"


def _backoff_for(attempts: int) -> timedelta:
    """Exponential backoff, capped. Jitter is added by the caller when it matters."""
    seconds = min(2 ** max(attempts, 1) * 15, 3600)
    return timedelta(seconds=seconds)


async def reap_expired(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 100
) -> list[tuple[uuid.UUID, str]]:
    """Requeue jobs whose lease expired, or kill them once attempts are spent.

    This is what makes a crashed worker harmless: the lease simply runs out and the job becomes
    claimable again. Returns `(job_id, new_status)` for each row touched, so the caller can log
    or alert on dead jobs.
    """
    now = now or datetime.now(tz=UTC)

    result = await session.execute(
        select(Job)
        .where(
            Job.status == "running",
            Job.locked_until.is_not(None),
            Job.locked_until < now,
        )
        .order_by(Job.locked_until)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    outcomes: list[tuple[uuid.UUID, str]] = []
    for job in result.scalars().all():
        if job.attempts >= job.max_attempts:
            job.status = "dead"
            job.last_error = (job.last_error or "") + " | lease expired, attempts exhausted"
            job.finished_at = now
            outcomes.append((job.id, "dead"))
        else:
            job.status = "queued"
            job.run_after = now + _backoff_for(job.attempts)
            job.last_error = (job.last_error or "") + " | lease expired, requeued"
            outcomes.append((job.id, "queued"))
        job.locked_by = None
        job.locked_until = None
    return outcomes


async def cancel_for_lead(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead_id: uuid.UUID,
    kinds: list[str] | None = None,
) -> int:
    """Cancel a lead's pending jobs. Returns how many were cancelled.

    Used when a reply, bounce or opt-out arrives: the pending follow-up must die in the same
    transaction that records the reason, or we would email someone who just asked us not to.
    Only `queued` jobs are cancelled — a `running` one is left to its lease, since the worker
    holding it re-checks eligibility before acting.
    """
    conditions = [Job.org_id == org_id, Job.lead_id == lead_id, Job.status == "queued"]
    if kinds:
        conditions.append(Job.kind.in_(kinds))
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Job)
            .where(and_(*conditions))
            .values(status="cancelled", finished_at=datetime.now(tz=UTC))
        ),
    )
    return result.rowcount


async def queue_depth(session: AsyncSession, *, org_id: uuid.UUID | None = None) -> dict[str, int]:
    """Counts by status, for `/admin/jobs` and the metrics view."""
    stmt = select(Job.status, text("count(*)")).group_by(Job.status)
    if org_id is not None:
        stmt = stmt.where(Job.org_id == org_id)
    result = await session.execute(stmt)
    return {status: int(count) for status, count in result.all()}


async def find_stuck_running(
    session: AsyncSession, *, older_than: timedelta, now: datetime | None = None
) -> list[Job]:
    """Jobs that have been `running` far longer than their lease should allow.

    A lease that keeps getting extended by a wedged worker will not be reaped, so this is the
    backstop that surfaces it to a human.
    """
    now = now or datetime.now(tz=UTC)
    cutoff = now - older_than
    result = await session.execute(
        select(Job).where(
            Job.status == "running",
            or_(Job.locked_until.is_(None), Job.locked_until < now),
            Job.created_at < cutoff,
        )
    )
    return list(result.scalars().all())
