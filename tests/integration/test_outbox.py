"""The outbox against a real Postgres.

The guarantee under test: **a crash can never produce a duplicate cold email.** The unique
`idempotency_key` is a database constraint, and the race it prevents needs two real concurrent
transactions to demonstrate, so this cannot be a unit test.

The prototype sent over SMTP first and wrote the database afterwards, so a crash in between
resent on the next run (issue B12).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import outbox, repo
from app.db.models import Mailbox, Message
from app.db.outbox import KIND_FOLLOWUP, KIND_INITIAL, KIND_REPLY

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org, campaign):
    record = Mailbox(org_id=org.id, from_name="Asha at Acme", from_email="asha@example.com")
    db.add(record)
    await db.flush()
    campaign.mailbox_id = record.id
    await db.commit()
    return record


class TestMessageIdGeneration:
    async def test_the_domain_comes_from_the_sending_address(self):
        message_id = outbox.build_message_id("asha@example.com")
        assert message_id.startswith("<")
        assert message_id.endswith("@example.com>")

    async def test_each_one_is_unique(self):
        ids = {outbox.build_message_id("a@example.com") for _ in range(100)}
        assert len(ids) == 100


class TestClaim:
    async def test_an_approved_lead_can_be_claimed(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED")
        result = await outbox.claim_send(
            db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL, subject="Hi"
        )
        await db.commit()

        assert result.claimed
        assert result.message is not None
        assert result.message.status == "sending"
        assert result.message.direction == "out"
        assert result.message.to_addr == lead.email_norm
        assert result.message.from_addr == "asha@example.com"
        assert result.message.message_id_hdr is not None
        assert result.message.idempotency_key == f"{lead.id}:initial:1"

    async def test_the_lead_is_not_moved_at_claim_time(self, db, org, lead_factory, mailbox):
        """A lead is only ever SENT because something was actually sent."""
        lead = await lead_factory(state="APPROVED")
        await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()
        await db.refresh(lead)
        assert lead.state == "APPROVED"

    @pytest.mark.parametrize(
        "state,kind",
        [
            ("APPROVED", KIND_FOLLOWUP),
            ("SENT", KIND_INITIAL),
            ("REPLIED", KIND_INITIAL),
            ("NEW", KIND_INITIAL),
            ("AWAITING_APPROVAL", KIND_INITIAL),
            ("OPTED_OUT", KIND_INITIAL),
        ],
    )
    async def test_the_wrong_state_for_the_kind_is_refused(
        self, db, org, lead_factory, mailbox, state, kind
    ):
        lead = await lead_factory(state=state)
        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=kind)
        assert not result.claimed
        assert "not valid" in (result.reason or "")

    async def test_a_reply_that_arrived_while_queued_cancels_the_followup(
        self, db, org, lead_factory, mailbox
    ):
        """The real race this guards: the follow-up was queued while the lead was SENT, and a
        reply landed before the send loop got to it."""
        lead = await lead_factory(state="REPLIED")
        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_FOLLOWUP)
        assert not result.claimed

    async def test_a_suppressed_recipient_is_refused(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED")
        ctx = repo.OrgContext(org_id=org.id)
        await repo.add_suppression(ctx, db, value=lead.email_norm, reason="opt_out")
        await db.commit()

        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        assert not result.claimed
        assert result.reason == "recipient is suppressed"

    async def test_a_suppressed_domain_is_refused(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED", email="someone@blocked.example")
        ctx = repo.OrgContext(org_id=org.id)
        await repo.add_suppression(ctx, db, value="blocked.example", reason="bounce", kind="domain")
        await db.commit()

        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        assert not result.claimed
        assert result.reason == "recipient is suppressed"

    async def test_a_paused_lead_is_refused(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED", paused=True)
        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        assert not result.claimed
        assert result.reason == "lead is paused"

    async def test_a_paused_campaign_is_refused(self, db, org, campaign, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED")
        campaign.paused = True
        await db.commit()

        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        assert not result.claimed
        assert result.reason == "campaign is paused"

    async def test_a_paused_mailbox_is_refused(self, db, org, lead_factory, mailbox):
        """Auto-pause on repeated SMTP auth failure must actually stop sending."""
        lead = await lead_factory(state="APPROVED")
        mailbox.paused = True
        mailbox.pause_reason = "3 consecutive SMTP auth failures"
        await db.commit()

        result = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        assert not result.claimed
        assert "mailbox is paused" in (result.reason or "")

    async def test_an_unknown_kind_is_a_programming_error(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED")
        with pytest.raises(ValueError, match="unknown message kind"):
            await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind="newsletter")


class TestIdempotency:
    """The core guarantee of the module."""

    async def test_two_concurrent_claims_produce_exactly_one_message(
        self, engine_factory, org, campaign, lead_factory, db, mailbox
    ):
        """Two workers, two real transactions, one email. This is why the key is UNIQUE."""
        lead = await lead_factory(state="APPROVED")
        lead_id, org_id = lead.id, org.id

        maker_a = async_sessionmaker(engine_factory(), expire_on_commit=False)
        maker_b = async_sessionmaker(engine_factory(), expire_on_commit=False)

        async def attempt(maker) -> str | None:
            async with maker() as session:
                result = await outbox.claim_send(
                    session, org_id=org_id, lead_id=lead_id, kind=KIND_INITIAL
                )
                if result.claimed:
                    await session.commit()
                    return "claimed"
                await session.rollback()
                return result.reason

        outcomes = await asyncio.gather(attempt(maker_a), attempt(maker_b))

        assert outcomes.count("claimed") == 1, f"expected exactly one claim, got {outcomes}"

        async with maker_a() as session:
            count = await session.scalar(
                select(func.count()).select_from(Message).where(Message.lead_id == lead_id)
            )
        assert count == 1

    async def test_a_second_claim_while_the_first_is_in_flight_is_refused(
        self, db, org, lead_factory, mailbox
    ):
        """The hole the unique key does not close.

        A claim deliberately leaves the lead where it was, so the lead's state does not prevent
        a second claim. And because the lead lock serialises the two attempts, the second one
        runs after the first has committed — it counts that row and computes a *valid distinct*
        sequence number. Without this check, both emails go out.
        """
        lead = await lead_factory(state="APPROVED")
        first = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()
        assert first.claimed

        second = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        assert not second.claimed
        assert "already in flight" in (second.reason or "")

    async def test_a_resolved_send_does_not_block_a_later_kind(
        self, db, org, lead_factory, mailbox
    ):
        """Once resolved, the in-flight check must not keep blocking legitimate work."""
        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()
        await outbox.mark_sent(db, org_id=org.id, message_id=claim.message.id)
        await db.commit()

        followup = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_FOLLOWUP)
        assert followup.claimed

    async def test_the_sequence_advances_for_a_legitimate_second_send(
        self, db, org, lead_factory, mailbox
    ):
        """A follow-up is a second outbound message, so it gets its own key rather than
        colliding with the first."""
        lead = await lead_factory(state="APPROVED")
        first = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()
        await outbox.mark_sent(db, org_id=org.id, message_id=first.message.id)
        await db.commit()

        second = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_FOLLOWUP)
        await db.commit()

        assert second.claimed
        assert second.message.idempotency_key == f"{lead.id}:followup:1"
        assert first.message.idempotency_key == f"{lead.id}:initial:1"


class TestMarking:
    async def test_mark_sent_moves_the_lead_and_stamps_the_time(
        self, db, org, lead_factory, mailbox
    ):
        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()

        due = datetime.now(tz=UTC) + timedelta(days=3)
        message = await outbox.mark_sent(
            db,
            org_id=org.id,
            message_id=claim.message.id,
            smtp_response="250 OK",
            next_action_at=due,
        )
        await db.commit()
        await db.refresh(lead)

        assert message.status == "sent"
        assert message.sent_at is not None
        assert message.smtp_response == "250 OK"
        assert lead.state == "SENT"
        assert lead.last_message_at is not None
        assert lead.next_action_at is not None, "the follow-up must be scheduled"

    async def test_a_followup_increments_the_counter(self, db, org, lead_factory, mailbox):
        """`max_followups` cannot be enforced without this."""
        lead = await lead_factory(state="SENT")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_FOLLOWUP)
        await db.commit()
        await outbox.mark_sent(db, org_id=org.id, message_id=claim.message.id)
        await db.commit()
        await db.refresh(lead)

        assert lead.state == "FOLLOWUP_SENT"
        assert lead.followups_sent == 1

    async def test_an_approved_reply_puts_the_lead_in_conversation(
        self, db, org, lead_factory, mailbox
    ):
        lead = await lead_factory(state="REPLIED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_REPLY)
        await db.commit()
        await outbox.mark_sent(db, org_id=org.id, message_id=claim.message.id)
        await db.commit()
        await db.refresh(lead)

        assert lead.state == "IN_CONVERSATION"

    async def test_mark_failed_leaves_the_lead_where_it_was(self, db, org, lead_factory, mailbox):
        """A refused send is retryable, so the lead must not advance."""
        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()

        message = await outbox.mark_failed(
            db, org_id=org.id, message_id=claim.message.id, error="451 try again later"
        )
        await db.commit()
        await db.refresh(lead)

        assert message.status == "failed"
        assert lead.state == "APPROVED"

    async def test_mark_unknown_flags_for_a_human_and_does_not_move_the_lead(
        self, db, org, lead_factory, mailbox
    ):
        """The ambiguous case. Never auto-retried: a duplicate cold email is worse than a
        missed one, so a person decides."""
        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()

        message = await outbox.mark_unknown(
            db,
            org_id=org.id,
            message_id=claim.message.id,
            note="worker died after SMTP, before mark",
        )
        await db.commit()
        await db.refresh(lead)

        assert message.status == "unknown"
        assert lead.state == "APPROVED"

        from app.db.models import Event

        event = await db.scalar(
            select(Event).where(Event.lead_id == lead.id, Event.type == "message.unknown")
        )
        assert event is not None
        assert event.data["needs_human"] is True

    async def test_another_org_cannot_mark_our_message(self, db, org, lead_factory, mailbox):
        import uuid

        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()

        with pytest.raises(LookupError):
            await outbox.mark_sent(db, org_id=uuid.uuid4(), message_id=claim.message.id)


class TestCrashRecovery:
    async def test_a_message_stuck_in_sending_is_found(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        # Backdate the claim to simulate a worker that died mid-send.
        claim.message.created_at = datetime.now(tz=UTC) - timedelta(minutes=30)
        await db.commit()

        stuck = await outbox.find_stuck_sending(db, org_id=org.id)
        assert [m.id for m in stuck] == [claim.message.id]

    async def test_a_recent_claim_is_not_treated_as_stuck(self, db, org, lead_factory, mailbox):
        """A send in progress must be left alone, or we would resolve it out from under the
        worker that is still working on it."""
        lead = await lead_factory(state="APPROVED")
        await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        await db.commit()

        assert await outbox.find_stuck_sending(db, org_id=org.id) == []

    async def test_a_sent_message_is_not_stuck(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="APPROVED")
        claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
        claim.message.created_at = datetime.now(tz=UTC) - timedelta(hours=1)
        await db.commit()
        await outbox.mark_sent(db, org_id=org.id, message_id=claim.message.id)
        await db.commit()

        assert await outbox.find_stuck_sending(db, org_id=org.id) == []


class TestTheCapCanBeCountedFromMessages:
    async def test_sent_messages_are_countable_per_mailbox(self, db, org, lead_factory, mailbox):
        """§9.2: today's send count comes from `messages`, never a separate counter, so it
        cannot drift. This proves the data supports that."""
        for _ in range(3):
            lead = await lead_factory(state="APPROVED")
            claim = await outbox.claim_send(db, org_id=org.id, lead_id=lead.id, kind=KIND_INITIAL)
            await db.commit()
            await outbox.mark_sent(db, org_id=org.id, message_id=claim.message.id)
            await db.commit()

        count = await db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.mailbox_id == mailbox.id,
                Message.status == "sent",
                Message.sent_at.is_not(None),
            )
        )
        assert count == 3
