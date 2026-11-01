"""Enqueueing a daily poll per watched board (F5).

Enqueue only -- the fetch happens in the job, because it calls the network and the scheduler must
not wait on it. The dedupe key is `feed:{source}:{slug}`, built from values two workspaces can
share, which is exactly why job deduplication had to be scoped by org first (ADR-032).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import queue
from app.db.models import WatchedBoard
from app.db.queue import JobKind

#: Once a day per board. More often is pointless -- a job board does not change by the minute --
#: and it is somebody else's service.
POLL_EVERY = timedelta(hours=24)


@dataclass
class PollReport:
    enqueued: int = 0
    skipped: int = 0


async def enqueue_due_polls(session: AsyncSession, *, now: datetime | None = None) -> PollReport:
    """Queue a poll for every board not read in the last day."""
    moment = now or datetime.now(tz=UTC)
    cutoff = moment - POLL_EVERY
    report = PollReport()

    boards = (
        await session.scalars(
            select(WatchedBoard).where(
                (WatchedBoard.last_polled_at.is_(None)) | (WatchedBoard.last_polled_at < cutoff)
            )
        )
    ).all()

    for board in boards:
        job = await queue.enqueue(
            session,
            org_id=board.org_id,
            kind=JobKind.POLL_ATS_FEED,
            payload={"source": board.source, "slug": board.slug, "board_id": str(board.id)},
            dedupe_key=f"feed:{board.source}:{board.slug}",
        )
        if job is None:
            # Already queued or running for this workspace. Not an error: the previous poll has
            # not finished, so there is nothing to add.
            report.skipped += 1
            continue
        # Stamped on enqueue rather than on success, so a board whose poll keeps failing is
        # retried once a day instead of every pass.
        board.last_polled_at = moment
        report.enqueued += 1

    return report
