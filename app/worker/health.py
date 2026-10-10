"""What the worker knows about itself.

`ARCHITECTURE_TARGET.md` §18. One object, shared by the five tasks, holding the last time each
one completed a cycle and the last error it hit. `Heartbeat` reads it; `/ready` will read it too.

It is deliberately in memory rather than in a table. A heartbeat's only job is to say "this
process is alive right now", and a process that has died cannot write a row saying so — the
absence of a recent heartbeat in the log is the signal, not its presence in the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

#: A task quiet for longer than this is treated as stalled rather than idle. Generous, because
#: `ImapLoop` legitimately sleeps 15 minutes outside send hours.
STALL_AFTER = timedelta(minutes=45)


@dataclass
class TaskHealth:
    """One task's record. `cycles` counts completed ticks, not attempts."""

    name: str
    cycles: int = 0
    last_ok: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    consecutive_failures: int = 0

    def ok(self, now: datetime) -> None:
        self.cycles += 1
        self.last_ok = now
        self.consecutive_failures = 0

    def failed(self, error: str, now: datetime) -> None:
        self.last_error = error
        self.last_error_at = now
        self.consecutive_failures += 1

    def stalled(self, now: datetime, *, after: timedelta = STALL_AFTER) -> bool:
        """True when this task has not completed a cycle for too long.

        A task that has never run is not stalled — the worker may have started seconds ago.
        """
        if self.last_ok is None:
            return False
        return now - self.last_ok > after


@dataclass
class WorkerHealth:
    """The whole process's view of itself."""

    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    tasks: dict[str, TaskHealth] = field(default_factory=dict)

    def task(self, name: str) -> TaskHealth:
        return self.tasks.setdefault(name, TaskHealth(name=name))

    def snapshot(self, now: datetime | None = None) -> dict[str, object]:
        """A plain dict for logging and for the readiness endpoint."""
        now = now or datetime.now(tz=UTC)
        return {
            "uptime_seconds": int((now - self.started_at).total_seconds()),
            "healthy": self.healthy(now),
            "tasks": {
                name: {
                    "cycles": task.cycles,
                    "last_ok": task.last_ok.isoformat() if task.last_ok else None,
                    "stalled": task.stalled(now),
                    "consecutive_failures": task.consecutive_failures,
                    "last_error": task.last_error,
                }
                for name, task in sorted(self.tasks.items())
            },
        }

    def healthy(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(tz=UTC)
        return not any(task.stalled(now) for task in self.tasks.values())
