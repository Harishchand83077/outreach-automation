"""The companies a workspace wants watched (F11, ADR-049).

Adding a company here does two things and only two: it says which domain's news matters, and --
when the company has a board we can read -- it makes sure that board is being polled. The second
is the part worth being careful about, because the poll already exists for the job-hunt side and
**a watch must not quietly create a second one.**
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WatchedBoard, WatchlistEntry
from app.db.repo import OrgContext
from app.services.jobposts.feeds import SOURCES
from app.services.research.net import company_domain

#: The boards the daily poll can read. `pasted` is in `SOURCES` and is not a board.
POLLABLE = tuple(name for name in SOURCES if name != "pasted")


class WatchRefused(Exception):
    """A watch that cannot be stored, with a sentence saying why."""


def normalise_domain(raw: str | None) -> str:
    """The registrable domain of whatever the person typed.

    They paste a URL as often as a domain, and `https://acme.example/careers` and `acme.example`
    are one company. Delegates to `net.company_domain` so the watchlist, the research cache and
    the knowledge graph cannot disagree about what a company is -- this used to be its own copy,
    and a second copy in the graph promptly behaved differently.
    """
    return company_domain(raw)


async def add(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    domain: str,
    label: str | None = None,
    ats_source: str | None = None,
    ats_slug: str | None = None,
    campaign_id: uuid.UUID | None = None,
) -> tuple[WatchlistEntry, bool]:
    """Watch a company. Returns `(row, created)`; watching twice updates rather than fails.

    Idempotent on the domain for the reason `POST /mailboxes` is idempotent on the address: the
    domain *is* the company's identity here, so a second attempt is somebody correcting a label
    or adding a board, not a different company.
    """
    key = normalise_domain(domain)
    if not key or "." not in key:
        raise WatchRefused("that does not look like a company domain.")

    source = (ats_source or "").strip().lower() or None
    slug = (ats_slug or "").strip().strip("/").lower() or None
    if source and source not in POLLABLE:
        raise WatchRefused("the board must be one of " + ", ".join(POLLABLE) + ".")
    if source and not slug:
        raise WatchRefused("a board needs the company's slug on it, as it appears in the URL.")
    if slug and not source:
        raise WatchRefused("a slug needs the board it belongs to.")

    existing = await session.scalar(
        select(WatchlistEntry).where(
            WatchlistEntry.org_id == ctx.org_id, WatchlistEntry.domain == key
        )
    )
    if existing is not None:
        existing.label = (label or "").strip()[:300] or existing.label
        if source:
            existing.ats_source = source
            existing.ats_slug = slug
        if campaign_id is not None:
            existing.campaign_id = campaign_id
        await _ensure_board(session, ctx, existing)
        await session.flush()
        return existing, False

    row = WatchlistEntry(
        org_id=ctx.org_id,
        domain=key,
        label=(label or "").strip()[:300] or None,
        ats_source=source,
        ats_slug=slug,
        campaign_id=campaign_id,
    )
    session.add(row)
    await session.flush()
    await _ensure_board(session, ctx, row)
    await session.flush()
    return row, True


async def _ensure_board(
    session: AsyncSession, ctx: OrgContext, entry: WatchlistEntry
) -> WatchedBoard | None:
    """Make sure the board behind a watch is in the daily poll, without adding a second one.

    **One board row per workspace per board**, which `uq_watched_boards_org_source_slug` already
    enforces -- so this looks first and inserts only when there is nothing there. Without the
    lookup, watching a company whose board the user already follows for job hunting would raise
    an integrity error on a path nobody expects one from.

    The poll itself is untouched: it reads `watched_boards` and stores postings exactly as it
    did. A watch is a *reason to read* a board, not a second way of reading it.
    """
    if not entry.ats_source or not entry.ats_slug:
        return None

    existing = await session.scalar(
        select(WatchedBoard).where(
            WatchedBoard.org_id == ctx.org_id,
            WatchedBoard.source == entry.ats_source,
            WatchedBoard.slug == entry.ats_slug,
        )
    )
    if existing is not None:
        return existing

    board = WatchedBoard(
        org_id=ctx.org_id,
        source=entry.ats_source,
        slug=entry.ats_slug,
        label=entry.label or entry.domain,
    )
    session.add(board)
    return board


async def entries(session: AsyncSession, ctx: OrgContext) -> list[WatchlistEntry]:
    rows = await session.scalars(
        select(WatchlistEntry)
        .where(WatchlistEntry.org_id == ctx.org_id)
        .order_by(WatchlistEntry.created_at.desc())
    )
    return list(rows.all())


async def for_board(
    session: AsyncSession, *, org_id: uuid.UUID, source: str, slug: str
) -> WatchlistEntry | None:
    """The watch, if any, that reads this board. Used by the producer, never by a route."""
    return await session.scalar(
        select(WatchlistEntry).where(
            WatchlistEntry.org_id == org_id,
            WatchlistEntry.ats_source == source,
            WatchlistEntry.ats_slug == slug,
            WatchlistEntry.paused.is_(False),
        )
    )


async def set_paused(
    session: AsyncSession, ctx: OrgContext, *, entry_id: uuid.UUID, paused: bool
) -> WatchlistEntry | None:
    row = await session.scalar(
        select(WatchlistEntry).where(
            WatchlistEntry.id == entry_id, WatchlistEntry.org_id == ctx.org_id
        )
    )
    if row is None:
        return None
    row.paused = paused
    await session.flush()
    return row


async def remove(session: AsyncSession, ctx: OrgContext, *, entry_id: uuid.UUID) -> bool:
    """Stop watching. The signals stay.

    `signal_events.watchlist_id` is `ON DELETE SET NULL` and each signal carries its own
    `domain`, so forgetting a company does not delete the record that it was hiring in March --
    which is what keeps a funnel auditable after somebody tidies a list. The board is left in
    the poll too: it may be there for the job-hunt side, and a watch is not its owner.
    """
    row = await session.scalar(
        select(WatchlistEntry).where(
            WatchlistEntry.id == entry_id, WatchlistEntry.org_id == ctx.org_id
        )
    )
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def touch(
    session: AsyncSession, entry: WatchlistEntry, *, now: datetime | None = None
) -> None:
    entry.last_signal_at = now or datetime.now(tz=UTC)
    await session.flush()
