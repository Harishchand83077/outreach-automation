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
4. **Follow-ups and the end of a lead.** Queue the follow-up drafts that have come due, and
   close as `NO_RESPONSE` the leads that have had everything we were going to send and said
   nothing. Both halves are idempotent, so running twice in a minute changes nothing.
5. **Retention (§17).** Once an hour rather than once a minute: clear old evidence text and
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
from app.services.campaign_clock import run_campaign_clock
from app.services.lifecycle import run_lifecycle
from app.services.mail.expiry import warn_about_expiring_grants
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
#: The grant-expiry sweep, likewise hourly: its thresholds are whole days, so a warning an hour
#: late on a seven-day deadline is not late.
GRANT_WATCH_EVERY = timedelta(hours=1)
#: Roughly a month per 30 days; retention is expressed in months and nobody means 30.44 days.
_DAYS_PER_MONTH = 30


@dataclass
class SchedulerReport:
    requeued: int = 0
    followups_queued: int = 0
    closed_silent: int = 0
    dead: int = 0
    recovered_sent: int = 0
    needs_human: int = 0
    stuck_jobs: int = 0
    leads_erased: int = 0
    evidence_cleared: int = 0
    campaigns_started: int = 0
    campaigns_completed: int = 0
    grants_warned: int = 0
    alerts: list[str] = field(default_factory=list)


class Scheduler(WorkerTask):
    name = "scheduler"

    def __init__(self, ctx: WorkerContext) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx
        #: When retention last ran. None means "not yet this process", and the first cycle does
        #: it — a worker that restarts hourly would otherwise never retain anything.
        self._retention_last: datetime | None = None
        #: When the grant-expiry sweep last ran. Same shape and the same reasoning.
        self._grants_last: datetime | None = None

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

        # **The lifecycle pass: the follow-up nobody asked for, and the end nobody wrote.**
        # Everything either half needs has existed since Phase 5 -- the due date, the state
        # machine edge, the settings, the send loop looking for an approved follow-up draft --
        # and nothing produced the thing in the middle. So "one follow-up three days later"
        # never once happened, and a lead that never replied stayed `SENT` for ever.
        #
        # In its own transaction, and never fatal: this is housekeeping, and a failure here
        # must not stop the lease reaping or the send recovery above it.
        try:
            async with self.ctx.session_factory() as session, session.begin():
                lifecycle = await run_lifecycle(session, now=now)
            report.followups_queued = lifecycle.followups_queued
            report.closed_silent = lifecycle.closed_silent
            if lifecycle.followups_queued:
                logger.info("follow-ups queued", extra={"count": lifecycle.followups_queued})
        except Exception as exc:
            logger.exception("lifecycle pass failed")
            report.alerts.append(f"the follow-up and close pass failed: {type(exc).__name__}")

        # **Campaigns that start or stop on their own.** Before this, "begin on Monday" meant
        # somebody remembering on Monday, and "stop on Friday" meant a campaign running through
        # the weekend because nobody was looking. Own transaction, never fatal: a campaign whose
        # window will not parse must not stop the lease reaping above it.
        try:
            async with self.ctx.session_factory() as session, session.begin():
                clock = await run_campaign_clock(session, now=now)
            report.campaigns_started = clock.started
            report.campaigns_completed = clock.completed
            for moved in clock.names:
                logger.info("campaign schedule moved", extra={"change": moved})
        except Exception as exc:
            logger.exception("campaign clock failed")
            report.alerts.append(f"the campaign schedule pass failed: {type(exc).__name__}")

        await self._maybe_warn_about_grants(report, now)
        await self._maybe_retain(report, now)

        for alert in report.alerts:
            logger.warning("scheduler alert", extra={"alert": alert})

        return report

    async def _maybe_warn_about_grants(self, report: SchedulerReport, now: datetime | None) -> None:
        """Warn about Gmail connections about to lapse. Hourly, and never fatal.

        **The failure this prevents is a silent one.** An unpublished OAuth client expires its
        refresh tokens after seven days (ADR-018), and until now the first anybody knew was a
        paused mailbox: a campaign that stopped on Friday, replies nobody fetched over the
        weekend, and a dashboard that said nothing until somebody happened to open it.

        Hourly rather than per-minute because the thresholds are whole days, and a warning that
        is up to an hour late on a seven-day deadline is on time.
        """
        moment = now or datetime.now(tz=UTC)
        if self._grants_last is not None and moment - self._grants_last < GRANT_WATCH_EVERY:
            return
        self._grants_last = moment

        try:
            async with self.ctx.session_factory() as session, session.begin():
                raised = await warn_about_expiring_grants(
                    session, now=now, settings=self.ctx.settings
                )
            report.grants_warned = len(raised)
        except Exception as exc:
            # Never fatal, for the same reason retention is not: this keeps people informed,
            # and the reaper and send recovery above keep the system working.
            logger.warning("the grant expiry sweep failed", extra={"error": type(exc).__name__})

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
