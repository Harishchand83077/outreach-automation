"""`SendLoop`: the one task that causes mail to leave.

`ARCHITECTURE_TARGET.md` §4 and §9. `send_loop.send_one` already holds every rule — eligibility
re-checked against the database immediately before sending, the IST window, the warm-up cap
counted from `messages` rather than a counter, pacing with jitter, the claim/send/mark sequence.
This task's only job is to call it, one mailbox at a time, and to wait exactly as long as it is
told to.

**One send per cycle, deliberately.** Sending a batch would make the gap between two emails zero,
which is the single most recognisable signature of bulk mail. `send_one` returns the interval to
wait, and the shortest interval any mailbox asks for governs the next cycle.

It is a separate task rather than a job kind for the reason §8 gives: send rate must be
controlled in exactly one place. Two runners draining a `send_message` queue would make the daily
cap a race between them, and the loser of that race is a suspended mailbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Mailbox
from app.domain.campaign import CampaignSettings
from app.worker.base import WorkerTask
from app.worker.context import WorkerContext
from app.worker.send_loop import send_one

logger = logging.getLogger(__name__)

#: Never wait longer than this, however far away the next send is. A worker that sleeps for
#: hours cannot notice that a mailbox was unpaused or a draft approved.
MAX_WAIT = timedelta(minutes=5)
MIN_WAIT = timedelta(seconds=5)


@dataclass
class SendTickReport:
    mailboxes: int = 0
    sent: int = 0
    reasons: list[str] = field(default_factory=list)
    wait: timedelta = MAX_WAIT


class SendLoop(WorkerTask):
    name = "send_loop"

    def __init__(self, ctx: WorkerContext) -> None:
        super().__init__(ctx.health)
        self.ctx = ctx

    async def tick(self, now: datetime) -> timedelta:
        report = await self.send_pass(now)
        return report.wait

    async def send_pass(self, now: datetime | None = None) -> SendTickReport:
        """At most one send per mailbox. Returns the shortest wait any of them asked for."""
        report = SendTickReport(wait=MAX_WAIT)

        async with self.ctx.session_factory() as session:
            mailboxes = list(
                (await session.execute(select(Mailbox).order_by(Mailbox.created_at))).scalars()
            )

        for mailbox in mailboxes:
            report.mailboxes += 1
            wait = await self._send_for(report, mailbox, now)
            report.wait = min(report.wait, wait)

        report.wait = max(min(report.wait, MAX_WAIT), MIN_WAIT)
        return report

    async def _send_for(
        self, report: SendTickReport, mailbox: Mailbox, now: datetime | None
    ) -> timedelta:
        # **No `session.begin()` here, deliberately.** `send_one` commits twice of its own
        # accord: once before the SMTP call, because a socket must not be held open inside a
        # transaction, and once after, because step 4 has to land whatever the outcome was.
        # Committing inside a `session.begin()` block ends it, and the next statement raises
        #
        #     InvalidRequestError: Can't operate on closed transaction inside context manager
        #
        # which in production happened *after* the mail was delivered: the email arrived, the
        # row stayed `sending`, and the lead never left `APPROVED`. At-most-once held, because
        # the unique idempotency key refused the next cycle's claim — but the lead was stuck.
        #
        # The reads below are safe without an explicit block; SQLAlchemy begins one on demand
        # and `send_one` is the thing that decides when work is durable.
        async with self.ctx.session_factory() as session:
            campaign = await self._campaign_for(session, mailbox)
            if campaign is None:
                report.reasons.append(f"{mailbox.from_email}: no campaign uses this mailbox")
                return MAX_WAIT

            live = await session.get(Mailbox, mailbox.id)
            if live is None:
                return MAX_WAIT

            # **Only the mailbox-level defaults are read here, and a failure is not fatal.**
            # This used to resolve a persona and return early if it was missing or unreadable,
            # which stopped the whole mailbox on account of one campaign -- while a *different*
            # campaign on the same mailbox might have had perfectly good drafts waiting.
            # `find_candidates` now validates each campaign's own persona and settings and skips
            # only that campaign, so what is wanted here is a day boundary and a window to
            # report, and the product defaults are the honest answer when there is no readable
            # campaign to take them from.
            settings = CampaignSettings()
            try:
                settings = CampaignSettings.model_validate(campaign.settings or {})
            except ValidationError as exc:
                # `campaigns.persona` and `campaigns.settings` are JSONB and nothing in the API
                # writes `settings`, so the only way a bad value gets there is a hand-written
                # UPDATE. That happened: a widened window was applied with `send_days` as
                # `["mon", ...]` where the model wants integers. The error escaped `tick()`,
                # `WorkerTask.run` recorded a failure and backed off, and *no mailbox sent
                # anything* until the row was corrected — with the reason visible only as a
                # traceback in the worker log.
                #
                # One unreadable campaign is one mailbox's problem. Report it as that mailbox's
                # reason and let the others have their turn.
                report.reasons.append(
                    f"{mailbox.from_email}: campaign {campaign.name!r} has settings that cannot "
                    f"be read ({_field_summary(exc)}); using the product defaults for this "
                    f"mailbox's cap and window"
                )

            decision = await send_one(
                session,
                org_id=mailbox.org_id,
                mailbox=live,
                settings=settings,
                sender=self.ctx.sender,
                now=now,
            )

        if decision.sent:
            report.sent += 1
            logger.info("sent", extra={"mailbox": str(mailbox.id), "lead": str(decision.lead_id)})
        elif decision.reason:
            report.reasons.append(f"{mailbox.from_email}: {decision.reason}")
        return decision.wait

    async def _campaign_for(self, session: AsyncSession, mailbox: Mailbox) -> Campaign | None:
        """A campaign on this mailbox, used only for the mailbox-level defaults.

        **This is no longer "the" campaign.** A mailbox may serve several running campaigns, and
        `send_loop.find_candidates` considers all of them: each candidate carries its own
        campaign, whose persona signs the message and whose settings schedule the follow-up and
        decide its window. What is taken from here is only the fallback used for the checks that
        belong to the mailbox rather than to a campaign — the daily cap's day boundary, and the
        wait to report when nothing is ready yet.

        It used to be the campaign for everything, which meant a lead belonging to the second
        campaign went out signed by the first.
        """
        campaign: Campaign | None = await session.scalar(
            select(Campaign)
            .where(Campaign.org_id == mailbox.org_id, Campaign.mailbox_id == mailbox.id)
            .order_by(Campaign.created_at)
            .limit(1)
        )
        return campaign


def _field_summary(exc: ValidationError) -> str:
    """Name the offending fields, without pasting a Pydantic traceback into a log line.

    The operator needs to know *which* key to correct. The values are not included: persona
    holds no secret, but a reason string ends up in logs and in `/admin` responses, and a habit
    of echoing stored content into either is how one eventually leaks.
    """
    fields = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        if location not in fields:
            fields.append(location)
    return "invalid: " + ", ".join(fields) if fields else "invalid"
