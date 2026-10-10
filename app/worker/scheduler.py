"""`Scheduler`: the periodic housekeeping nothing else owns.

`ARCHITECTURE_TARGET.md` §8 and §9.5. Three jobs on a one-minute cycle:

1. **Reap expired leases.** A job whose worker died is requeued, or marked `dead` once it has
   spent its attempts. Without this a crash mid-job loses that lead silently.
2. **Resolve interrupted sends.** A row left in `sending` is asked about over IMAP: found in the
   Sent folder means the mail left and the row becomes `sent`; not found means `unknown` and a
   human decides. It never re-sends (ADR-002), because a duplicate cold email is worse than one
   that needs confirming.
3. **Report what is stuck.** Jobs `running` far past their lease and messages sitting in
   `unknown` are surfaced in the tick report, which the heartbeat logs.

Everything here is idempotent, so running it twice in the same minute — two workers, or a manual
`POST /internal/tick` — changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.db import queue
from app.services.reply.recovery import recover_stuck_sends
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=1)
#: A job still `running` this long after its lease should have been extended is a symptom, not
#: normal slowness: the heartbeat runs every 30 seconds.
STUCK_AFTER = timedelta(minutes=15)


@dataclass
class SchedulerReport:
    requeued: int = 0
    dead: int = 0
    recovered_sent: int = 0
    needs_human: int = 0
    stuck_jobs: int = 0
    alerts: list[str] = field(default_factory=list)


class Scheduler(WorkerTask):
    name = "scheduler"

    def __init__(self, ctx: WorkerContext) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx

    async def tick(self, now: datetime) -> timedelta:
        await self.run_cycle(now)
        return INTERVAL

    async def run_cycle(self, now: datetime | None = None) -> SchedulerReport:
        """One housekeeping pass. This is what `POST /internal/tick` will call."""
        report = SchedulerReport()

        async with self.ctx.session_factory() as session, session.begin():
            reaped = await queue.reap_expired(session, now=now)
            report.requeued = sum(1 for _, status in reaped if status == "queued")
            report.dead = sum(1 for _, status in reaped if status == "dead")
            report.stuck_jobs = len(
                await queue.find_stuck_running(session, older_than=STUCK_AFTER, now=now)
            )

        if report.dead:
            report.alerts.append(f"{report.dead} job(s) exhausted their retries and are dead")

        recovery = await recover_stuck_sends(
            self.ctx.session_factory, poller=self.ctx.poller, now=now
        )
        report.recovered_sent = recovery.confirmed_sent
        report.needs_human = recovery.needs_human

        if recovery.needs_human:
            # §9.5: never auto-retried. Somebody has to look in the mailbox and say.
            report.alerts.append(
                f"{recovery.needs_human} interrupted send(s) could not be resolved and need a "
                "person to confirm whether they left"
            )
            logger.error("sends need human confirmation", extra={"count": recovery.needs_human})

        for alert in report.alerts:
            logger.warning("scheduler alert", extra={"alert": alert})

        return report
