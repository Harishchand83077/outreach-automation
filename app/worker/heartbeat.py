"""`Heartbeat`: say that this process is alive, and what it has been doing.

`ARCHITECTURE_TARGET.md` §18. One structured log line a minute carrying the health snapshot and
the queue depth. That is the whole task.

It writes to the log rather than to a table on purpose. A heartbeat's only claim is "I am alive
now", and a dead process cannot write a row saying it is not — so what monitoring watches for is
the *absence* of recent lines, which no amount of database writing would improve. It also means
the heartbeat keeps working when the database is the thing that has failed, which is exactly when
somebody wants to know.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.db import queue
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=1)


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
            extra={"health": snapshot, "queue_depth": depth, "stalled": stalled},
        )
        if stalled:
            logger.error("worker tasks are stalled", extra={"tasks": stalled})

        return INTERVAL
