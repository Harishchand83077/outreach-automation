"""The shared public-company cache (F19, ADR-038).

Keyed on the registrable domain, which is also the research cache key (ADR-011) — the key that
had to be fixed once already, because keying on the address domain meant every Gmail-addressed
lead in a workspace shared one row and the second was served the first company's findings.

**Reads cost no slot; a fetch does.** Each workspace gets a daily allowance of fetches, so one
workspace importing a thousand new companies cannot spend everybody's budget. A cache hit is
free, which is the entire point of sharing.

**A refresh past the age limit does nothing when the page has not changed.** The content hash is
compared before any field is written, so an unchanged page costs one fetch and no model call,
and `version` only moves on a real change.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CompanyProfile, SearchUsage

#: How long a profile is considered current. Public company facts are stable -- a careers page is
#: a careers page -- so this is generous; `content_hash` is what avoids work inside the window.
MAX_AGE = timedelta(days=30)

#: Fetches one workspace may trigger per day. A cache hit is not a fetch.
DAILY_FETCH_BUDGET = 50

#: The fields this table is allowed to hold. Written out so `apply` cannot quietly start storing
#: something else, and so the structural test has one list to check against.
PUBLIC_FIELDS = ("name", "careers_url", "ats_type", "ats_slug", "size_band", "summary")


@dataclass(frozen=True)
class CacheHit:
    profile: CompanyProfile
    fresh: bool


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def get(session: AsyncSession, *, domain: str) -> CacheHit | None:
    """The cached profile for a domain, and whether it is still fresh.

    No `org_id` and no `OrgContext`, which is the exception ADR-038 records: there is nothing
    workspace-specific to scope by. A stale hit is still returned, because a careers page from
    five weeks ago beats nothing while a refresh is queued.
    """
    key = domain.strip().lower().removeprefix("www.")
    if not key:
        return None
    row = await session.scalar(select(CompanyProfile).where(CompanyProfile.domain == key))
    if row is None:
        return None
    return CacheHit(profile=row, fresh=datetime.now(tz=UTC) - row.fetched_at < MAX_AGE)


async def fetches_used_today(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime | None = None
) -> int:
    """How many fetches this workspace has spent today.

    Read from `search_usage`, which already records what was spent and on whose behalf, so
    there is no second ledger to keep in step with the first.
    """
    moment = now or datetime.now(tz=UTC)
    since = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    # **Credits summed, not rows counted**, because credits are what the provider bills and one
    # search can cost several. The column is `at`, not `created_at` -- the same naming trap as
    # `Event.at`, caught by the type checker for the third time in this project.
    used = await session.scalar(
        select(func.coalesce(func.sum(SearchUsage.credits), 0)).where(
            SearchUsage.org_id == org_id, SearchUsage.at >= since
        )
    )
    return int(used or 0)


async def may_fetch(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime | None = None
) -> bool:
    """Whether this workspace has fetch budget left. A cache hit never asks."""
    return await fetches_used_today(session, org_id=org_id, now=now) < DAILY_FETCH_BUDGET


async def apply(
    session: AsyncSession,
    *,
    domain: str,
    fields: dict[str, str | None],
    sources: dict[str, str],
    page_text: str = "",
    now: datetime | None = None,
) -> tuple[CompanyProfile, bool]:
    """Store or refresh a profile. Returns `(row, changed)`.

    Only `PUBLIC_FIELDS` are written, whatever the caller passes: this table is shared, and a
    caller that could add a field could add a workspace's private note to it.
    """
    key = domain.strip().lower().removeprefix("www.")
    if not key:
        raise ValueError("a company profile needs a domain")

    moment = now or datetime.now(tz=UTC)
    digest = content_hash(page_text) if page_text else None
    clean = {name: fields.get(name) for name in PUBLIC_FIELDS if fields.get(name) is not None}

    row = await session.scalar(select(CompanyProfile).where(CompanyProfile.domain == key))
    if row is None:
        row = CompanyProfile(
            domain=key, sources=dict(sources), content_hash=digest, fetched_at=moment, version=1
        )
        for name, value in clean.items():
            setattr(row, name, value)
        session.add(row)
        await session.flush()
        return row, True

    # An unchanged page costs the fetch and nothing else: no field write, no version bump, so a
    # consumer holding version N can trust that N still means what it meant.
    if digest is not None and digest == row.content_hash:
        row.fetched_at = moment
        await session.flush()
        return row, False

    changed = False
    for name, value in clean.items():
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    if sources:
        row.sources = {**dict(row.sources or {}), **sources}
    row.content_hash = digest or row.content_hash
    row.fetched_at = moment
    if changed:
        row.version += 1
    await session.flush()
    return row, changed
