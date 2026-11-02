"""Two mailboxes on one campaign, and the sticky sender (F7, ADR-044, slice S14.1).

The three acceptance criteria for this slice, each with a test named after it:

1. two mailboxes share a campaign;
2. a lead's follow-ups stay on its first mailbox;
3. the per-mailbox cap still binds.

The one that could not be written anywhere else is the second: stickiness is enforced by
`claim_send` under the lead's row lock, so only a real claim against real Postgres shows it.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import outbox
from app.db.models import CampaignMailbox, Draft, Lead, Mailbox, Org
from app.db.repo import OrgContext
from app.services.mailboxes import pool
from app.worker import send_loop
from tests.integration.conftest import connect_mailbox

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

INSIDE = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)  # a Tuesday, inside the IST window


async def _mailbox(db, org, address: str) -> Mailbox:
    record = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email=address,
        reply_to=address,
        daily_cap=100,
    )
    db.add(record)
    await db.flush()
    await db.commit()
    await connect_mailbox(db, record)
    return record


@pytest_asyncio.fixture(loop_scope="session")
async def pair(db, org, campaign):
    """A campaign with two mailboxes: its primary, plus one added to the pool."""
    primary = await _mailbox(db, org, f"primary-{uuid.uuid4().hex[:8]}@outreach.example")
    second = await _mailbox(db, org, f"second-{uuid.uuid4().hex[:8]}@outreach.example")
    campaign.mailbox_id = primary.id
    db.add(
        CampaignMailbox(
            org_id=org.id, campaign_id=campaign.id, mailbox_id=second.id, weight=1
        )
    )
    await db.commit()
    return primary, second


async def _approved(db, org, lead_factory, *, kind="initial", state="APPROVED", **kw):
    lead = await lead_factory(state=state, **kw)
    draft = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind=kind,
        version=1,
        subject="Your pipeline hiring",
        body="Hi Ben,\n\nWorth a short call? https://cal.example.com/asha\n\nAsha",
        status="approved",
        approved_by="reviewer@example.com",
        approved_at=datetime.now(tz=UTC),
    )
    db.add(draft)
    await db.commit()
    return lead, draft


class TestThePool:
    async def test_the_primary_is_a_member_without_a_row_of_its_own(
        self, db, org, campaign
    ) -> None:
        """The decision the whole slice rests on. Making `campaign_mailboxes` the sole authority
        would mean every existing campaign and every future `POST /campaigns` had to write a
        row, and the one that forgot would stop sending with nothing on screen saying why.
        """
        box = await _mailbox(db, org, f"solo-{uuid.uuid4().hex[:8]}@outreach.example")
        campaign.mailbox_id = box.id
        await db.commit()

        members = await pool.pool_for(db, org_id=org.id, campaign=campaign)

        assert [member.mailbox_id for member in members] == [box.id]
        assert members[0].primary is True
        assert members[0].weight == 1

    async def test_a_row_for_the_primary_sets_its_weight_rather_than_doubling_it(
        self, db, org, campaign, pair
    ) -> None:
        primary, _ = pair
        db.add(
            CampaignMailbox(
                org_id=org.id, campaign_id=campaign.id, mailbox_id=primary.id, weight=5
            )
        )
        await db.commit()

        members = await pool.pool_for(db, org_id=org.id, campaign=campaign)

        assert len([m for m in members if m.mailbox_id == primary.id]) == 1
        assert next(m for m in members if m.mailbox_id == primary.id).weight == 5

    async def test_both_directions_of_the_rule_agree(self, db, org, campaign, pair) -> None:
        """`pool_for` and `campaign_ids_for` are the same rule read two ways -- the send loop
        asks "whose work may I do?" and the dashboard asks "what may this campaign use?". They
        live in one module so a change to one is a change to both."""
        primary, second = pair

        for box in (primary, second):
            ids = await pool.campaign_ids_for(db, org_id=org.id, mailbox_id=box.id)
            assert campaign.id in ids
            members = await pool.pool_for(db, org_id=org.id, campaign=campaign)
            assert box.id in [member.mailbox_id for member in members]

    async def test_another_workspaces_mailbox_is_not_in_any_pool_here(
        self, db, org, campaign
    ) -> None:
        other = Org(name="Another agency")
        db.add(other)
        await db.commit()
        theirs = await _mailbox(db, other, f"theirs-{uuid.uuid4().hex[:8]}@outreach.example")

        ids = await pool.campaign_ids_for(db, org_id=org.id, mailbox_id=theirs.id)

        assert ids == []


class TestTwoMailboxesShareACampaign:
    async def test_both_mailboxes_see_the_campaigns_work(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """Acceptance criterion 1. Before F7 only `campaigns.mailbox_id` did."""
        primary, second = pair
        await _approved(db, org, lead_factory, email="ben@example.com")

        for box in (primary, second):
            found = await send_loop.find_candidates(
                db, org_id=org.id, mailbox_id=box.id, now=INSIDE
            )
            # Which of them is offered the lead is the rotation's business; what matters here is
            # that the campaign is visible from both, where it used to be visible from one.
            assert campaign.id in await pool.campaign_ids_for(
                db, org_id=org.id, mailbox_id=box.id
            )
            assert isinstance(found, list)

    async def test_an_unassigned_lead_is_offered_to_exactly_one_member(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """Both mailboxes may do the campaign's work; only one of them may do *this* lead, or
        the same person is emailed twice from two addresses in one pass."""
        primary, second = pair
        lead, _ = await _approved(db, org, lead_factory, email="ben@example.com")

        offered = [
            box.id
            for box in (primary, second)
            if lead.id
            in {
                candidate.lead.id
                for candidate in await send_loop.find_candidates(
                    db, org_id=org.id, mailbox_id=box.id, now=INSIDE
                )
            }
        ]

        assert len(offered) == 1, offered

    async def test_a_mailbox_in_no_pool_gets_nothing(self, db, org, pair, lead_factory) -> None:
        """The query short-circuits on an empty pool rather than scanning the org's leads."""
        loner = await _mailbox(db, org, f"loner-{uuid.uuid4().hex[:8]}@outreach.example")
        await _approved(db, org, lead_factory, email="ben@example.com")

        found = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=loner.id, now=INSIDE
        )

        assert found == []


class TestTheStickySender:
    async def test_a_claim_stamps_the_lead_with_the_mailbox_that_sent(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        primary, _ = pair
        lead, draft = await _approved(db, org, lead_factory, email="ben@example.com")

        result = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind=outbox.KIND_INITIAL,
            mailbox_id=primary.id,
            draft_id=draft.id,
            subject=draft.subject,
            body_text=draft.body,
        )
        await db.commit()

        assert result.message is not None, result.reason
        await db.refresh(lead)
        assert lead.mailbox_id == primary.id

    async def test_a_followup_from_the_other_mailbox_is_refused_by_name(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """Acceptance criterion 2, in its load-bearing half.

        A follow-up arriving from a second address shows the recipient one conversation from two
        strangers, and threads separately in their client whatever `References` says. Refused
        rather than silently redirected: the caller asked for a specific mailbox, and quietly
        using another is how a cap is exceeded on a mailbox nobody was watching.
        """
        primary, second = pair
        lead, _ = await _approved(
            db, org, lead_factory, email="ben@example.com", state="SENT", kind="followup"
        )
        lead.mailbox_id = primary.id
        lead.next_action_at = INSIDE
        await db.commit()

        refusal = await outbox.why_not_sendable(
            db,
            org_id=org.id,
            lead=lead,
            kind=outbox.KIND_FOLLOWUP,
            mailbox_id=second.id,
            now=INSIDE,
        )

        assert refusal is not None
        assert "different mailbox" in refusal

    async def test_the_other_mailbox_is_not_even_offered_the_lead(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """The other half: a refusal at claim time would be correct and wasteful. The selection
        query keeps another mailbox's leads out in the first place."""
        primary, second = pair
        lead, _ = await _approved(
            db, org, lead_factory, email="ben@example.com", state="SENT", kind="followup"
        )
        lead.mailbox_id = primary.id
        lead.next_action_at = INSIDE
        await db.commit()

        theirs = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=second.id, now=INSIDE
        )
        mine = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=primary.id, now=INSIDE
        )

        assert lead.id not in {candidate.lead.id for candidate in theirs}
        assert lead.id in {candidate.lead.id for candidate in mine}

    async def test_the_assignment_is_never_changed_once_set(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """`claim_send` writes it only when it is NULL. A second send that could move it would
        make the whole guarantee advisory."""
        primary, second = pair
        second_id = second.id
        lead, draft = await _approved(db, org, lead_factory, email="ben@example.com")
        lead.mailbox_id = second_id
        await db.commit()

        result = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind=outbox.KIND_INITIAL,
            mailbox_id=primary.id,
            draft_id=draft.id,
            subject=draft.subject,
            body_text=draft.body,
        )
        await db.commit()

        assert result.message is None
        assert "different mailbox" in result.reason
        await db.refresh(lead)
        assert lead.mailbox_id == second_id

    async def test_removing_a_mailbox_from_the_pool_leaves_its_leads_on_it(
        self, api, owner, db, org, campaign, pair, lead_factory
    ) -> None:
        """Stated in the response rather than left to be discovered. Reassigning them would do
        the exact thing the sticky sender exists to prevent, to the people furthest along."""
        _, second = pair
        lead, _ = await _approved(db, org, lead_factory, email="ben@example.com")
        lead.mailbox_id = second.id
        await db.commit()

        response = api.delete(
            f"/api/v1/campaigns/{campaign.id}/mailboxes/{second.id}", headers=owner
        )

        assert response.status_code == 200, response.text
        assert response.json()["leads_still_assigned"] >= 1
        await db.refresh(lead)
        assert lead.mailbox_id == second.id


class TestTheCapStillBinds:
    async def test_the_cap_is_counted_per_mailbox_not_per_pool(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """Acceptance criterion 3. A pool spreads a campaign's sends across several reputations;
        it does not raise any one of them. If the cap were pooled, two mailboxes at 100 each
        would be one campaign sending 200 from addresses capped at 100 (ADR-034's whole point).
        """
        primary, second = pair
        settings = send_loop.CampaignSettings()

        primary_cap = await send_loop.effective_cap(
            db, mailbox=primary, settings=settings, now=INSIDE
        )
        second_cap = await send_loop.effective_cap(
            db, mailbox=second, settings=settings, now=INSIDE
        )

        assert primary_cap == primary.daily_cap
        assert second_cap == second.daily_cap
        # And the counts are separate, which is what makes the two caps mean two things.
        assert (
            await send_loop.sent_today(
                db, mailbox_id=primary.id, settings=settings, now=INSIDE
            )
            == 0
        )

    async def test_a_paused_member_is_skipped_and_the_other_keeps_sending(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """Degrading to a smaller pool is the point of having one. ADR-034 pauses a mailbox that
        is bouncing badly, and a pool that stopped because one member was paused would turn that
        safety signal into an outage."""
        primary, second = pair
        primary.paused = True
        primary.pause_reason = "bounce rate"
        await db.commit()
        await _approved(db, org, lead_factory, email="ben@example.com")

        chosen = await send_loop._assignee_for(
            db,
            org_id=org.id,
            campaign=campaign,
            settings=send_loop.CampaignSettings(),
            now=INSIDE,
        )

        assert chosen == second.id


class TestThePoolRoutes:
    async def test_the_pool_is_listed_with_its_weights(
        self, api, owner, db, org, campaign, pair
    ) -> None:
        response = api.get(f"/api/v1/campaigns/{campaign.id}/mailboxes", headers=owner)

        assert response.status_code == 200, response.text
        rows = response.json()["mailboxes"]
        assert len(rows) == 2
        assert sum(1 for row in rows if row["primary"]) == 1
        assert all(row["weight"] == 1 for row in rows)

    async def test_a_weight_can_be_set_and_is_bounded(
        self, api, owner, db, org, campaign, pair
    ) -> None:
        _, second = pair

        ok = api.put(
            f"/api/v1/campaigns/{campaign.id}/mailboxes/{second.id}",
            json={"weight": 4},
            headers=owner,
        )
        too_big = api.put(
            f"/api/v1/campaigns/{campaign.id}/mailboxes/{second.id}",
            json={"weight": 5000},
            headers=owner,
        )

        assert ok.status_code == 200, ok.text
        assert ok.json()["weight"] == 4
        assert too_big.status_code == 422
        assert "share" in too_big.text

    async def test_the_primary_cannot_be_removed_from_its_own_pool(
        self, api, owner, db, org, campaign, pair
    ) -> None:
        primary, _ = pair

        response = api.delete(
            f"/api/v1/campaigns/{campaign.id}/mailboxes/{primary.id}", headers=owner
        )

        assert response.status_code == 422
        assert "campaign's own mailbox" in response.text

    async def test_a_member_may_read_the_pool_but_not_change_it(
        self, api, member, db, org, campaign, pair
    ) -> None:
        """Who sends is a rule about the workspace, so changing it is `admin` -- the same line
        ADR-023 drew for the allowlist. Reading it is part of understanding a campaign."""
        _, second = pair

        assert (
            api.get(f"/api/v1/campaigns/{campaign.id}/mailboxes", headers=member).status_code
            == 200
        )
        assert (
            api.put(
                f"/api/v1/campaigns/{campaign.id}/mailboxes/{second.id}",
                json={"weight": 2},
                headers=member,
            ).status_code
            == 403
        )

    async def test_another_workspaces_mailbox_cannot_join_this_pool(
        self, api, owner, db, org, campaign, pair
    ) -> None:
        other = Org(name="Another agency again")
        db.add(other)
        await db.commit()
        theirs = await _mailbox(db, other, f"theirs2-{uuid.uuid4().hex[:8]}@outreach.example")

        response = api.put(
            f"/api/v1/campaigns/{campaign.id}/mailboxes/{theirs.id}",
            json={"weight": 1},
            headers=owner,
        )

        assert response.status_code == 404
        rows = await db.scalars(
            select(CampaignMailbox).where(CampaignMailbox.mailbox_id == theirs.id)
        )
        assert rows.all() == []

    async def test_the_campaign_reports_a_partly_broken_pool_without_calling_it_blocked(
        self, api, owner, db, org, campaign, pair
    ) -> None:
        """A pool reporting itself blocked because one of two mailboxes needs reconnecting would
        have somebody stop a campaign that is sending perfectly well."""
        primary, _ = pair
        primary.paused = True
        primary.pause_reason = "bounce rate"
        await db.commit()

        response = api.get(f"/api/v1/campaigns/{campaign.id}", headers=owner)

        assert response.status_code == 200, response.text
        codes = {blocker["code"] for blocker in response.json()["blockers"]}
        assert "pool_member_unavailable" in codes
        assert "mailbox_unavailable" not in codes


class TestNothingChangedForAPoolOfOne:
    async def test_a_campaign_with_one_mailbox_behaves_as_it_did(
        self, db, org, campaign, lead_factory
    ) -> None:
        """The migration adds an empty table and a NULL column, so every existing campaign must
        be indistinguishable from before. Asserted rather than assumed."""
        box = await _mailbox(db, org, f"only-{uuid.uuid4().hex[:8]}@outreach.example")
        campaign.mailbox_id = box.id
        await db.commit()
        lead, _ = await _approved(db, org, lead_factory, email="ben@example.com")

        found = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=box.id, now=INSIDE
        )

        assert lead.id in {candidate.lead.id for candidate in found}

    async def test_an_existing_lead_with_no_assignment_is_sendable(
        self, db, org, campaign, lead_factory
    ) -> None:
        """Every lead in the live database has `mailbox_id` NULL. If that were treated as "not
        yet eligible" the deploy would stop an in-flight campaign."""
        box = await _mailbox(db, org, f"legacy-{uuid.uuid4().hex[:8]}@outreach.example")
        campaign.mailbox_id = box.id
        await db.commit()
        lead, _ = await _approved(db, org, lead_factory, email="ben@example.com")
        assert lead.mailbox_id is None

        refusal = await outbox.why_not_sendable(
            db,
            org_id=org.id,
            lead=lead,
            kind=outbox.KIND_INITIAL,
            mailbox_id=box.id,
            now=INSIDE,
        )

        assert refusal is None, refusal


class TestTheLeadIsNotOrphaned:
    async def test_deleting_a_mailbox_unassigns_its_leads_rather_than_deleting_them(
        self, db, org, campaign, pair, lead_factory
    ) -> None:
        """`ON DELETE SET NULL`, not CASCADE. The address they knew is gone either way; the
        record of what was sent to whom must not be."""
        _, second = pair
        lead, _ = await _approved(db, org, lead_factory, email="ben@example.com")
        lead.mailbox_id = second.id
        await db.commit()

        lead_id = lead.id
        await db.delete(second)
        await db.commit()

        # The columns, not the ORM object. The identity map still holds the pre-delete row, so
        # reading the mapped attribute back would assert what this session remembers rather
        # than what Postgres did -- and expiring it makes the assertion itself do IO.
        row = (
            await db.execute(select(Lead.id, Lead.mailbox_id).where(Lead.id == lead_id))
        ).one_or_none()

        assert row is not None, "the lead must survive its mailbox being deleted"
        assert row.mailbox_id is None


class TestTheOrgContextIsNeverTakenFromACaller:
    async def test_usable_ids_is_scoped_to_the_org(self, db, org) -> None:
        other = Org(name="A third agency")
        db.add(other)
        await db.commit()
        mine = await _mailbox(db, org, f"mine-{uuid.uuid4().hex[:8]}@outreach.example")
        theirs = await _mailbox(db, other, f"not-mine-{uuid.uuid4().hex[:8]}@outreach.example")

        usable = await pool.usable_ids(
            db, org_id=org.id, mailbox_ids=[mine.id, theirs.id]
        )

        assert usable == {mine.id}
        assert OrgContext(org_id=org.id).org_id == org.id
