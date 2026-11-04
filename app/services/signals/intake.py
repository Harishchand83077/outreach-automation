"""Recording a signal, and matching it to a lead this workspace already has (F11, ADR-049).

**The one rule: a signal proposes, and never creates a lead.** Nothing here writes to `leads`,
and a signal that matches nobody is the interesting case rather than the error case -- it is a
company worth approaching that this workspace has no contact at, which is exactly what F9's
lookup button is for.

Matching is deliberately narrow. A lead belongs to a signal's company when the workspace already
has one at that **domain**, which is a fact rather than an inference: the lead's own
`company_url` resolved through the same key `domain_research` uses (ADR-011). No fuzzy name
matching, because the cost of a wrong match here is a reason-to-write attached to the wrong
person, and the person sending the email would have no way to see it was wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import JobPost, Lead, SignalEvent, WatchlistEntry
from app.db.repo import OrgContext
from app.domain.state import TERMINAL
from app.services.research.service import cache_key
from app.services.signals import watchlist


@dataclass
class RecordResult:
    created: list[SignalEvent] = field(default_factory=list)
    already_known: int = 0

    @property
    def anything(self) -> bool:
        return bool(self.created)


@dataclass(frozen=True)
class Candidate:
    """One thing that happened, before it is known whether we have seen it."""

    kind: str
    source: str
    external_id: str
    title: str
    url: str | None = None
    observed_at: datetime | None = None
    job_post_id: uuid.UUID | None = None


async def _lead_for(session: AsyncSession, *, org_id: uuid.UUID, domain: str) -> uuid.UUID | None:
    """A live lead at that company, or None.

    **Terminal leads are excluded**, and that is the decision in this function. A lead who
    opted out, bounced or was marked not interested is not somebody a new reason to write makes
    approachable again -- attaching a signal to them would put a suggestion on screen next to a
    person we have already been told to leave alone. A signal with no lead reads as "nobody here
    yet", which for those companies is the honest answer.
    """
    # Narrowed in SQL and confirmed in Python. `cache_key` strips a scheme, a `www.` and a
    # path, which SQL cannot do -- but a lead at this company must mention the domain somewhere
    # in its `company_url`, so the `LIKE` turns "load every lead" into "load the few that could
    # match" without being trusted to decide anything.
    rows = await session.scalars(
        select(Lead).where(
            Lead.org_id == org_id,
            Lead.state.not_in([state.value for state in TERMINAL]),
            Lead.company_url.ilike(f"%{domain}%"),
        )
    )
    for lead in rows.all():
        if cache_key(lead.company_url) == domain:
            return lead.id
    return None


async def record(
    session: AsyncSession,
    ctx: OrgContext,
    entry: WatchlistEntry,
    candidates: list[Candidate],
    *,
    now: datetime | None = None,
) -> RecordResult:
    """Store what happened at one watched company. Idempotent on the source's own id.

    A re-poll sees yesterday's postings again every single time it runs, so without the unique
    key every morning would add a fresh copy of every open role and the list would be unusable
    within a week. The key is checked here as well as in the database, because an
    `IntegrityError` inside a worker pass would roll back the postings stored beside it.
    """
    moment = now or datetime.now(tz=UTC)
    result = RecordResult()
    if entry.paused:
        return result

    lead_id = await _lead_for(session, org_id=ctx.org_id, domain=entry.domain)

    for candidate in candidates:
        existing = await session.scalar(
            select(SignalEvent).where(
                SignalEvent.org_id == ctx.org_id,
                SignalEvent.kind == candidate.kind,
                SignalEvent.source == candidate.source,
                SignalEvent.external_id == candidate.external_id,
            )
        )
        if existing is not None:
            result.already_known += 1
            continue

        row = SignalEvent(
            org_id=ctx.org_id,
            watchlist_id=entry.id,
            domain=entry.domain,
            kind=candidate.kind,
            source=candidate.source,
            external_id=candidate.external_id,
            title=candidate.title[:500],
            url=candidate.url,
            job_post_id=candidate.job_post_id,
            # Matched, never created. A signal is not a reason to put somebody in a sending
            # pipeline; it is a reason to write to somebody who is already in one.
            lead_id=lead_id,
            state="new",
            observed_at=candidate.observed_at,
        )
        session.add(row)
        result.created.append(row)

    if result.created:
        await watchlist.touch(session, entry, now=moment)
    await session.flush()
    return result


async def from_postings(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    source: str,
    slug: str,
    postings: list[JobPost],
    now: datetime | None = None,
) -> RecordResult:
    """Turn postings from one board into hiring signals, if a watch reads that board.

    **This is the producer, and it exists in this slice on purpose.** A table with no writer is
    how `search` came to be wired to nothing for three phases, and how a `Draft(kind="followup")`
    was never once created -- so `signal_events` ships with the one producer the system can
    already feed it, rather than waiting for S15.5's richer poll.

    A posting is a hiring signal and nothing more. It is not read, not summarised and not sent to
    a model: the title and the link are what a person needs to decide whether this company is
    worth an email today.
    """
    entry = await watchlist.for_board(session, org_id=ctx.org_id, source=source, slug=slug)
    if entry is None:
        # The board is polled for the job-hunt side and nobody is watching the company for
        # outreach. Not an error: the two uses of a board are independent.
        return RecordResult()

    return await record(
        session,
        ctx,
        entry,
        [
            Candidate(
                kind="hiring",
                source=post.source,
                external_id=post.external_id,
                title=post.title,
                url=post.apply_url,
                observed_at=post.posted_at,
                job_post_id=post.id,
            )
            for post in postings
        ],
        now=now,
    )


async def set_state(
    session: AsyncSession, ctx: OrgContext, *, signal_id: uuid.UUID, state: str
) -> SignalEvent | None:
    """Mark a signal acted on or dismissed.

    There is no transition back to `new`: "I have looked at this" is not something somebody
    un-does, and a list that can be reset is a list nobody trusts to shrink.
    """
    row = await session.scalar(
        select(SignalEvent).where(SignalEvent.id == signal_id, SignalEvent.org_id == ctx.org_id)
    )
    if row is None:
        return None
    row.state = state
    await session.flush()
    return row


async def unresolved(
    session: AsyncSession, ctx: OrgContext, *, limit: int = 100
) -> list[SignalEvent]:
    rows = await session.scalars(
        select(SignalEvent)
        .where(SignalEvent.org_id == ctx.org_id, SignalEvent.state == "new")
        .order_by(SignalEvent.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    return list(rows.all())


__all__ = [
    "Candidate",
    "RecordResult",
    "from_postings",
    "record",
    "set_state",
    "unresolved",
]
