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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Mailbox
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
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
        async with self.ctx.session_factory() as session, session.begin():
            campaign = await self._campaign_for(session, mailbox)
            if campaign is None:
                report.reasons.append(f"{mailbox.from_email}: no campaign uses this mailbox")
                return MAX_WAIT

            persona_data = campaign.persona
            if not persona_data:
                # Without proof points there is nothing the sender is allowed to assert, and a
                # draft built from an empty persona should never have existed.
                report.reasons.append(f"{mailbox.from_email}: campaign has no persona")
                return MAX_WAIT

            live = await session.get(Mailbox, mailbox.id)
            if live is None:
                return MAX_WAIT

            decision = await send_one(
                session,
                org_id=mailbox.org_id,
                mailbox=live,
                persona=Persona.model_validate(persona_data),
                settings=CampaignSettings.model_validate(campaign.settings or {}),
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
        """The campaign this mailbox sends for.

        One campaign per mailbox is the shape the product has today. When that stops being true
        the send loop will need to interleave campaigns under one cap, which is a scheduling
        decision rather than a lookup, so it is better to fail this assumption loudly later than
        to guess at it here.
        """
        campaign: Campaign | None = await session.scalar(
            select(Campaign)
            .where(Campaign.org_id == mailbox.org_id, Campaign.mailbox_id == mailbox.id)
            .order_by(Campaign.created_at)
            .limit(1)
        )
        return campaign
