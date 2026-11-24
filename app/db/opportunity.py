"""The single writer of ``opportunities.status`` (S19.2a, ADR-069).

Everything that changes an opportunity's status goes through ``transition()``.
It locks the row, checks the move against ``app.domain.opportunity``, updates
the status, and writes an ``OpportunityEvent`` row — all inside the caller's
transaction.  Nothing else may write ``opportunities.status``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Opportunity, OpportunityEvent
from app.domain.opportunity import (
    CLOSED_REASONS,
    OpportunityStatus,
    assert_allowed,
)

ActorKind = Literal["user", "extension", "system"]


class OpportunityNotFound(Exception):
    def __init__(self, opportunity_id: uuid.UUID) -> None:
        self.opportunity_id = opportunity_id
        super().__init__(f"opportunity {opportunity_id} not found in this org")


class MissingClosedReason(ValueError):
    pass


async def lock_opportunity(
    session: AsyncSession, org_id: uuid.UUID, opportunity_id: uuid.UUID
) -> Opportunity:
    """Fetch an opportunity ``FOR UPDATE``, scoped to its org."""
    result = await session.execute(
        select(Opportunity)
        .where(Opportunity.id == opportunity_id, Opportunity.org_id == org_id)
        .with_for_update()
    )
    opp = result.scalar_one_or_none()
    if opp is None:
        raise OpportunityNotFound(opportunity_id)
    return opp


async def transition(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    to_status: OpportunityStatus,
    actor_kind: ActorKind = "system",
    actor_id: str | None = None,
    closed_reason: str | None = None,
    data: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> Opportunity:
    """Move an opportunity to ``to_status``, or raise.

    Raises:
        IllegalTransition: If the domain graph forbids the move.
        OpportunityNotFound: If the opportunity does not exist in this org.
        MissingClosedReason: If closing without a reason.

    The caller's transaction must still be committed; this function deliberately
    does not commit, so a transition can be combined with other writes.
    """
    if to_status == OpportunityStatus.CLOSED:
        if not closed_reason:
            raise MissingClosedReason("closing an opportunity requires a closed_reason")
        if closed_reason not in CLOSED_REASONS:
            raise ValueError(f"closed_reason must be one of: {', '.join(CLOSED_REASONS)}")

    _now = now or datetime.now(tz=UTC)
    opp = await lock_opportunity(session, org_id, opportunity_id)
    from_status = OpportunityStatus(opp.status)

    assert_allowed(from_status, to_status)

    opp.status = to_status.value
    opp.updated_at = _now
    if to_status == OpportunityStatus.CLOSED:
        opp.closed_reason = closed_reason

    event = OpportunityEvent(
        org_id=org_id,
        opportunity_id=opportunity_id,
        occurred_at=_now,
        actor_kind=actor_kind,
        event_kind="opportunity.status_changed",
        from_status=from_status.value,
        to_status=to_status.value,
        payload={"actor_id": actor_id, **(data or {})},
    )
    session.add(event)
    return opp
