"""Reusing one search answer, so a repeat question costs nobody a credit (F17, ADR-046).

**This is the half of F17 that saves money.** The chain stopping at the first provider that
answers decides *which* key is spent; this decides whether any key is spent at all. A hit calls
no provider and records no usage, which is the slice's second acceptance criterion.

Two rules carry the design, and both are about telling an answer from the absence of one:

1. **A confirmed empty is an answer and is cached.** A provider that answered and found nothing
   has told us something true about the company, and the next lead there should not pay to be
   told it again.
2. **A chain where every member failed is not an answer and is never cached.** Storing it would
   let one provider outage stop a workspace searching for a week -- the same failure as a wrong
   cache key serving one company's findings for another, in the cheaper direction.

The two are told apart by which provider answered, which is why `store` requires a name.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SEARCH_CACHE_DAYS, SearchCache
from app.services.research.search import SearchHit

#: The column is 300 characters; a query longer than this is truncated rather than refused, so a
#: pathological company name costs a slightly blunter cache key instead of an exception on a
#: housekeeping path.
MAX_QUERY_CHARS = 300

_SPACES = re.compile(r"\s+")


def normalise(query: str) -> str:
    """The cache key for a question.

    Case and whitespace are flattened, because `Acme  Analytics company news` and
    `acme analytics company news` are one question and two cache rows would be two credits. No
    stemming and no word reordering: those would make two genuinely different questions collide,
    and a cache that answers the wrong question is worse than one that misses.
    """
    return _SPACES.sub(" ", (query or "").strip().lower())[:MAX_QUERY_CHARS]


@dataclass(frozen=True)
class CachedAnswer:
    """A stored answer, with enough to say where it came from and how old it is."""

    provider: str
    hits: list[SearchHit]
    fetched_at: datetime

    @property
    def found_nothing(self) -> bool:
        return not self.hits


async def lookup(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    query: str,
    now: datetime | None = None,
) -> CachedAnswer | None:
    """This workspace's unexpired answer to that question, or None."""
    key = normalise(query)
    if not key:
        return None
    moment = now or datetime.now(tz=UTC)
    row = await session.scalar(
        select(SearchCache).where(
            SearchCache.org_id == org_id,
            SearchCache.query_norm == key,
            SearchCache.expires_at > moment,
        )
    )
    if row is None:
        return None
    return CachedAnswer(
        provider=row.provider,
        hits=[
            SearchHit(
                title=str(hit.get("title") or ""),
                url=str(hit.get("url") or ""),
                snippet=str(hit.get("snippet") or ""),
            )
            for hit in (row.hits or [])
            if hit.get("url")
        ],
        fetched_at=row.fetched_at,
    )


async def store(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    query: str,
    provider: str,
    hits: list[SearchHit],
    now: datetime | None = None,
) -> SearchCache | None:
    """Keep one provider's answer. Replaces this workspace's previous answer to the question.

    Replacing rather than inserting, because the unique key is the question: a second row would
    be refused by the database, and a cache that errors on its second write is a cache that
    silently stops being one.
    """
    key = normalise(query)
    if not key:
        return None
    moment = now or datetime.now(tz=UTC)
    payload = [
        {"title": hit.title, "url": hit.url, "snippet": hit.snippet} for hit in hits if hit.url
    ]
    expires = moment + timedelta(days=SEARCH_CACHE_DAYS)

    existing = await session.scalar(
        select(SearchCache).where(SearchCache.org_id == org_id, SearchCache.query_norm == key)
    )
    if existing is not None:
        existing.provider = provider
        existing.hits = payload
        existing.fetched_at = moment
        existing.expires_at = expires
        await session.flush()
        return existing

    row = SearchCache(
        org_id=org_id,
        query_norm=key,
        provider=provider,
        hits=payload,
        fetched_at=moment,
        expires_at=expires,
    )
    session.add(row)
    await session.flush()
    return row


async def purge_expired(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 1000
) -> int:
    """Delete answers nobody may use any more. Called from the hourly retention sweep.

    Deleted rather than kept, unlike `domain_research`, and the asymmetry is the point: those
    rows hold findings a sent email cited and are part of the record of what we said. A search
    answer nobody may serve is a copy of somebody else's index with no audit value at all.
    """
    moment = now or datetime.now(tz=UTC)
    rows = (
        await session.scalars(
            select(SearchCache).where(SearchCache.expires_at <= moment).limit(limit)
        )
    ).all()
    for row in rows:
        await session.delete(row)
    if rows:
        await session.flush()
    return len(rows)
