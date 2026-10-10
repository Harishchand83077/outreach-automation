"""`JobRunner`: claim work, do it, record what happened.

`ARCHITECTURE_TARGET.md` §8. Polls every 2 seconds, backing off to 10 when there is nothing to
do. No Redis, no broker: `FOR UPDATE SKIP LOCKED` on a table is enough at this volume and keeps
enqueueing work in the same transaction as the state change that caused it.

Three properties worth stating, because each one is a bug in the prototype it replaces:

- **Each job runs in its own session and its own transaction.** One failing job rolls back only
  its own work. The prototype ran leads concurrently in one process with `asyncio.gather` and no
  transaction boundary, so a crash orphaned every lead in flight (B9, E4).
- **The lease is heartbeated while a job runs**, so a slow job is not reaped out from under
  itself, and a job whose worker dies *is* reaped and retried.
- **Concurrency is capped per kind**, to protect LLM quotas and the sites being researched — not
  to ration "approval slots". A lead waiting for a human holds no worker capacity at all (B8).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.logging import log_context
from app.db import queue
from app.db.models import Job
from app.db.queue import JobKind
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext
from app.worker.handlers import HANDLERS, PermanentFailure

logger = logging.getLogger(__name__)

#: §8 defaults. Research is the most parallel because it is mostly waiting on other people's
#: web servers; drafting is capped tighter because it burns the free LLM quota.
CONCURRENCY: dict[str, int] = {
    JobKind.ENRICH_LEAD: 3,
    JobKind.RESEARCH_LEAD: 3,
    JobKind.DRAFT_LEAD: 2,
    JobKind.VALIDATE_DRAFT: 2,
    JobKind.CLASSIFY_REPLY: 2,
    JobKind.DRAFT_REPLY: 2,
}

IDLE_WAIT = timedelta(seconds=10)
#: How often to extend the lease of a job that is still running.
HEARTBEAT_EVERY = timedelta(seconds=30)


@dataclass
class RunnerReport:
    """What one tick did. Returned for tests and for the heartbeat log."""

    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    dead: int = 0
    errors: list[str] = field(default_factory=list)


class JobRunner(WorkerTask):
    name = "job_runner"

    def __init__(
        self,
        ctx: WorkerContext,
        *,
        worker_id: str | None = None,
        kinds: list[str] | None = None,
    ) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx
        # The same id the heartbeat row carries, so a job stuck in `locked_by` can be joined
        # to the process holding it rather than guessed at from timestamps.
        self.worker_id = worker_id or ctx.worker_id
        #: Only kinds with a registered handler are ever claimed, so a queued job the runner
        #: cannot do stays queued and visible rather than being claimed and failed repeatedly.
        self.kinds = kinds if kinds is not None else sorted(HANDLERS)
        self._running: dict[str, int] = dict.fromkeys(self.kinds, 0)

    def capacity(self) -> int:
        """How many more jobs this tick may claim, across all kinds."""
        return sum(
            max(CONCURRENCY.get(kind, 1) - self._running.get(kind, 0), 0) for kind in self.kinds
        )

    async def tick(self, now: datetime) -> timedelta:
        report = await self.run_batch(now)
        return (
            timedelta(seconds=self.ctx.settings.JOB_POLL_SECONDS) if report.claimed else IDLE_WAIT
        )

    async def run_batch(self, now: datetime | None = None) -> RunnerReport:
        """Claim up to the available capacity and run those jobs concurrently."""
        report = RunnerReport()
        limit = self.capacity()
        if limit <= 0:
            return report

        async with self.ctx.session_factory() as session, session.begin():
            jobs = await queue.claim(
                session,
                worker_id=self.worker_id,
                kinds=self.kinds,
                limit=limit,
                lease_seconds=self.ctx.settings.JOB_LEASE_SECONDS,
                now=now,
            )
            # Read what the handlers need before the claim transaction closes, so nothing
            # lazy-loads from a detached instance later.
            claimed = [(job.id, job.kind, job.org_id, dict(job.payload or {})) for job in jobs]

        report.claimed = len(claimed)
        if not claimed:
            return report

        results = await asyncio.gather(
            *(
                self._run_one(job_id, kind, org_id, payload)
                for job_id, kind, org_id, payload in claimed
            ),
            return_exceptions=False,
        )
        for outcome in results:
            if outcome == "succeeded":
                report.succeeded += 1
            elif outcome == "dead":
                report.dead += 1
                report.failed += 1
            else:
                report.failed += 1
        return report

    async def _run_one(
        self, job_id: uuid.UUID, kind: str, org_id: uuid.UUID, payload: dict[str, object]
    ) -> str:
        handler = HANDLERS.get(kind)
        if handler is None:
            # Claimed but unrunnable. Dead rather than requeued: nothing about retrying it
            # will make a handler appear.
            await self._finish(job_id, error=f"no handler for {kind}", permanent=True)
            return "dead"

        self._running[kind] = self._running.get(kind, 0) + 1
        keepalive = asyncio.create_task(self._keep_lease(job_id))
        # Bound here rather than passed down: every line any service writes while this job runs
        # carries the job and the org, including the ones written by code that has never heard
        # of a job id. The task's context is its own copy, but it is restored anyway so a direct
        # call from a test leaves nothing behind.
        lead = payload.get("lead_id")
        with log_context(
            job_id=str(job_id), org_id=str(org_id), lead_id=str(lead) if lead else None
        ):
            try:
                async with self.ctx.session_factory() as session, session.begin():
                    job = await session.get(Job, job_id)
                    if job is None:
                        return "failed"
                    await handler(self.ctx, session, job)
                    await queue.succeed(session, job_id)
                return "succeeded"
            except PermanentFailure as exc:
                logger.warning(
                    "job permanently failed",
                    extra={"job": str(job_id), "kind": kind, "error": str(exc)},
                )
                return await self._finish(job_id, error=str(exc), permanent=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("job failed", extra={"job": str(job_id), "kind": kind})
                return await self._finish(
                    job_id, error=f"{type(exc).__name__}: {exc}", permanent=False
                )
            finally:
                keepalive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keepalive
                self._running[kind] = max(self._running.get(kind, 1) - 1, 0)

    async def _finish(self, job_id: uuid.UUID, *, error: str, permanent: bool) -> str:
        """Record a failure in a fresh transaction, since the job's own has rolled back."""
        async with self.ctx.session_factory() as session, session.begin():
            return await queue.fail(session, job_id, error=error, permanent=permanent)

    async def _keep_lease(self, job_id: uuid.UUID) -> None:
        """Extend the lease while the job runs, so the reaper leaves it alone."""
        while True:
            await asyncio.sleep(HEARTBEAT_EVERY.total_seconds())
            try:
                async with self.ctx.session_factory() as session, session.begin():
                    await queue.heartbeat(
                        session,
                        job_id,
                        worker_id=self.worker_id,
                        lease_seconds=self.ctx.settings.JOB_LEASE_SECONDS,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed heartbeat means the job may be reaped and retried. That is the
                # queue working as designed, so it is logged rather than escalated.
                logger.warning("could not extend job lease", extra={"job": str(job_id)})
