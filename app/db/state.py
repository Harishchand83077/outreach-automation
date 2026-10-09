"""The single writer of `leads.state`.

Everything that moves a lead goes through `transition()`. It locks the row, checks the move
against `app.domain.state`, updates the lead and writes the `events` row — all in the caller's
transaction, so a state change and its audit record are never separated. Anything else that
writes `leads.state` is a bug.

This is the structural answer to several prototype defects at once: there was no transition
table (any module could set any string), no audit history (E3), and nothing was transactional,
so a crash could leave a lead in a state nobody would ever pick up again (B9).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Event, Lead
from app.domain.state import LeadState, assert_allowed

Actor = Literal["user", "system", "llm"]


class LeadNotFound(Exception):
    def __init__(self, lead_id: uuid.UUID) -> None:
        self.lead_id = lead_id
        super().__init__(f"lead {lead_id} not found in this org")


async def lock_lead(session: AsyncSession, org_id: uuid.UUID, lead_id: uuid.UUID) -> Lead:
    """Fetch a lead `FOR UPDATE`, scoped to its org.

    The lock is what makes concurrent transitions safe: two workers reaching the same lead
    serialise here instead of both reading the old state and both acting on it.
    """
    result = await session.execute(
        select(Lead).where(Lead.id == lead_id, Lead.org_id == org_id).with_for_update()
    )
    lead = result.scalar_one_or_none()
    if lead is None:
        raise LeadNotFound(lead_id)
    return lead


async def record_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    type: str,
    actor: Actor = "system",
    actor_id: str | None = None,
    campaign_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    data: dict[str, Any] | None = None,
) -> Event:
    """Append an audit event. Never updated or deleted — a trigger enforces that."""
    event = Event(
        org_id=org_id,
        campaign_id=campaign_id,
        lead_id=lead_id,
        actor=actor,
        actor_id=actor_id,
        type=type,
        from_state=from_state,
        to_state=to_state,
        data=data or {},
    )
    session.add(event)
    return event


async def transition(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead_id: uuid.UUID,
    to_state: LeadState,
    actor: Actor = "system",
    actor_id: str | None = None,
    data: dict[str, Any] | None = None,
    next_action_at: datetime | None = None,
    clear_next_action: bool = False,
    flags_add: list[str] | None = None,
    flags_remove: list[str] | None = None,
    now: datetime | None = None,
) -> Lead:
    """Move a lead to `to_state`, or raise.

    Raises `IllegalTransition` if the machine forbids the move, and `LeadNotFound` if the lead
    does not exist in this org. The caller's transaction must still be committed; this function
    deliberately does not commit, so a transition can be combined with other work — cancelling
    follow-up jobs when a reply arrives, for instance — and either all of it lands or none does.
    """
    lead = await lock_lead(session, org_id, lead_id)
    from_state = LeadState(lead.state)

    assert_allowed(lead_id, from_state, to_state)

    lead.state = to_state.value
    lead.state_changed_at = now or datetime.now(tz=UTC)

    if clear_next_action:
        lead.next_action_at = None
    elif next_action_at is not None:
        lead.next_action_at = next_action_at

    if flags_add or flags_remove:
        flags = list(lead.flags or [])
        for flag in flags_add or []:
            if flag not in flags:
                flags.append(flag)
        for flag in flags_remove or []:
            if flag in flags:
                flags.remove(flag)
        lead.flags = flags

    await record_event(
        session,
        org_id=org_id,
        campaign_id=lead.campaign_id,
        lead_id=lead_id,
        actor=actor,
        actor_id=actor_id,
        type="lead.state_changed",
        from_state=from_state.value,
        to_state=to_state.value,
        data=data or {},
    )

    await session.flush()
    return lead
