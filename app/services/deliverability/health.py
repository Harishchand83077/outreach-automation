"""Per-mailbox health, and the one thing it is allowed to do about it: pause (ADR-034).

Two halves, deliberately separate.

**The rollup** recomputes `mailbox_health` from `messages` and `classifications`, which are the
record. Not counters incremented at send time: a crash between the send and the increment leaves
a counter wrong for ever and nothing notices, while a rollup is always a fresh answer to a query.

**The decision** reads a rolling window of those rows and may pause a mailbox. It may never
raise a cap, never un-pause, and never change a send mode. That asymmetry is the whole design: a
signal that can only restrict cannot itself cause an outage, so it can be trusted to act without
a human in the loop. Resuming is somebody's decision, because resuming is the risky direction.

**Three guards against pausing for nothing**, which matter more than the threshold itself:

1. A **minimum volume** before any rate is believed. At five sends, one bounce is 20% — a number
   that means nothing. Alerting on noise is how an operator learns to ignore alerts.
2. A **rolling window**, not a single day. One bad afternoon is not a reputation problem.
3. An **already-paused mailbox is left alone**, so the alert fires once rather than every pass.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Classification, Mailbox, MailboxHealth, Message
from app.services.alerts import raise_alert

logger = logging.getLogger(__name__)

#: Days of history the decision reads. Long enough that one bad afternoon does not pause a
#: mailbox, short enough that a real problem is caught in days rather than weeks.
WINDOW_DAYS = 7

#: Below this many sends in the window, no rate is believed at all. At five sends one bounce is
#: 20%, which is not a signal about anything.
MIN_SENDS = 20

#: Bounce rate at which a mailbox is paused. Deliverability guidance clusters around 2-5%;
#: this takes the lenient end, because the action here is automatic and stopping somebody's
#: campaign wrongly is worse than stopping it a day later.
MAX_BOUNCE_RATE = 0.05


@dataclass
class HealthReport:
    """What one pass did, for the scheduler's log and for tests."""

    rows_written: int = 0
    paused: list[uuid.UUID] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


def _ist_day(moment: datetime) -> date:
    """The IST calendar day a moment belongs to.

    IST is a fixed +05:30 with no daylight saving, so this needs no zone database — and
    `domain/scheduling` is where every other IST conversion lives, which is why this is the only
    arithmetic of its kind outside it.
    """
    return (moment.astimezone(UTC) + timedelta(hours=5, minutes=30)).date()


async def roll_up(
    session: AsyncSession, *, days: int = WINDOW_DAYS, now: datetime | None = None
) -> int:
    """Recompute the last `days` of health rows. Returns how many rows were written.

    Recomputed rather than appended, so a row written while a day was still in progress is
    corrected by the next pass instead of being wrong until somebody notices.
    """
    moment = now or datetime.now(tz=UTC)
    since = moment - timedelta(days=days)

    sent_rows = (
        await session.execute(
            select(
                Message.org_id,
                Message.mailbox_id,
                Message.sent_at,
            ).where(
                Message.direction == "out",
                Message.status == "sent",
                Message.mailbox_id.is_not(None),
                Message.sent_at.is_not(None),
                Message.sent_at >= since,
            )
        )
    ).all()

    # A bounce is an *inbound* message, classified. The mailbox is the one that received it,
    # which is the same mailbox that sent the message it is bouncing.
    bounce_rows = (
        await session.execute(
            select(Message.org_id, Message.mailbox_id, Message.created_at)
            .join(Classification, Classification.message_id == Message.id)
            .where(
                Message.direction == "in",
                Message.mailbox_id.is_not(None),
                Message.created_at >= since,
                Classification.label == "bounce",
            )
        )
    ).all()

    counts: dict[tuple[uuid.UUID, uuid.UUID, date], dict[str, int]] = {}
    # Both columns are nullable in the schema and the queries above exclude nulls, but the
    # types cannot know that -- so the narrowing is explicit rather than asserted. A row that
    # somehow has neither is skipped rather than crashing a housekeeping pass.
    for org_id, mailbox_id, moment_sent in sent_rows:
        if mailbox_id is None or moment_sent is None:
            continue
        key = (org_id, mailbox_id, _ist_day(moment_sent))
        counts.setdefault(key, {"sent": 0, "bounced": 0})["sent"] += 1
    for org_id, mailbox_id, received in bounce_rows:
        if mailbox_id is None or received is None:
            continue
        key = (org_id, mailbox_id, _ist_day(received))
        counts.setdefault(key, {"sent": 0, "bounced": 0})["bounced"] += 1

    for (org_id, mailbox_id, day), tally in counts.items():
        statement = insert(MailboxHealth).values(
            org_id=org_id,
            mailbox_id=mailbox_id,
            day=day,
            sent=tally["sent"],
            bounced=tally["bounced"],
            source="messages",
        )
        await session.execute(
            statement.on_conflict_do_update(
                constraint="uq_mailbox_health_mailbox_day",
                set_={"sent": statement.excluded.sent, "bounced": statement.excluded.bounced},
            )
        )
    return len(counts)


@dataclass(frozen=True)
class Window:
    sent: int
    bounced: int

    @property
    def bounce_rate(self) -> float:
        return self.bounced / self.sent if self.sent else 0.0

    @property
    def believable(self) -> bool:
        """Whether there is enough volume for the rate to mean anything."""
        return self.sent >= MIN_SENDS


async def window_for(
    session: AsyncSession, *, mailbox_id: uuid.UUID, now: datetime | None = None
) -> Window:
    """The rolling window's totals for one mailbox."""
    moment = now or datetime.now(tz=UTC)
    cutoff = _ist_day(moment) - timedelta(days=WINDOW_DAYS)
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(MailboxHealth.sent), 0),
                func.coalesce(func.sum(MailboxHealth.bounced), 0),
            ).where(MailboxHealth.mailbox_id == mailbox_id, MailboxHealth.day >= cutoff)
        )
    ).one()
    return Window(sent=int(row[0]), bounced=int(row[1]))


async def pause_unhealthy(session: AsyncSession, *, now: datetime | None = None) -> HealthReport:
    """Pause any mailbox whose bounce rate has crossed the limit, and say so.

    **Only pauses.** There is no branch here that resumes a mailbox, raises a cap or touches a
    send mode, and `tests/integration/test_mailbox_health.py` asserts that over the source.
    """
    report = HealthReport()
    moment = now or datetime.now(tz=UTC)

    mailboxes = (await session.scalars(select(Mailbox).where(Mailbox.paused.is_(False)))).all()
    for mailbox in mailboxes:
        window = await window_for(session, mailbox_id=mailbox.id, now=moment)
        if not window.believable or window.bounce_rate <= MAX_BOUNCE_RATE:
            continue

        mailbox.paused = True
        report.paused.append(mailbox.id)
        percent = window.bounce_rate * 100
        message = (
            f"Mailbox paused: {window.bounced} of {window.sent} messages bounced in the last "
            f"{WINDOW_DAYS} days ({percent:.1f}%), over the {MAX_BOUNCE_RATE * 100:.0f}% limit. "
            "Check the addresses being imported before resuming it."
        )
        report.alerts.append(message)
        # **The mailbox id is in the alert kind**, not only in its detail. The cool-down dedupes
        # by kind, so without it the first mailbox paused would silence every other mailbox in
        # the deployment -- and the one that never got its alert is the one still bouncing. The
        # same reasoning as the OAuth grant warnings (ADR-021).
        await raise_alert(
            session,
            org_id=mailbox.org_id,
            kind=f"mailbox_bounce_rate:{mailbox.id}",
            message=message,
            severity="warning",
            # Counts and a rate, never an address: this reaches logs and a screenshot.
            detail={
                "mailbox_id": str(mailbox.id),
                "sent": window.sent,
                "bounced": window.bounced,
                "window_days": WINDOW_DAYS,
            },
            now=moment,
        )
        logger.warning(
            "mailbox paused on bounce rate",
            extra={"mailbox_id": str(mailbox.id), "bounce_rate": round(window.bounce_rate, 4)},
        )

    return report


async def run_health_pass(session: AsyncSession, *, now: datetime | None = None) -> HealthReport:
    """Roll up, then decide. One call for the `Scheduler`."""
    report = HealthReport()
    report.rows_written = await roll_up(session, now=now)
    decided = await pause_unhealthy(session, now=now)
    report.paused = decided.paused
    report.alerts = decided.alerts
    return report
