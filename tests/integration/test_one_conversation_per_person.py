"""One conversation per person per org, however many campaigns hold them.

`UNIQUE(campaign_id, email_norm)` is per campaign on purpose: the same person may legitimately
appear in a later campaign, and that was the deliberate fix for the prototype's *global* unique
key, which made re-importing a corrected sheet silently do nothing (issue B16).

The cost of that choice is this file. Two campaigns can hold the same person, and if both run,
both send -- the recipient gets two cold emails from the same agency. Worse, if they reply to
one, the other campaign sends anyway, because it is looking at its own lead row and that row
knows nothing about the conversation happening beside it.

The guard is therefore **per person per org** rather than per lead, and it runs in `claim_send`
beside the suppression check -- the one place every send already passes through.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import outbox
from app.db.models import Campaign, Draft, Lead, Mailbox, Message
from app.domain.state import LeadState

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
def person() -> str:
    """One shared address per test.

    The fixtures here are function-scoped but their rows outlive a test, so a module-wide
    constant would collide on `uq_leads_campaign_email` and the failure would be about test
    isolation rather than about the rule under test."""
    return f"shared-{uuid.uuid4().hex[:8]}@example.com"


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org):
    row = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture(loop_scope="session")
async def two_campaigns(db, org, campaign, mailbox):
    """The fixture campaign, plus a second one on the same mailbox."""
    campaign.mailbox_id = mailbox.id
    campaign.status = "running"
    second = Campaign(
        org_id=org.id,
        mailbox_id=mailbox.id,
        name="Second campaign",
        status="running",
        persona=dict(campaign.persona or {}),
        settings={},
    )
    db.add(second)
    await db.commit()
    return campaign, second


async def person_in(db, org, campaign_id, address, *, state=LeadState.APPROVED.value):
    lead = Lead(
        org_id=org.id,
        campaign_id=campaign_id,
        state=state,
        email_norm=address,
        email_raw=address,
        full_name="Shared Person",
        company_name="Shared Co",
    )
    db.add(lead)
    await db.flush()
    await db.commit()
    return lead


async def emailed(db, org, lead, mailbox, *, when):
    db.add(
        Message(
            org_id=org.id,
            lead_id=lead.id,
            mailbox_id=mailbox.id,
            direction="out",
            status="sent",
            kind="initial",
            subject="Your pipeline",
            body_text="Hello",
            message_id_hdr=f"<{uuid.uuid4()}@example.com>",
            idempotency_key=str(uuid.uuid4()),
            sent_at=when,
        )
    )
    await db.commit()


async def claim(db, org, lead, mailbox, *, kind="initial"):
    # A real draft row: `messages.draft_id` is a foreign key, and a random uuid fails the
    # constraint rather than the rule under test.
    draft = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind=kind,
        version=1,
        subject="Your pipeline",
        body="Hello",
        status="approved",
    )
    db.add(draft)
    await db.flush()
    return await outbox.claim_send(
        db,
        org_id=org.id,
        lead_id=lead.id,
        kind=kind,
        mailbox_id=mailbox.id,
        draft_id=draft.id,
        subject="Your pipeline",
        body_text="Hello",
        now=NOW,
    )


class TestASecondCampaignDoesNotWriteAgain:
    async def test_a_person_emailed_recently_elsewhere_is_not_approached_again(
        self, db, org, two_campaigns, mailbox, person
    ):
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.SENT.value)
        await emailed(db, org, theirs, mailbox, when=NOW - timedelta(days=5))
        mine = await person_in(db, org, second.id, person)

        result = await claim(db, org, mine, mailbox)

        assert not result.claimed
        assert "another campaign" in result.reason or "we emailed this person" in result.reason

    async def test_the_scenario_as_described(self, db, org, two_campaigns, mailbox, person):
        """Campaign A sends, A is paused, B runs with the same person. B must not send."""
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.SENT.value)
        await emailed(db, org, theirs, mailbox, when=NOW - timedelta(days=2))
        first.paused = True
        await db.commit()
        mine = await person_in(db, org, second.id, person)

        result = await claim(db, org, mine, mailbox)

        assert not result.claimed, "pausing the first campaign does not unsend its email"

    async def test_a_live_conversation_elsewhere_blocks_whatever_the_date_says(
        self, db, org, two_campaigns, mailbox, person
    ):
        """The reply-mid-switch case: answering a reply in one campaign while opening a pitch
        in another is the worst version of this."""
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.REPLIED.value)
        # No recent message at all -- only the conversation state.
        await emailed(db, org, theirs, mailbox, when=NOW - timedelta(days=400))
        mine = await person_in(db, org, second.id, person)

        result = await claim(db, org, mine, mailbox)

        assert not result.claimed
        assert "already in touch" in result.reason

    async def test_an_old_contact_no_longer_blocks(self, db, org, two_campaigns, mailbox, person):
        """A second pitch a year later reads as a fresh approach, not as the same agency
        forgetting it already wrote. This is a cool-down, not a suppression."""
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.NO_RESPONSE.value)
        await emailed(
            db,
            org,
            theirs,
            mailbox,
            when=NOW - timedelta(days=outbox.CONTACT_COOLDOWN_DAYS + 10),
        )
        mine = await person_in(db, org, second.id, person)

        result = await claim(db, org, mine, mailbox)

        assert result.claimed, result.reason


class TestWhatItMustNotBlock:
    async def test_a_lead_is_not_blocked_by_its_own_history(
        self, db, org, two_campaigns, mailbox, person
    ):
        """The check is about *other* leads. A lead's own earlier message is a thread, not a
        clash -- blocking on it would stop every follow-up in the system."""
        first, _second = two_campaigns
        mine = await person_in(db, org, first.id, person, state=LeadState.SENT.value)
        await emailed(db, org, mine, mailbox, when=NOW - timedelta(days=3))

        result = await claim(db, org, mine, mailbox, kind="followup")

        assert result.claimed, result.reason

    async def test_a_reply_is_never_blocked(self, db, org, two_campaigns, mailbox, person):
        """Only first emails are checked. A reply belongs to a thread already open with that
        person, and blocking it would break the conversation this exists to protect."""
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.SENT.value)
        await emailed(db, org, theirs, mailbox, when=NOW - timedelta(days=1))
        mine = await person_in(db, org, second.id, person, state=LeadState.REPLIED.value)

        result = await claim(db, org, mine, mailbox, kind="reply")

        assert result.claimed, result.reason

    async def test_a_different_person_is_unaffected(self, db, org, two_campaigns, mailbox, person):
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.SENT.value)
        await emailed(db, org, theirs, mailbox, when=NOW)
        other = Lead(
            org_id=org.id,
            campaign_id=second.id,
            state=LeadState.APPROVED.value,
            email_norm="someone.else@example.com",
            email_raw="someone.else@example.com",
            full_name="Someone Else",
        )
        db.add(other)
        await db.commit()

        result = await claim(db, org, other, mailbox)

        assert result.claimed, result.reason

    async def test_another_orgs_contact_is_not_our_business(
        self, db, org, two_campaigns, mailbox, stranger_org, person
    ):
        """Tenancy: a different org writing to this person tells us nothing."""
        _first, second = two_campaigns
        theirs = Lead(
            org_id=stranger_org.id,
            campaign_id=None,
            state=LeadState.SENT.value,
            email_norm=person,
            email_raw=person,
        )
        # A lead needs a campaign; use the stranger org's own.
        other_campaign = Campaign(
            org_id=stranger_org.id, name="Theirs", status="running", persona={}
        )
        db.add(other_campaign)
        await db.flush()
        theirs.campaign_id = other_campaign.id
        db.add(theirs)
        await db.commit()

        mine = await person_in(db, org, second.id, person)
        result = await claim(db, org, mine, mailbox)

        assert result.claimed, result.reason


@pytest_asyncio.fixture(loop_scope="session")
async def stranger_org(db):
    from app.db.models import Org

    other = Org(name="Rival Ltd")
    db.add(other)
    await db.commit()
    return other


class TestItIsVisibleRatherThanSilent:
    async def test_the_reason_says_which_campaign_and_when(
        self, db, org, two_campaigns, mailbox, person
    ):
        """A send that silently does not happen is the failure mode this project keeps
        producing. The reason reaches the send loop's report and the logs."""
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.NO_RESPONSE.value)
        await emailed(db, org, theirs, mailbox, when=NOW - timedelta(days=5))
        mine = await person_in(db, org, second.id, person)

        result = await claim(db, org, mine, mailbox)

        assert "2026-10-12" in result.reason
        assert str(outbox.CONTACT_COOLDOWN_DAYS) in result.reason

    async def test_nothing_is_written_when_the_claim_is_refused(
        self, db, org, two_campaigns, mailbox, person
    ):
        first, second = two_campaigns
        theirs = await person_in(db, org, first.id, person, state=LeadState.SENT.value)
        await emailed(db, org, theirs, mailbox, when=NOW)
        mine = await person_in(db, org, second.id, person)

        await claim(db, org, mine, mailbox)

        rows = (await db.execute(select(Message).where(Message.lead_id == mine.id))).scalars().all()
        assert rows == []
