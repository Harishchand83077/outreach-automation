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
4. **Retention (§17).** Once an hour rather than once a minute: clear old evidence text and
   erase leads that finished longer ago than `LEAD_RETENTION_MONTHS`. Erasure goes through the
   same path a data-subject request takes, so the hashed suppression survives — a lead that
   opted out must not become contactable again because the system tidied up.

Everything here is idempotent, so running it twice in the same minute — two workers, or a manual
`POST /internal/tick` — changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.db import queue
from app.services.privacy import run_retention
from app.services.reply.recovery import recover_stuck_sends
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext

logger = logging.getLogger(__name__)

INTERVAL = timedelta(minutes=1)
#: A job still `running` this long after its lease should have been extended is a symptom, not
#: normal slowness: the heartbeat runs every 30 seconds.
STUCK_AFTER = timedelta(minutes=15)
#: Retention runs hourly, not on every tick. Nothing about it is urgent — a lead a minute past
#: its retention date is not a problem — and each pass cascades deletes across five tables.
RETENTION_EVERY = timedelta(hours=1)
#: Roughly a month per 30 days; retention is expressed in months and nobody means 30.44 days.
_DAYS_PER_MONTH = 30


@dataclass
class SchedulerReport:
    requeued: int = 0
    dead: int = 0
    recovered_sent: int = 0
    needs_human: int = 0
    stuck_jobs: int = 0
    leads_erased: int = 0
    evidence_cleared: int = 0
    alerts: list[str] = field(default_factory=list)


class Scheduler(WorkerTask):
    name = "scheduler"

    def __init__(self, ctx: WorkerContext) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx
        #: When retention last ran. None means "not yet this process", and the first cycle does
        #: it — a worker that restarts hourly would otherwise never retain anything.
        self._retention_last: datetime | None = None

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

        await self._maybe_retain(report, now)

        for alert in report.alerts:
            logger.warning("scheduler alert", extra={"alert": alert})

        return report

    async def _maybe_retain(self, report: SchedulerReport, now: datetime | None) -> None:
        """Run the retention sweep at most hourly. Never fails the cycle.

        A retention failure must not stop the reaper or send recovery: those keep the system
        working, and this one only keeps it tidy.
        """
        settings = self.ctx.settings
        if not settings.RETENTION_ENABLED:
            return

        moment = now or datetime.now(tz=UTC)
        if self._retention_last is not None and moment - self._retention_last < RETENTION_EVERY:
            return
        self._retention_last = moment

        try:
            swept = await run_retention(
                self.ctx.session_factory,
                lead_retention=timedelta(days=settings.LEAD_RETENTION_MONTHS * _DAYS_PER_MONTH),
                evidence_retention=timedelta(days=settings.RESEARCH_TEXT_RETENTION_DAYS),
                now=now,
            )
        except Exception as exc:
            logger.warning("retention sweep failed", extra={"error": type(exc).__name__})
            return

        report.leads_erased = swept.leads_erased
        report.evidence_cleared = swept.evidence_cleared
