"""Starting and stopping campaigns on their own schedule.

The pure rule is `app/domain/campaign_schedule.py`; this is the pass that applies it, once a
minute, from the `Scheduler`.

Every move writes an `events` row. A campaign that stopped sending because its window closed and
a campaign that stopped because somebody paused it look identical from outside, and the first
question anybody asks is "why did this stop?" -- so the answer goes in the audit trail at the
moment it happens, rather than being reconstructed later from a timestamp and a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign
from app.db.state import record_event
from app.domain.campaign_schedule import MOVEABLE, scheduled_status


@dataclass
class ClockReport:
    started: int = 0
    completed: int = 0
    parked: int = 0
    names: list[str] = field(default_factory=list)


async def run_campaign_clock(session: AsyncSession, *, now: datetime | None = None) -> ClockReport:
    """Move every campaign whose schedule says it should be somewhere else.

    The caller commits. Idempotent: a campaign already in the right status is not written, so
    running this every minute produces one event per actual transition rather than one per
    minute.
    """
    now = now or datetime.now(tz=UTC)
    report = ClockReport()

    rows = (
        await session.execute(
            select(Campaign).where(
                # Only campaigns that have a schedule at all. Without this the pass would read
                # every campaign in every org once a minute to decide to do nothing.
                or_(Campaign.starts_at.is_not(None), Campaign.ends_at.is_not(None)),
                Campaign.status.in_(tuple(MOVEABLE)),
                Campaign.paused.is_(False),
            )
        )
    ).scalars()

    for campaign in rows:
        target = scheduled_status(
            status=campaign.status,
            starts_at=campaign.starts_at,
            ends_at=campaign.ends_at,
            now=now,
        )
        if target == campaign.status:
            continue

        was = campaign.status
        campaign.status = target
        await record_event(
            session,
            org_id=campaign.org_id,
            campaign_id=campaign.id,
            type="campaign.schedule_moved",
            actor="system",
            data={
                "from": was,
                "to": target,
                "starts_at": campaign.starts_at.isoformat() if campaign.starts_at else None,
                "ends_at": campaign.ends_at.isoformat() if campaign.ends_at else None,
            },
        )
        report.names.append(f"{campaign.name}: {was} -> {target}")
        if target == "running":
            report.started += 1
        elif target == "completed":
            report.completed += 1
        else:
            report.parked += 1

    return report
