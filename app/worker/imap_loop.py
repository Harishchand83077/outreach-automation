"""`ImapLoop`: bring inbound mail in, on a cadence that matches how much it matters.

`ARCHITECTURE_TARGET.md` §10.1. Every 3 minutes inside send hours, every 15 outside. The
asymmetry is the point: a reply arriving at 11am is someone waiting on us during their working
day, and a reply arriving at 2am can wait until morning without anyone noticing.

The loop itself owns almost no logic. `ingest_folder` already does the careful part — poll above
the stored UID, process each message in its own transaction, advance the cursor only after the
message commits. This task's job is to call it once per mailbox and to keep going when one
mailbox fails, because one broken mailbox must not stop the others.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db.models import Mailbox
from app.domain.campaign import CampaignSettings
from app.domain.scheduling import in_window
from app.services.reply.ingest import IngestReport, ingest_folder
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext

logger = logging.getLogger(__name__)

BUSY_INTERVAL = timedelta(minutes=3)
QUIET_INTERVAL = timedelta(minutes=15)

#: INBOX only for now. Spam is worth adding once a real campaign has run, because replies do
#: land there — but polling a folder we have never seen in production is a good way to discover
#: a provider quirk at the worst moment.
FOLDERS: tuple[str, ...] = ("INBOX",)


@dataclass
class ImapTickReport:
    mailboxes: int = 0
    processed: int = 0
    duplicates: int = 0
    orphans: int = 0
    alerts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ImapLoop(WorkerTask):
    name = "imap_loop"

    def __init__(self, ctx: WorkerContext, *, settings: CampaignSettings | None = None) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx
        #: Used only to decide the polling cadence. Per-campaign windows govern sending, but a
        #: mailbox is shared, so the cadence follows the default working day.
        self.settings = settings or CampaignSettings()

    async def tick(self, now: datetime) -> timedelta:
        await self.poll_all(now)
        return BUSY_INTERVAL if in_window(now, self.settings) else QUIET_INTERVAL

    async def poll_all(self, now: datetime | None = None) -> ImapTickReport:
        """One pass over every mailbox. Never raises for one mailbox's failure."""
        report = ImapTickReport()

        async with self.ctx.session_factory() as session:
            mailboxes = list(
                (await session.execute(select(Mailbox).order_by(Mailbox.created_at))).scalars()
            )

        for mailbox in mailboxes:
            if mailbox.paused:
                # A paused mailbox should not be sending, but it should still be read: a reply
                # or an opt-out that arrives while it is paused still has to be honoured.
                logger.debug("polling a paused mailbox", extra={"mailbox": str(mailbox.id)})
            report.mailboxes += 1
            for folder in FOLDERS:
                await self._poll_one(report, mailbox, folder, now)

        return report

    async def _poll_one(
        self, report: ImapTickReport, mailbox: Mailbox, folder: str, now: datetime | None
    ) -> None:
        try:
            result: IngestReport = await ingest_folder(
                self.ctx.session_factory,
                poller=self.ctx.poller,
                router=self.ctx.router_for_org(mailbox.org_id),
                org_id=mailbox.org_id,
                mailbox_id=mailbox.id,
                folder=folder,
                settings=self.settings,
                now=now,
            )
        except Exception as exc:
            # ingest_folder reports expected failures rather than raising, so reaching here
            # means a bug. One mailbox's bug must not stop the rest from being read.
            report.errors.append(f"{mailbox.from_email}/{folder}: {type(exc).__name__}: {exc}")
            logger.exception("ingest raised", extra={"mailbox": str(mailbox.id), "folder": folder})
            return

        report.processed += len(result.processed)
        report.duplicates += result.duplicates
        report.orphans += result.orphans
        report.errors.extend(result.errors)
        if result.poll_error:
            report.errors.append(f"{mailbox.from_email}/{folder}: {result.poll_error}")
        if result.should_alert:
            report.alerts.append(
                f"{mailbox.from_email}/{folder} has failed to poll repeatedly: {result.poll_error}"
            )
            logger.error(
                "imap polling needs a human",
                extra={"mailbox": str(mailbox.id), "folder": folder, "error": result.poll_error},
            )
