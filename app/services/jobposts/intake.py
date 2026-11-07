"""Turning postings into `job_posts` rows (F5).

Upserts on `(org_id, source, external_id)`, so re-polling a board updates what changed rather
than duplicating it, and a posting that disappears from a feed is left alone: a candidate may
have applied to it and `applications` will point at it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobPost
from app.db.repo import OrgContext
from app.services.jobposts import boards
from app.services.jobposts.feeds import MAX_BODY_CHARS, Posting


@dataclass
class IntakeResult:
    created: int = 0
    updated: int = 0
    #: Every row this call stored or refreshed. Carried so a caller that needs the postings
    #: themselves -- F11's signal producer -- does not have to query them back by their keys,
    #: which would be a second place that decides what "the postings from this poll" means.
    #:
    #: **Updated rows are included, and deliberately.** A posting seen again is not a second
    #: signal: the signal's own unique key refuses that. But a company watched *after* its
    #: postings were first stored would otherwise never produce a signal at all, because the
    #: first poll after the watch is a poll of rows it has already seen.
    rows: list[JobPost] = field(default_factory=list)


def domain_of(url: str) -> str | None:
    """The company's own domain, or None when the URL belongs to a job board (ADR-047).

    This used to return the URL's host unconditionally, which for every feed posting is the ATS
    vendor. `boards.company_domain_of` is now the one place that decision is made, so the
    intake, the resolver and any future caller cannot disagree about what a company domain is.
    """
    return boards.company_domain_of(url)


def pasted_id(url: str) -> str:
    """A stable id for a posting with no board id of its own.

    The URL hashed rather than stored as the id, so the unique key stays short and a URL with a
    tracking parameter does not become a second posting.
    """
    cleaned = urlsplit(url.strip())._replace(query="", fragment="").geturl().rstrip("/").lower()
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:32]


async def store(
    session: AsyncSession, ctx: OrgContext, postings: list[Posting], *, now: datetime | None = None
) -> IntakeResult:
    """Upsert postings for one workspace."""
    moment = now or datetime.now(tz=UTC)
    result = IntakeResult()

    for posting in postings:
        existing = await session.scalar(
            select(JobPost).where(
                JobPost.org_id == ctx.org_id,
                JobPost.source == posting.source,
                JobPost.external_id == posting.external_id,
            )
        )
        body = (posting.body_text or "")[:MAX_BODY_CHARS] or None
        if existing is not None:
            existing.title = posting.title
            existing.location = posting.location
            existing.apply_url = posting.apply_url
            existing.posted_at = posting.posted_at or existing.posted_at
            existing.fetched_at = moment
            # Only overwrite stored text with text: a feed that stops carrying descriptions must
            # not blank what we already have.
            if body:
                existing.body_text = body
            result.updated += 1
            result.rows.append(existing)
            continue

        row = JobPost(
            org_id=ctx.org_id,
            source=posting.source,
            external_id=posting.external_id,
            company_domain=domain_of(posting.apply_url),
            board_slug=boards.slug_from_url(posting.apply_url),
            title=posting.title,
            location=posting.location,
            apply_url=posting.apply_url,
            posted_at=posting.posted_at,
            body_text=body,
            fetched_at=moment,
        )
        session.add(row)
        result.created += 1
        result.rows.append(row)

    await session.flush()
    return result


async def store_pasted(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    url: str,
    title: str | None = None,
    body_text: str | None = None,
    now: datetime | None = None,
) -> tuple[JobPost, bool]:
    """A posting the user is reading. Returns `(row, created)`.

    The primary path for boards with no public feed -- Naukri, Instahyre, a LinkedIn post -- and
    the reason it exists is that nothing here scrapes them. The user supplies the text.
    """
    cleaned = url.strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        raise ValueError("a posting URL must be http or https")

    external_id = pasted_id(cleaned)
    existing = await session.scalar(
        select(JobPost).where(
            JobPost.org_id == ctx.org_id,
            JobPost.source == "pasted",
            JobPost.external_id == external_id,
        )
    )
    moment = now or datetime.now(tz=UTC)
    body = (body_text or "").strip()[:MAX_BODY_CHARS] or None

    if existing is not None:
        existing.fetched_at = moment
        if title:
            existing.title = title.strip()[:500]
        if body:
            existing.body_text = body
        await session.flush()
        return existing, False

    row = JobPost(
        org_id=ctx.org_id,
        source="pasted",
        external_id=external_id,
        company_domain=domain_of(cleaned),
        board_slug=boards.slug_from_url(cleaned),
        title=(title or "").strip()[:500] or "(untitled, pasted)",
        apply_url=cleaned,
        body_text=body,
        fetched_at=moment,
    )
    session.add(row)
    await session.flush()
    return row, True


async def list_posts(session: AsyncSession, ctx: OrgContext, *, limit: int = 50) -> list[JobPost]:
    rows = await session.scalars(
        select(JobPost)
        .where(JobPost.org_id == ctx.org_id, JobPost.status == "open")
        .order_by(JobPost.fetched_at.desc(), JobPost.id.desc())
        .limit(limit)
    )
    return list(rows)


async def archive(session: AsyncSession, ctx: OrgContext, *, post_id: uuid.UUID) -> bool:
    """Archive one posting. Scoped in the WHERE, so another workspace's id resolves to nothing."""
    row = await session.scalar(
        select(JobPost).where(JobPost.id == post_id, JobPost.org_id == ctx.org_id)
    )
    if row is None:
        return False
    row.status = "archived"
    return True


@dataclass
class DiffResult:
    """What one complete poll of a board changed.

    `closed` is the half F22 adds: a posting that was in yesterday's feed and is not in today's.
    """

    closed: int = 0
    reopened: int = 0
    closed_titles: list[str] = field(default_factory=list)


async def close_missing(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    source: str,
    slug: str,
    seen: set[str],
    now: datetime | None = None,
) -> DiffResult:
    """Archive the open postings of one board that this poll did not see (F22, ADR-054).

    **Only ever called with the authoritative id set from a poll that parsed successfully**, and
    that is the whole safety argument. A fetch that failed raises `FeedError` long before here,
    so this is never reached with an accidentally empty set -- which would archive a company's
    entire board on one bad afternoon and report it as every role being filled. The job-post
    intake has already paid for the neighbouring version of this mistake once: an unknown source
    with a plausible payload returned zero postings, which read as an empty board.

    A genuinely empty board is still an empty board. A company that closed its last role is a
    real and interesting event, and refusing to believe it would be second-guessing the only
    evidence we have.

    **Archived, never deleted.** `applications` may point at a posting the candidate applied to,
    and the record of what they applied to must survive the role being filled. `status` has said
    `open | archived` since F5 and nothing ever wrote the second value.
    """
    moment = now or datetime.now(tz=UTC)
    result = DiffResult()

    rows = (
        await session.scalars(
            select(JobPost).where(
                JobPost.org_id == ctx.org_id,
                JobPost.source == source,
                JobPost.board_slug == slug,
            )
        )
    ).all()

    for row in rows:
        present = row.external_id in seen
        if present and row.status == "archived":
            # A role relisted after being filled. Worth distinguishing from a new posting: the
            # id is the board's own, so this is the same role rather than a second one.
            row.status = "open"
            row.fetched_at = moment
            result.reopened += 1
        elif not present and row.status == "open":
            row.status = "archived"
            result.closed += 1
            result.closed_titles.append(row.title)

    await session.flush()
    return result
