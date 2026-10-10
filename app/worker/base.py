"""The shape every worker task shares.

`ARCHITECTURE_TARGET.md` §4 and §8. One process, five asyncio tasks. Each one is a class with a
`tick()` that does exactly one cycle and returns how long to wait before the next, and a `run()`
that does nothing but call `tick()` and sleep.

Splitting it that way is what makes the worker testable at all. A test calls `tick()` directly
with a fixed clock and asserts on what it did; nothing in the test suite ever sleeps or races a
real interval. `run()` is then thin enough to read in one go and contains no logic worth testing.

**A tick never raises.** An exception inside one cycle is recorded against that task's health and
swallowed, because the alternative — a task dying silently while the other four carry on — is the
failure mode that is hardest to notice. The task retries on its normal schedule, and three
consecutive failures become an alert (§18).
"""

from __future__ import annotations

import abc
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.worker.health import WorkerHealth

logger = logging.getLogger(__name__)

#: How long to wait after a tick raised. Longer than any normal interval, so a task failing
#: against an unreachable database backs off instead of spinning.
ERROR_BACKOFF = timedelta(seconds=30)


class WorkerTask(abc.ABC):
    """One of the five. Subclasses implement `tick`; everything else is handled here."""

    #: Short name used in logs and in the health snapshot.
    name: str = "task"
    #: How long to wait after a tick raised. An attribute rather than a constant so a test can
    #: shorten it; nothing in production overrides it.
    error_backoff: timedelta = ERROR_BACKOFF

    def __init__(self, health: WorkerHealth) -> None:
        self.health = health
        self._stopping = asyncio.Event()

    @abc.abstractmethod
    async def tick(self, now: datetime) -> timedelta:
        """Do one cycle. Return how long to wait before the next one."""

    def now(self) -> datetime:
        """Overridden in tests to pin the clock."""
        return datetime.now(tz=UTC)

    def stop(self) -> None:
        """Ask the loop to finish its current cycle and return."""
        self._stopping.set()

    async def run(self) -> None:
        """Tick, sleep, repeat, until stopped or cancelled.

        The sleep waits on the stop event rather than just the clock, so a shutdown does not have
        to wait out a 15-minute `ImapLoop` interval before the process can exit.
        """
        record = self.health.task(self.name)
        logger.info("worker task starting", extra={"task": self.name})

        while not self._stopping.is_set():
            now = self.now()
            try:
                wait = await self.tick(now)
                record.ok(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = self.error_backoff
                record.failed(f"{type(exc).__name__}: {exc}", now)
                logger.exception(
                    "worker task cycle failed",
                    extra={"task": self.name, "failures": record.consecutive_failures},
                )

            seconds = max(wait.total_seconds(), 0.0)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            except TimeoutError:
                pass

        logger.info("worker task stopped", extra={"task": self.name, "cycles": record.cycles})
