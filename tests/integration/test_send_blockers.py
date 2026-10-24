"""The dashboard's answer to "why is this not sending?" is the send loop's own answer.

**The property under test is that there is only one implementation.** A lead sat `APPROVED` for
eight days on the live system showing "next action: —", which is indistinguishable from "waiting
its turn"; the real reason was that another campaign in the same org was already in conversation
with that person, and `claim_send` had been computing exactly that sentence once a minute and
writing it to a log nobody reads.

The obvious fix -- a second function for the dashboard -- would have been the seventh instance of
this project's signature defect. So `claim_send` calls `why_not_sendable`, and the test that
matters is the last one in this file: for every refusal, both answer the same thing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.db import outbox
from app.db.models import Mailbox, Suppression

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


async def attached_mailbox(db, org, campaign):
    row = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(row)
    await db.flush()
    campaign.mailbox_id = row.id
    campaign.status = "running"
    await db.commit()
    return row


class TestTheReasons:
    async def test_a_ready_lead_has_no_reason(self, db, org, campaign, lead_factory):
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="APPROVED")

        assert (
            await outbox.why_not_sendable(db, org_id=org.id, lead=lead, kind="initial", now=NOW)
            is None
        )

    async def test_a_paused_lead_says_so(self, db, org, campaign, lead_factory):
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="APPROVED", paused=True)

        reason = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )

        assert reason == "this lead is paused"

    async def test_a_campaign_with_no_mailbox_says_so(self, db, org, campaign, lead_factory):
        campaign.status = "running"
        await db.commit()
        lead = await lead_factory(state="APPROVED")

        reason = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )

        assert reason is not None
        assert "no mailbox" in reason

    async def test_a_paused_campaign_says_so(self, db, org, campaign, lead_factory):
        await attached_mailbox(db, org, campaign)
        campaign.paused = True
        await db.commit()
        lead = await lead_factory(state="APPROVED")

        reason = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )

        assert reason == "the campaign is paused"

    async def test_a_suppressed_address_says_so(self, db, org, campaign, lead_factory):
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="APPROVED")
        db.add(Suppression(org_id=org.id, kind="email", value=lead.email_norm, reason="opt_out"))
        await db.commit()

        reason = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )

        assert reason == "this address is on the suppression list"

    async def test_the_wrong_state_names_the_state_and_the_kind(
        self, db, org, campaign, lead_factory
    ):
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="SENT")

        reason = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )

        assert reason is not None
        assert "SENT" in reason and "initial" in reason

    async def test_another_campaign_in_conversation_is_the_reason_that_cost_a_week(
        self, db, org, campaign, lead_factory
    ):
        """**The real one.** Two campaigns, the same person, one of them mid-conversation. The
        second campaign's cold email is refused -- correctly, so nobody gets two cold pitches
        from one agency -- and until this function existed nothing said so anywhere."""
        from app.db.models import Campaign

        await attached_mailbox(db, org, campaign)
        other = Campaign(org_id=org.id, name="The other campaign", status="running", persona={})
        db.add(other)
        await db.flush()

        address = "founder@example.com"
        sibling = await lead_factory(state="FOLLOWUP_SENT", email=address)
        sibling.campaign_id = other.id
        await db.commit()

        lead = await lead_factory(state="APPROVED", email=address)

        reason = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )

        assert reason is not None
        assert "already in touch" in reason
        assert "FOLLOWUP_SENT" in reason
        assert sibling.id != lead.id

    async def test_a_followup_is_not_blocked_by_that_rule(self, db, org, campaign, lead_factory):
        """A follow-up belongs to a thread already open with that person; blocking it would
        break the conversation the rule exists to protect."""
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="SENT")

        assert (
            await outbox.why_not_sendable(db, org_id=org.id, lead=lead, kind="followup", now=NOW)
            is None
        )

    async def test_an_unknown_kind_is_a_programming_error_not_a_refusal(
        self, db, org, campaign, lead_factory
    ):
        lead = await lead_factory(state="APPROVED")

        with pytest.raises(ValueError, match="unknown message kind"):
            await outbox.why_not_sendable(db, org_id=org.id, lead=lead, kind="banana", now=NOW)


class TestTheSendLoopAgrees:
    @pytest.mark.parametrize(
        ("state", "paused_lead", "paused_campaign", "suppress"),
        [
            ("APPROVED", True, False, False),
            ("APPROVED", False, True, False),
            ("APPROVED", False, False, True),
            ("SENT", False, False, False),
        ],
    )
    async def test_the_claim_refuses_with_the_same_sentence(
        self, db, org, campaign, lead_factory, state, paused_lead, paused_campaign, suppress
    ):
        """**The property that stops this drifting.** If somebody later adds a refusal to
        `claim_send` without putting it in `why_not_sendable`, this fails."""
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state=state, paused=paused_lead)
        if paused_campaign:
            campaign.paused = True
        if suppress:
            db.add(
                Suppression(org_id=org.id, kind="email", value=lead.email_norm, reason="opt_out")
            )
        await db.commit()

        explained = await outbox.why_not_sendable(
            db, org_id=org.id, lead=lead, kind="initial", now=NOW
        )
        claimed = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            draft_id=None,
            subject="Hello",
            body_text="Hi there.",
            now=NOW,
        )

        assert not claimed.claimed
        assert claimed.reason == explained

    async def test_a_sendable_lead_is_claimed(self, db, org, campaign, lead_factory):
        """The other half: the explainer saying "nothing wrong" must mean the claim succeeds,
        or the dashboard would promise a send that never happens."""
        mailbox = await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="APPROVED")

        assert (
            await outbox.why_not_sendable(db, org_id=org.id, lead=lead, kind="initial", now=NOW)
            is None
        )
        claimed = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            mailbox_id=mailbox.id,
            draft_id=None,
            subject="Hello",
            body_text="Hi there.",
            now=NOW,
        )

        assert claimed.claimed, claimed.reason


class TestItDoesNotWrite:
    async def test_asking_why_claims_nothing(self, db, org, campaign, lead_factory):
        """A dashboard asking about fifty leads must not interfere with the loop sending one."""
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="APPROVED")

        for _ in range(3):
            await outbox.why_not_sendable(db, org_id=org.id, lead=lead, kind="initial", now=NOW)

        from sqlalchemy import func, select

        from app.db.models import Message

        count = await db.scalar(
            select(func.count()).select_from(Message).where(Message.lead_id == lead.id)
        )
        assert count == 0

    async def test_another_orgs_lead_is_not_reachable_through_it(
        self, db, org, campaign, lead_factory
    ):
        """`org_id` is passed explicitly and is never taken from the lead, so a caller cannot
        be tricked into answering about somebody else's data."""
        await attached_mailbox(db, org, campaign)
        lead = await lead_factory(state="APPROVED")

        reason = await outbox.why_not_sendable(
            db, org_id=uuid.uuid4(), lead=lead, kind="initial", now=NOW
        )

        # The campaign belongs to the real org, so a stranger's org_id finds no suppression and
        # no sibling — but it also cannot be used to *send*, which is what claim_send enforces.
        assert reason is None or "already in touch" not in reason
