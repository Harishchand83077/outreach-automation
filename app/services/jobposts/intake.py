"""Turning postings into `job_posts` rows (F5).

Upserts on `(org_id, source, external_id)`, so re-polling a board updates what changed rather
than duplicating it, and a posting that disappears from a feed is left alone: a candidate may
have applied to it and `applications` will point at it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobPost
from app.db.repo import OrgContext
from app.services.jobposts.feeds import MAX_BODY_CHARS, Posting


@dataclass
class IntakeResult:
    created: int = 0
    updated: int = 0


def domain_of(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.") or None


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
            continue

        session.add(
            JobPost(
                org_id=ctx.org_id,
                source=posting.source,
                external_id=posting.external_id,
                company_domain=domain_of(posting.apply_url),
                title=posting.title,
                location=posting.location,
                apply_url=posting.apply_url,
                posted_at=posting.posted_at,
                body_text=body,
                fetched_at=moment,
            )
        )
        result.created += 1

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
