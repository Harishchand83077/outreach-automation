"""`app.db.state` against a real Postgres.

What needs a database rather than a fake: the row lock that serialises two workers, the fact
that a state change and its audit event share one transaction, and the append-only trigger.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Event, Lead
from app.db.state import LeadNotFound, lock_lead, record_event, transition
from app.domain.state import IllegalTransition, LeadState

pytestmark = pytest.mark.asyncio(loop_scope="session")


class TestTransition:
    async def test_a_legal_move_updates_the_lead(self, db, org, lead_factory):
        lead = await lead_factory(state="NEW")
        await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.ENRICHING)
        await db.commit()

        await db.refresh(lead)
        assert lead.state == "ENRICHING"

    async def test_an_illegal_move_raises_and_changes_nothing(self, db, org, lead_factory):
        lead = await lead_factory(state="NEW")
        with pytest.raises(IllegalTransition):
            await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.SENT)
        await db.rollback()

        await db.refresh(lead)
        assert lead.state == "NEW", "an illegal transition must leave the lead untouched"

    async def test_an_unknown_lead_raises(self, db, org):
        import uuid

        with pytest.raises(LeadNotFound):
            await transition(db, org_id=org.id, lead_id=uuid.uuid4(), to_state=LeadState.ENRICHING)

    async def test_another_org_cannot_move_our_lead(self, db, org, lead_factory):
        """Tenancy is enforced in the query, not by trusting the caller (ADR-004)."""
        import uuid

        lead = await lead_factory(state="NEW")
        with pytest.raises(LeadNotFound):
            await transition(db, org_id=uuid.uuid4(), lead_id=lead.id, to_state=LeadState.ENRICHING)


class TestTheAuditEvent:
    async def test_an_event_is_written_in_the_same_transaction(self, db, org, lead_factory):
        lead = await lead_factory(state="NEW")
        await transition(
            db,
            org_id=org.id,
            lead_id=lead.id,
            to_state=LeadState.ENRICHING,
            actor="user",
            actor_id="reviewer@example.com",
            data={"why": "manual start"},
        )
        await db.commit()

        event = (await db.execute(select(Event).where(Event.lead_id == lead.id))).scalar_one()
        assert event.from_state == "NEW"
        assert event.to_state == "ENRICHING"
        assert event.actor == "user"
        assert event.actor_id == "reviewer@example.com"
        assert event.data == {"why": "manual start"}
        assert event.campaign_id == lead.campaign_id

    async def test_a_rolled_back_transition_leaves_no_event(self, db, org, lead_factory):
        """The state change and its audit record are atomic together, or there is no audit."""
        lead = await lead_factory(state="NEW")
        # Held separately: rollback expires the instance, so reading lead.id afterwards would
        # try to reload it.
        lead_id = lead.id
        await transition(db, org_id=org.id, lead_id=lead_id, to_state=LeadState.ENRICHING)
        await db.rollback()

        count = await db.scalar(
            select(func.count()).select_from(Event).where(Event.lead_id == lead_id)
        )
        assert count == 0

    async def test_the_whole_journey_is_reconstructable(self, db, org, lead_factory):
        """E3: the prototype's single mutable row could not answer 'what happened to X?'."""
        lead = await lead_factory(state="NEW")
        for to_state in (
            LeadState.ENRICHING,
            LeadState.RESEARCHED,
            LeadState.DRAFTED,
            LeadState.AWAITING_APPROVAL,
            LeadState.APPROVED,
        ):
            await transition(db, org_id=org.id, lead_id=lead.id, to_state=to_state)
        await db.commit()

        events = (
            (await db.execute(select(Event).where(Event.lead_id == lead.id).order_by(Event.id)))
            .scalars()
            .all()
        )
        assert [e.to_state for e in events] == [
            "ENRICHING",
            "RESEARCHED",
            "DRAFTED",
            "AWAITING_APPROVAL",
            "APPROVED",
        ]
        assert [e.from_state for e in events] == [
            "NEW",
            "ENRICHING",
            "RESEARCHED",
            "DRAFTED",
            "AWAITING_APPROVAL",
        ]


class TestEventsAreAppendOnly:
    """Enforced by a database trigger, so no application bug can rewrite history."""

    async def test_update_is_refused(self, db, org, lead_factory):
        lead = await lead_factory(state="NEW")
        await record_event(db, org_id=org.id, lead_id=lead.id, type="test.event")
        await db.commit()

        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text("UPDATE app.events SET type = 'tampered'"))
        await db.rollback()

    async def test_delete_is_refused_by_default(self, db, org, lead_factory):
        lead = await lead_factory(state="NEW")
        await record_event(db, org_id=org.id, lead_id=lead.id, type="test.event")
        await db.commit()

        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text("DELETE FROM app.events"))
        await db.rollback()

    async def test_delete_is_possible_for_lawful_erasure(self, db, org, lead_factory):
        """A data-subject request must be satisfiable; it just has to be deliberate."""
        lead = await lead_factory(state="NEW")
        await record_event(db, org_id=org.id, lead_id=lead.id, type="test.event")
        await db.commit()

        await db.execute(text("SET LOCAL app.allow_event_delete = 'on'"))
        await db.execute(text("DELETE FROM app.events WHERE lead_id = :i"), {"i": lead.id})
        await db.commit()

        count = await db.scalar(
            select(func.count()).select_from(Event).where(Event.lead_id == lead.id)
        )
        assert count == 0


class TestRowLocking:
    async def test_two_concurrent_transitions_serialise(self, engine_factory, org, lead_factory):
        """B9/E3: without a lock both workers read the old state and both act on it.

        Each transaction uses its own engine, so these are genuinely concurrent connections.
        The second must see the first's result and therefore fail the transition check.
        """
        lead = await lead_factory(state="NEW")

        maker_a = async_sessionmaker(engine_factory(), expire_on_commit=False)
        maker_b = async_sessionmaker(engine_factory(), expire_on_commit=False)

        started = asyncio.Event()

        async def first():
            async with maker_a() as session:
                await transition(
                    session, org_id=org.id, lead_id=lead.id, to_state=LeadState.ENRICHING
                )
                started.set()
                # Hold the lock briefly so the other transaction is forced to wait on it.
                await asyncio.sleep(0.3)
                await session.commit()

        async def second():
            await started.wait()
            async with maker_b() as session:
                # Blocks on the row lock, then sees ENRICHING — so NEW -> ENRICHING is illegal.
                with pytest.raises(IllegalTransition):
                    await transition(
                        session, org_id=org.id, lead_id=lead.id, to_state=LeadState.ENRICHING
                    )
                await session.rollback()

        await asyncio.gather(first(), second())

        async with maker_a() as session:
            state = await session.scalar(select(Lead.state).where(Lead.id == lead.id))
        assert state == "ENRICHING"

    async def test_lock_lead_scopes_by_org(self, db, org, lead_factory):
        import uuid

        lead = await lead_factory(state="NEW")
        assert (await lock_lead(db, org.id, lead.id)).id == lead.id
        with pytest.raises(LeadNotFound):
            await lock_lead(db, uuid.uuid4(), lead.id)


class TestLeadConstraints:
    async def test_the_same_address_cannot_be_imported_twice_into_one_campaign(
        self, db, org, campaign, lead_factory
    ):
        await lead_factory(email="dupe@example.com")
        with pytest.raises(IntegrityError):
            await lead_factory(email="dupe@example.com")
        await db.rollback()

    async def test_the_same_address_may_appear_in_another_campaign(self, db, org, campaign):
        """B16: the prototype made email globally unique, so a re-contact was impossible."""
        from app.db.models import Campaign, Lead

        other = Campaign(org_id=org.id, name="Second Campaign", status="draft")
        db.add(other)
        await db.commit()

        for campaign_id in (campaign.id, other.id):
            db.add(
                Lead(
                    org_id=org.id,
                    campaign_id=campaign_id,
                    email_norm="reuse@example.com",
                    email_raw="reuse@example.com",
                    state="NEW",
                )
            )
        await db.commit()

        count = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.email_norm == "reuse@example.com")
        )
        assert count == 2


class TestNextActionAndFlags:
    async def test_next_action_at_can_be_set_and_cleared(self, db, org, lead_factory):
        from datetime import UTC, datetime, timedelta

        lead = await lead_factory(state="SENT")
        due = datetime.now(tz=UTC) + timedelta(days=3)

        await transition(
            db,
            org_id=org.id,
            lead_id=lead.id,
            to_state=LeadState.FOLLOWUP_SENT,
            next_action_at=due,
        )
        await db.commit()
        await db.refresh(lead)
        assert lead.next_action_at is not None

        await transition(
            db,
            org_id=org.id,
            lead_id=lead.id,
            to_state=LeadState.REPLIED,
            clear_next_action=True,
        )
        await db.commit()
        await db.refresh(lead)
        assert lead.next_action_at is None, "a reply must cancel the pending follow-up time"

    async def test_flags_are_added_and_removed(self, db, org, lead_factory):
        lead = await lead_factory(state="RESEARCHED")
        await transition(
            db,
            org_id=org.id,
            lead_id=lead.id,
            to_state=LeadState.DRAFTED,
            flags_add=["thin_research"],
        )
        await db.commit()
        await db.refresh(lead)
        assert lead.flags == ["thin_research"]

        await transition(
            db,
            org_id=org.id,
            lead_id=lead.id,
            to_state=LeadState.AWAITING_APPROVAL,
            flags_remove=["thin_research"],
        )
        await db.commit()
        await db.refresh(lead)
        assert lead.flags == []
