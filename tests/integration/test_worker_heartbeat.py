"""The `Heartbeat` task: what it publishes, and what it refuses to let fail.

§18. Three properties, each of which is the reason this task exists:

- **It publishes a row the api can read**, because the worker's own health lives in this
  process's memory and `/ready` runs in another one.
- **It beats even when the database is unreachable.** That is precisely the news, and a
  heartbeat that fails whenever it has something important to say is worse than none.
- **Trouble becomes an alert, but only after it persists.** One failed cycle is a blip; three in
  a row is a condition. Alerting on the blip trains whoever receives the alerts to ignore them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.settings import config
from app.db import ops
from app.db.models import Alert, WorkerHeartbeat
from app.worker.context import WorkerContext
from app.worker.health import STALL_AFTER
from app.worker.heartbeat import ALERT_AFTER_FAILURES, INTERVAL, Heartbeat

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)


def context(session_factory) -> WorkerContext:
    return WorkerContext(
        session_factory=session_factory,
        settings=config,
        poller=object(),  # type: ignore[arg-type]
        sender=object(),  # type: ignore[arg-type]
        worker_id="test-host:1",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def task(db, session_factory):
    """`db` is requested so the table is truncated after each test."""
    return Heartbeat(context(session_factory))


class TestPublishing:
    async def test_the_beat_writes_a_row_the_api_can_read(self, task, db):
        wait = await task.tick(NOW)
        assert wait == INTERVAL

        status = await ops.latest_heartbeat(db, now=NOW)
        assert status is not None
        assert status.worker_id == "test-host:1"

    async def test_the_row_carries_the_send_mode_and_the_queue_depth(self, task, db):
        await task.tick(NOW)
        status = await ops.latest_heartbeat(db, now=NOW)
        assert status is not None
        assert status.info["send_mode"] == config.SEND_MODE
        assert "queue_depth" in status.info

    async def test_beating_twice_leaves_one_row(self, task, db):
        await task.tick(NOW)
        await task.tick(NOW + timedelta(minutes=1))
        assert await db.scalar(select(func.count()).select_from(WorkerHeartbeat)) == 1

    async def test_a_database_failure_does_not_fail_the_beat(self, session_factory, db):
        """The database being unreachable is the news, not a reason to stop reporting."""

        class Broken:
            def __call__(self):
                raise RuntimeError("no database")

        broken = Heartbeat(context(session_factory))
        broken.ctx.session_factory = Broken()  # type: ignore[assignment]

        # No exception: the tick completes and the log line carries the failure.
        assert await broken.tick(NOW) == INTERVAL


class TestAlerting:
    async def test_a_single_failed_cycle_is_not_an_alert(self, task, db):
        task.ctx.health.task("send_loop").failed("boom", NOW)
        await task.tick(NOW)

        assert await db.scalar(select(func.count()).select_from(Alert)) == 0

    async def test_repeated_failures_become_a_critical_alert(self, task, db):
        record = task.ctx.health.task("send_loop")
        for _ in range(ALERT_AFTER_FAILURES):
            record.failed("boom", NOW)
        await task.tick(NOW)

        alert = (await db.execute(select(Alert))).scalars().one()
        assert alert.severity == "critical"
        assert "send_loop" in alert.message
        assert alert.org_id is None  # a dead task stops every org's work (ADR-009)

    async def test_the_alert_does_not_carry_the_error_text(self, task, db):
        """It comes from an exception, and an exception is as likely to quote a connection
        string as anything useful (§18)."""
        record = task.ctx.health.task("send_loop")
        for _ in range(ALERT_AFTER_FAILURES):
            record.failed("could not connect to postgresql://user:hunter2@host/db", NOW)
        await task.tick(NOW)

        alert = (await db.execute(select(Alert))).scalars().one()
        assert "hunter2" not in alert.message
        assert "hunter2" not in str(alert.detail)

    async def test_a_stalled_task_becomes_a_critical_alert(self, task, db):
        record = task.ctx.health.task("imap_loop")
        record.ok(NOW - STALL_AFTER - timedelta(minutes=5))
        await task.tick(NOW)

        alert = (await db.execute(select(Alert))).scalars().one()
        assert alert.kind == "worker_task_stalled"
        assert "imap_loop" in alert.message

    async def test_the_same_trouble_does_not_alert_every_minute(self, task, db):
        record = task.ctx.health.task("imap_loop")
        record.ok(NOW - STALL_AFTER - timedelta(minutes=5))
        await task.tick(NOW)
        await task.tick(NOW + timedelta(minutes=1))
        await task.tick(NOW + timedelta(minutes=2))

        assert await db.scalar(select(func.count()).select_from(Alert)) == 1

    async def test_a_healthy_worker_raises_nothing(self, task, db):
        task.ctx.health.task("send_loop").ok(NOW)
        await task.tick(NOW)

        assert await db.scalar(select(func.count()).select_from(Alert)) == 0
