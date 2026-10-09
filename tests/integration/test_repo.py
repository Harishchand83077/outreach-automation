"""Org-scoped data access.

The guarantee: a caller holding one org's `OrgContext` cannot see or touch another org's rows,
and cannot accidentally query without a scope because `org_id` is a required parameter rather
than a convention (ADR-004).

The prototype had no tenancy at all, and `GET /api/leads` returned every lead's full insights,
draft and reply to anyone who found the URL (issues C1, C7).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.db import repo
from app.db.models import Campaign, Lead, Org
from app.db.repo import MAX_PAGE, OrgContext
from app.domain.state import LeadState

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def other_org(db):
    """A second tenant, to prove isolation rather than assume it."""
    org = Org(name="Someone Else Ltd")
    db.add(org)
    await db.flush()
    campaign = Campaign(org_id=org.id, name="Their Campaign", status="running")
    db.add(campaign)
    await db.flush()
    lead = Lead(
        org_id=org.id,
        campaign_id=campaign.id,
        email_norm="theirs@example.com",
        email_raw="theirs@example.com",
        state="APPROVED",
    )
    db.add(lead)
    await db.commit()
    return {"org": org, "campaign": campaign, "lead": lead}


class TestOrgContext:
    def test_a_uuid_is_required(self):
        """A string that merely looks like a UUID would silently match nothing."""
        with pytest.raises(TypeError):
            OrgContext(org_id="not-a-uuid")  # type: ignore[arg-type]

    def test_it_carries_the_actor_for_the_audit_trail(self):
        ctx = OrgContext(org_id=uuid.uuid4(), actor_id="reviewer@example.com")
        assert ctx.actor_id == "reviewer@example.com"


class TestCrossOrgIsolation:
    async def test_another_orgs_lead_is_invisible(self, db, org, other_org):
        """Returns None rather than raising, so a guessed UUID is indistinguishable from one
        that does not exist — no existence oracle."""
        ctx = OrgContext(org_id=org.id)
        assert await repo.get_lead(ctx, db, other_org["lead"].id) is None

    async def test_our_own_lead_is_visible(self, db, org, lead_factory):
        lead = await lead_factory(state="APPROVED")
        ctx = OrgContext(org_id=org.id)
        found = await repo.get_lead(ctx, db, lead.id)
        assert found is not None
        assert found.id == lead.id

    async def test_listing_by_state_excludes_other_orgs(self, db, org, lead_factory, other_org):
        await lead_factory(state="APPROVED")
        ctx = OrgContext(org_id=org.id)
        found = await repo.leads_by_state(ctx, db, state=LeadState.APPROVED)
        assert [lead.org_id for lead in found] == [org.id]

    async def test_the_funnel_counts_only_our_leads(self, db, org, lead_factory, other_org):
        await lead_factory(state="APPROVED")
        await lead_factory(state="SENT")
        ctx = OrgContext(org_id=org.id)
        assert await repo.count_leads_by_state(ctx, db) == {"APPROVED": 1, "SENT": 1}

    async def test_suppressions_are_per_org(self, db, org, other_org):
        """One org suppressing an address must not silently suppress it for another."""
        ours = OrgContext(org_id=org.id)
        theirs = OrgContext(org_id=other_org["org"].id)

        await repo.add_suppression(ours, db, value="blocked@example.com", reason="opt_out")
        await db.commit()

        assert len(await repo.list_suppressions(ours, db)) == 1
        assert await repo.list_suppressions(theirs, db) == []


class TestSuppressions:
    async def test_adding_twice_is_a_no_op(self, db, org):
        """An opt-out can be detected twice — once by keyword, once by the classifier — so both
        paths have to be safe to run."""
        ctx = OrgContext(org_id=org.id)
        first = await repo.add_suppression(ctx, db, value="x@example.com", reason="opt_out")
        await db.commit()
        second = await repo.add_suppression(ctx, db, value="x@example.com", reason="manual")
        await db.commit()

        assert first.id == second.id
        assert second.reason == "opt_out", "the original reason is kept"
        assert len(await repo.list_suppressions(ctx, db)) == 1

    async def test_addresses_are_normalised(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await repo.add_suppression(ctx, db, value="  MiXeD@Example.COM ", reason="bounce")
        await db.commit()

        stored = await repo.list_suppressions(ctx, db)
        assert [s.value for s in stored] == ["mixed@example.com"]

    async def test_a_domain_can_be_suppressed(self, db, org):
        ctx = OrgContext(org_id=org.id)
        record = await repo.add_suppression(
            ctx, db, value="spam.example", reason="complaint", kind="domain"
        )
        await db.commit()
        assert record.kind == "domain"


class TestLeadsDue:
    async def test_a_lead_whose_time_has_come_is_returned(self, db, org, lead_factory):
        past = datetime.now(tz=UTC) - timedelta(minutes=1)
        await lead_factory(state="SENT", next_action_at=past)
        ctx = OrgContext(org_id=org.id)

        due = await repo.leads_due(ctx, db, states=[LeadState.SENT])
        assert len(due) == 1

    async def test_a_future_time_is_not_due(self, db, org, lead_factory):
        future = datetime.now(tz=UTC) + timedelta(days=3)
        await lead_factory(state="SENT", next_action_at=future)
        ctx = OrgContext(org_id=org.id)

        assert await repo.leads_due(ctx, db, states=[LeadState.SENT]) == []

    async def test_a_lead_with_no_scheduled_time_is_not_due(self, db, org, lead_factory):
        await lead_factory(state="SENT")
        ctx = OrgContext(org_id=org.id)
        assert await repo.leads_due(ctx, db, states=[LeadState.SENT]) == []

    async def test_a_paused_lead_is_skipped(self, db, org, lead_factory):
        past = datetime.now(tz=UTC) - timedelta(minutes=1)
        await lead_factory(state="SENT", next_action_at=past, paused=True)
        ctx = OrgContext(org_id=org.id)
        assert await repo.leads_due(ctx, db, states=[LeadState.SENT]) == []

    async def test_a_paused_campaign_holds_all_its_leads(self, db, org, campaign, lead_factory):
        """The per-campaign kill switch has to actually stop work, without losing position."""
        past = datetime.now(tz=UTC) - timedelta(minutes=1)
        lead = await lead_factory(state="SENT", next_action_at=past)
        campaign.paused = True
        await db.commit()

        ctx = OrgContext(org_id=org.id)
        assert await repo.leads_due(ctx, db, states=[LeadState.SENT]) == []

        # Resuming does not lose the lead: pausing is a flag, not a state.
        campaign.paused = False
        await db.commit()
        due = await repo.leads_due(ctx, db, states=[LeadState.SENT])
        assert [lead_.id for lead_ in due] == [lead.id]

    async def test_a_draft_campaign_is_not_worked(self, db, org, campaign, lead_factory):
        past = datetime.now(tz=UTC) - timedelta(minutes=1)
        await lead_factory(state="SENT", next_action_at=past)
        campaign.status = "draft"
        await db.commit()

        ctx = OrgContext(org_id=org.id)
        assert await repo.leads_due(ctx, db, states=[LeadState.SENT]) == []

    async def test_the_oldest_due_lead_comes_first(self, db, org, lead_factory):
        now = datetime.now(tz=UTC)
        later = await lead_factory(state="SENT", next_action_at=now - timedelta(minutes=1))
        earlier = await lead_factory(state="SENT", next_action_at=now - timedelta(hours=2))
        ctx = OrgContext(org_id=org.id)

        due = await repo.leads_due(ctx, db, states=[LeadState.SENT])
        assert [lead.id for lead in due] == [earlier.id, later.id]


class TestPagination:
    async def test_no_query_can_return_a_whole_table(self, db, org, lead_factory):
        """C7: the prototype returned every lead's full insights, draft and reply on every poll,
        and the dashboard polled five such endpoints every 2.5 seconds."""
        ctx = OrgContext(org_id=org.id)
        for _ in range(5):
            await lead_factory(state="APPROVED")

        assert len(await repo.leads_by_state(ctx, db, state=LeadState.APPROVED, limit=2)) == 2
        # An absurd limit is clamped rather than honoured.
        assert len(await repo.leads_by_state(ctx, db, state=LeadState.APPROVED, limit=10_000)) == 5

    async def test_the_page_size_is_capped(self, db, org):
        assert repo._page_size(10_000) == MAX_PAGE
        assert repo._page_size(None) == repo.DEFAULT_PAGE
        assert repo._page_size(0) == 1

    async def test_cursor_paging_walks_every_row_once(self, db, org, lead_factory):
        ctx = OrgContext(org_id=org.id)
        for _ in range(5):
            await lead_factory(state="APPROVED")

        seen: list[uuid.UUID] = []
        cursor: uuid.UUID | None = None
        while True:
            page = await repo.leads_by_state(
                ctx, db, state=LeadState.APPROVED, limit=2, after_id=cursor
            )
            if not page:
                break
            seen.extend(lead.id for lead in page)
            cursor = page[-1].id

        assert len(seen) == 5
        assert len(set(seen)) == 5, "cursor paging must not repeat or skip rows"
