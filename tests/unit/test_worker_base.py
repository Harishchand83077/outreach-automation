"""The loop scaffolding: a tick that fails must not kill its task.

The failure this guards against is the quiet one. If an exception escapes a task's loop, that
task dies while the other four carry on — the process stays up, the health endpoint says it is
running, and nobody finds out until a day of replies has gone unread. So a tick records its
failure, backs off and tries again, and three in a row become visible.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.worker.base import ERROR_BACKOFF, WorkerTask
from app.worker.health import STALL_AFTER, WorkerHealth

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)


class Counting(WorkerTask):
    """Ticks a fixed number of times, then stops itself."""

    name = "counting"

    error_backoff = timedelta(0)

    def __init__(self, health, *, stop_after=2, explode_on=None, wait=timedelta(0)) -> None:
        super().__init__(health)
        self.calls = 0
        self.stop_after = stop_after
        self.explode_on = explode_on or set()
        self.wait = wait

    def now(self) -> datetime:
        return NOW

    async def tick(self, now: datetime) -> timedelta:
        self.calls += 1
        if self.calls >= self.stop_after:
            self.stop()
        if self.calls in self.explode_on:
            raise RuntimeError(f"tick {self.calls} failed")
        return self.wait


@pytest.mark.asyncio
class TestATickThatRaises:
    async def test_does_not_stop_the_task(self):
        health = WorkerHealth()
        task = Counting(health, stop_after=3, explode_on={1})
        await asyncio.wait_for(task.run(), timeout=5)
        assert task.calls == 3, "it kept going after the failure"

    async def test_is_recorded_against_the_task(self):
        health = WorkerHealth()
        task = Counting(health, stop_after=2, explode_on={1})
        await asyncio.wait_for(task.run(), timeout=5)

        record = health.task("counting")
        assert record.last_error is not None
        assert "RuntimeError" in record.last_error

    async def test_a_success_clears_the_failure_run(self):
        health = WorkerHealth()
        task = Counting(health, stop_after=3, explode_on={1})
        await asyncio.wait_for(task.run(), timeout=5)
        assert health.task("counting").consecutive_failures == 0

    async def test_consecutive_failures_are_counted(self):
        health = WorkerHealth()
        task = Counting(health, stop_after=3, explode_on={1, 2, 3})
        await asyncio.wait_for(task.run(), timeout=5)
        assert health.task("counting").consecutive_failures == 3

    async def test_the_backoff_is_longer_than_a_normal_interval(self):
        """A task failing against an unreachable database must not spin."""
        assert ERROR_BACKOFF >= timedelta(seconds=10)


@pytest.mark.asyncio
class TestStopping:
    async def test_a_long_sleep_does_not_delay_shutdown(self):
        """The sleep waits on the stop event, not only on the clock."""
        health = WorkerHealth()
        task = Counting(health, stop_after=1, wait=timedelta(hours=1))
        await asyncio.wait_for(task.run(), timeout=5)
        assert task.calls == 1

    async def test_cancellation_still_propagates(self):
        health = WorkerHealth()

        class Hanging(WorkerTask):
            name = "hanging"

            async def tick(self, now):
                await asyncio.sleep(3600)
                return timedelta(0)

        task = Hanging(health)
        running = asyncio.create_task(task.run())
        await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running


class TestHealth:
    def test_a_task_that_never_ran_is_not_stalled(self):
        """The worker may have started a second ago."""
        health = WorkerHealth()
        assert not health.task("fresh").stalled(NOW)
        assert health.healthy(NOW)

    def test_a_quiet_task_eventually_counts_as_stalled(self):
        health = WorkerHealth()
        record = health.task("quiet")
        record.ok(NOW - STALL_AFTER - timedelta(minutes=1))
        assert record.stalled(NOW)
        assert not health.healthy(NOW)

    def test_the_snapshot_names_every_task(self):
        health = WorkerHealth()
        health.task("a").ok(NOW)
        health.task("b").failed("boom", NOW)
        snapshot = health.snapshot(NOW)
        assert set(snapshot["tasks"]) == {"a", "b"}
