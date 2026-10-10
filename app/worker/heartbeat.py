"""`Heartbeat`: say that this process is alive, and what it has been doing.

`ARCHITECTURE_TARGET.md` §18. Once a minute: one structured log line carrying the health
snapshot and the queue depth, one upserted row so the api's `/ready` can see this process at
all, and an alert for anything that has gone wrong and stayed wrong.

**Why both a log line and a row.** The log line is the liveness signal, and what monitoring
watches for is the *absence* of recent lines — a dead process cannot log that it has died, and no
amount of database writing would improve that. But `/ready` runs in the api process and cannot
see this one's memory, so it needs something shared; what it reads is the row's **age**, which a
stopped worker stops refreshing. The two are the same signal expressed for two different
readers, which is why both exist and neither is redundant (ADR-009).

The row write is deliberately not allowed to fail the tick: if the database is unreachable, that
is the news, and the log line still carries it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.db import ops, queue
from app.services.alerts import raise_alert
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=1)

#: Consecutive failed cycles before a task's trouble is worth an operator's attention. One
#: failure is a blip — an LLM provider hiccup, a dropped connection — and alerting on it would
#: train whoever receives the alerts to ignore them.
ALERT_AFTER_FAILURES = 3

#: Rows from processes that have not beaten for this long are deleted. Long enough that a
#: restart does not lose the previous row before the new one lands, short enough that the table
#: does not accumulate a row per container ever started.
PRUNE_AFTER = timedelta(days=2)


class Heartbeat(WorkerTask):
    name = "heartbeat"

    def __init__(self, ctx: WorkerContext) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx

    async def tick(self, now: datetime) -> timedelta:
        snapshot = self.ctx.health.snapshot(now)
        depth: dict[str, int] | str
        try:
            async with self.ctx.session_factory() as session:
                depth = await queue.queue_depth(session)
        except Exception as exc:
            # The database being unreachable is precisely what the heartbeat exists to report,
            # so it is logged as part of the beat rather than failing the tick.
            depth = f"unavailable: {type(exc).__name__}"

        stalled = [name for name, task in self.ctx.health.tasks.items() if task.stalled(now)]
        logger.info(
            "worker heartbeat",
            extra={
                "health": snapshot,
                "queue_depth": depth,
                "stalled": stalled,
                "worker_id": self.ctx.worker_id,
            },
        )
        if stalled:
            logger.error("worker tasks are stalled", extra={"tasks": stalled})

        await self._publish(snapshot, depth, now)
        await self._alert_on_trouble(stalled, now)

        return INTERVAL

    async def _publish(self, snapshot: dict[str, object], depth: object, now: datetime) -> None:
        """Upsert this process's row so `/ready` can see it. Never fails the tick."""
        try:
            async with self.ctx.session_factory() as session, session.begin():
                await ops.record_heartbeat(
                    session,
                    worker_id=self.ctx.worker_id,
                    started_at=self.ctx.health.started_at,
                    info={
                        "health": snapshot,
                        "queue_depth": depth,
                        "send_mode": self.ctx.settings.SEND_MODE,
                    },
                    now=now,
                )
                await ops.prune_heartbeats(session, older_than=PRUNE_AFTER, now=now)
        except Exception as exc:
            logger.warning(
                "could not record the heartbeat row; /ready will report the worker stale",
                extra={"error": type(exc).__name__},
            )

    async def _alert_on_trouble(self, stalled: list[str], now: datetime) -> None:
        """Raise the §18 alerts this process is in a position to notice about itself.

        System-wide (`org_id=None`): a stalled or repeatedly failing task is not one tenant's
        problem, it stops the work of every org at once.
        """
        failing = {
            name: task.consecutive_failures
            for name, task in self.ctx.health.tasks.items()
            if task.consecutive_failures >= ALERT_AFTER_FAILURES
        }
        if not stalled and not failing:
            return

        try:
            async with self.ctx.session_factory() as session, session.begin():
                if stalled:
                    await raise_alert(
                        session,
                        org_id=None,
                        kind="worker_task_stalled",
                        severity="critical",
                        message=(
                            "Worker tasks have not completed a cycle for too long: "
                            + ", ".join(sorted(stalled))
                        ),
                        detail={"tasks": sorted(stalled), "worker_id": self.ctx.worker_id},
                        now=now,
                    )
                for name, failures in sorted(failing.items()):
                    # The error text is in the logs, not here: it comes from an exception and
                    # may quote a connection string (§18).
                    await raise_alert(
                        session,
                        org_id=None,
                        kind=f"worker_task_failing:{name}",
                        severity="critical",
                        message=(
                            f"Worker task {name} has failed {failures} cycles in a row. "
                            "Check the worker logs for the error."
                        ),
                        detail={"task": name, "failures": failures},
                        now=now,
                    )
        except Exception as exc:
            logger.warning("could not record an alert", extra={"error": type(exc).__name__})
