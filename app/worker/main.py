"""The worker process: five asyncio tasks in one process.

`ARCHITECTURE_TARGET.md` §4. Run it with `python -m app.worker`.

    JobRunner   research, drafting, reply drafting — claimed with FOR UPDATE SKIP LOCKED
    SendLoop    the only code that calls SMTP, paced and capped
    ImapLoop    poll with BODY.PEEK[], match, classify, act
    Scheduler   reap expired leases, resolve interrupted sends
    Heartbeat   one structured line a minute saying what the other four are doing

**Graceful shutdown.** SIGTERM and SIGINT ask every task to stop after its current cycle rather
than cancelling mid-flight. That matters most for `SendLoop`: a cancellation between the SMTP
call and the row that records it leaves a message in `sending`, which costs a human a trip to the
mailbox to work out whether it left. The scheduler would recover it, but not having to is better.

If a task does not stop within the grace period it is cancelled, because a worker that refuses to
exit is worse than one that loses a cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from app.core.logging import configure_logging, redacted_database_url
from app.core.settings import config
from app.db.engine import dispose_engine
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext
from app.worker.heartbeat import Heartbeat
from app.worker.imap_loop import ImapLoop
from app.worker.runner import JobRunner
from app.worker.scheduler import Scheduler
from app.worker.send_task import SendLoop

logger = logging.getLogger(__name__)

#: How long a task gets to finish its cycle after being asked to stop.
SHUTDOWN_GRACE_SECONDS = 30.0


def build_tasks(ctx: WorkerContext) -> list[WorkerTask]:
    """The five, in the order they appear in §4."""
    return [JobRunner(ctx), SendLoop(ctx), ImapLoop(ctx), Scheduler(ctx), Heartbeat(ctx)]


async def run_worker(ctx: WorkerContext | None = None) -> None:
    """Start every task and run until a signal asks us to stop."""
    ctx = ctx or WorkerContext.build()
    tasks = build_tasks(ctx)
    stopping = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # Windows has no add_signal_handler for SIGTERM; KeyboardInterrupt still works and
            # the suppression keeps the worker runnable on a developer machine.
            loop.add_signal_handler(sig, stopping.set)

    logger.info(
        "worker starting",
        extra={
            "tasks": [task.name for task in tasks],
            "send_mode": ctx.settings.SEND_MODE,
            "worker_id": ctx.worker_id,
            "env": ctx.settings.APP_ENV,
            # The password is stripped: the rest is worth seeing, because "connected to the
            # wrong database" is otherwise invisible until something is missing (ADR-005).
            "database": redacted_database_url(ctx.settings.DATABASE_URL),
        },
    )

    running = [asyncio.create_task(task.run(), name=task.name) for task in tasks]
    try:
        await stopping.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("worker stopping")
        for task in tasks:
            task.stop()

        done, pending = await asyncio.wait(running, timeout=SHUTDOWN_GRACE_SECONDS)
        for late in pending:
            logger.warning("task did not stop in time; cancelling", extra={"task": late.get_name()})
            late.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await dispose_engine()
        logger.info("worker stopped", extra={"finished": len(done)})


def main() -> None:
    configure_logging(config)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
