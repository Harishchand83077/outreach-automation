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
from app.db.models import WatchedBoard, WatchlistEntry
from app.db.queue import JobKind

#: Once a day per board. More often is pointless -- a job board does not change by the minute --
#: and it is somebody else's service.
POLL_EVERY = timedelta(hours=24)


@dataclass
class PollReport:
    enqueued: int = 0
    skipped: int = 0
    #: Page polls, counted separately so a log line can say which half did nothing.
    pages_enqueued: int = 0
    pages_skipped: int = 0


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

    await _enqueue_page_polls(session, report, moment=moment, cutoff=cutoff)
    return report


async def _enqueue_page_polls(
    session: AsyncSession, report: PollReport, *, moment: datetime, cutoff: datetime
) -> None:
    """Queue a hash check for every watched careers page not read in the last day (F22).

    A **paused** page is skipped and a **refused** one is not: `refused_reason` is set when a
    site's `robots.txt` says no, and that can change -- a watch that stopped being tried for
    ever would never notice. Trying once a day is the same frequency we would have used anyway,
    and the fetch asks `robots.txt` first every time, so a site that still says no is still not
    read.
    """
    pages = (
        await session.scalars(
            select(WatchlistEntry).where(
                WatchlistEntry.careers_url.is_not(None),
                WatchlistEntry.paused.is_(False),
                (WatchlistEntry.page_polled_at.is_(None))
                | (WatchlistEntry.page_polled_at < cutoff),
            )
        )
    ).all()

    for page in pages:
        job = await queue.enqueue(
            session,
            org_id=page.org_id,
            kind=JobKind.POLL_CAREERS_PAGE,
            payload={"watchlist_id": str(page.id)},
            # Keyed on the watch rather than on the URL: two workspaces watching one careers
            # page are two watches, and ADR-032 scoped dedupe by org precisely so a key two
            # workspaces share cannot silently suppress one of them.
            dedupe_key=f"page:{page.id}",
        )
        if job is None:
            report.pages_skipped += 1
            continue
        page.page_polled_at = moment
        report.pages_enqueued += 1
