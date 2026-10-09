"""Org-scoped data access.

Every query goes through an `OrgContext`, so filtering by `org_id` is a parameter the caller
cannot forget rather than a convention they might (ADR-004). The prototype had no tenancy at
all — and `GET /api/leads` returned every lead's full insights, draft and reply to anyone who
asked (issues C1, C7).

Only the queries Phase 1 needs are here. More are added by the phase that needs them, not
guessed at now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Lead, Suppression
from app.domain.state import LeadState

#: Pagination ceiling. No endpoint returns a whole table — the prototype's dashboard polled five
#: endpoints every 2.5 seconds, each returning everything (issue C7).
MAX_PAGE = 200
DEFAULT_PAGE = 50


@dataclass(frozen=True)
class OrgContext:
    """Who is asking, and on whose behalf. Required by every function in this module."""

    org_id: uuid.UUID
    #: For the audit trail: an email address, or None when the system acts on its own.
    actor_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.org_id, uuid.UUID):
            raise TypeError("OrgContext.org_id must be a UUID")


def _page_size(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE
    return max(1, min(limit, MAX_PAGE))


async def get_lead(ctx: OrgContext, session: AsyncSession, lead_id: uuid.UUID) -> Lead | None:
    """A lead, or None. Returns None for another org's lead rather than raising, so a guessed
    UUID is indistinguishable from one that does not exist."""
    return await session.scalar(select(Lead).where(Lead.id == lead_id, Lead.org_id == ctx.org_id))


async def leads_by_state(
    ctx: OrgContext,
    session: AsyncSession,
    *,
    state: LeadState,
    campaign_id: uuid.UUID | None = None,
    limit: int | None = None,
    after_id: uuid.UUID | None = None,
) -> list[Lead]:
    """One page of leads in a given state, ordered for stable cursor paging."""
    stmt = select(Lead).where(Lead.org_id == ctx.org_id, Lead.state == state.value)
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    if after_id is not None:
        stmt = stmt.where(Lead.id > after_id)
    result = await session.execute(stmt.order_by(Lead.id).limit(_page_size(limit)))
    return list(result.scalars().all())


async def leads_due(
    ctx: OrgContext,
    session: AsyncSession,
    *,
    states: list[LeadState],
    now: datetime | None = None,
    limit: int | None = None,
) -> list[Lead]:
    """Leads whose `next_action_at` has arrived.

    "Follow-up due" is computed from (state, next_action_at) rather than stored as a state of its
    own, so there is no scheduled-but-stale row to reconcile (ARCHITECTURE_TARGET §6.1).

    Paused leads are excluded here, and paused orgs, campaigns and mailboxes are re-checked at
    send time — pausing is a flag, so it never loses a lead's position.
    """
    now = now or datetime.now(tz=UTC)
    result = await session.execute(
        select(Lead)
        .join(Campaign, Campaign.id == Lead.campaign_id)
        .where(
            Lead.org_id == ctx.org_id,
            Lead.state.in_([s.value for s in states]),
            Lead.next_action_at.is_not(None),
            Lead.next_action_at <= now,
            Lead.paused.is_(False),
            Campaign.paused.is_(False),
            Campaign.status == "running",
        )
        .order_by(Lead.next_action_at)
        .limit(_page_size(limit))
    )
    return list(result.scalars().all())


async def count_leads_by_state(
    ctx: OrgContext, session: AsyncSession, *, campaign_id: uuid.UUID | None = None
) -> dict[str, int]:
    """The funnel. Counted in the database, not by fetching rows and tallying them in Python."""
    stmt = select(Lead.state, func.count()).where(Lead.org_id == ctx.org_id).group_by(Lead.state)
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    result = await session.execute(stmt)
    return {state: int(count) for state, count in result.all()}


async def add_suppression(
    ctx: OrgContext,
    session: AsyncSession,
    *,
    value: str,
    reason: str,
    kind: str = "email",
    source_message_id: uuid.UUID | None = None,
) -> Suppression:
    """Suppress an address or domain, idempotently.

    Re-suppressing something is a no-op rather than an error: an opt-out may be detected twice —
    once by keyword, once by the classifier — and both paths must be safe to run.
    """
    normalised = value.strip().lower()
    existing = await session.scalar(
        select(Suppression).where(
            Suppression.org_id == ctx.org_id,
            Suppression.kind == kind,
            Suppression.value == normalised,
        )
    )
    if existing is not None:
        return existing

    record = Suppression(
        org_id=ctx.org_id,
        kind=kind,
        value=normalised,
        reason=reason,
        source_message_id=source_message_id,
    )
    session.add(record)
    await session.flush()
    return record


async def list_suppressions(
    ctx: OrgContext, session: AsyncSession, *, limit: int | None = None
) -> list[Suppression]:
    result = await session.execute(
        select(Suppression)
        .where(Suppression.org_id == ctx.org_id)
        .order_by(Suppression.created_at.desc())
        .limit(_page_size(limit))
    )
    return list(result.scalars().all())
