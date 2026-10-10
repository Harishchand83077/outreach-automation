"""Resolving an interrupted send by asking the mailbox what happened (ADR-002).

A row left in `sending` means the SMTP call was made and the worker died before recording the
result. The pre-generated `Message-ID` lets the mailbox answer the question our own data cannot.

The rule these tests exist to hold: **recovery never sends anything.** It marks a row `sent`
when the mailbox proves the mail left, and `unknown` — for a human — in every other case. A
duplicate cold email is worse than a missing one, so at-most-once wins over completeness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import outbox
from app.db.models import Draft, Event, Mailbox, Message
from app.domain.state import LeadState
from app.services.reply.recovery import GRACE, recover_stuck_sends

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)  # a Monday, inside the window


class FakePoller:
    """Answers `find_by_message_id` from a dict. Records what it was asked."""

    def __init__(self, found: dict[str, str] | None = None) -> None:
        self.found = found or {}
        self.asked: list[str] = []

    async def find_by_message_id(self, message_id, *, folders=()):
        self.asked.append(message_id)
        return self.found.get(message_id)


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org, campaign):
    record = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(record)
    await db.flush()
    campaign.mailbox_id = record.id
    await db.commit()
    return record


async def stuck_row(
    db,
    org,
    lead,
    *,
    message_id: str | None = "<stuck-1@outreach.example>",
    kind: str = "initial",
    age: timedelta = GRACE + timedelta(minutes=5),
    with_draft: bool = True,
) -> Message:
    """A row exactly as `claim_send` leaves it, aged past the grace period."""
    draft = None
    if with_draft:
        draft = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind=kind,
            version=1,
            subject="quick question",
            body="our pitch",
            status="approved",
        )
        db.add(draft)
        await db.flush()

    row = Message(
        org_id=org.id,
        lead_id=lead.id,
        direction="out",
        kind=kind,
        draft_id=draft.id if draft is not None else None,
        message_id_hdr=message_id,
        subject="quick question",
        body_text="our pitch",
        from_addr="asha@outreach.example",
        to_addr=lead.email_norm,
        status="sending",
        idempotency_key=f"{kind}:{lead.id}:1",
        created_at=NOW - age,
    )
    db.add(row)
    await db.commit()
    return row


class TestFoundInSent:
    async def test_it_is_marked_sent(self, db, session_factory, org, mailbox, lead_factory):
        """Confirmed empirically against the real provider before this was written."""
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead)

        poller = FakePoller({"<stuck-1@outreach.example>": "[Gmail]/Sent Mail"})
        report = await recover_stuck_sends(session_factory, poller=poller, org_id=org.id, now=NOW)

        assert report.checked == 1
        assert report.confirmed_sent == 1
        await db.refresh(row)
        assert row.status == "sent"
        assert "Sent Mail" in (row.smtp_response or "")

    async def test_the_lead_moves_on(self, db, session_factory, org, mailbox, lead_factory):
        lead = await lead_factory(state=LeadState.APPROVED.value)
        await stuck_row(db, org, lead)

        await recover_stuck_sends(
            session_factory,
            poller=FakePoller({"<stuck-1@outreach.example>": "Sent"}),
            org_id=org.id,
            now=NOW,
        )

        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value

    async def test_the_draft_is_marked_sent_so_it_cannot_be_sent_again(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        """A draft left `approved` is still eligible — that would be the duplicate."""
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead)

        await recover_stuck_sends(
            session_factory,
            poller=FakePoller({"<stuck-1@outreach.example>": "Sent"}),
            org_id=org.id,
            now=NOW,
        )

        draft = await db.get(Draft, row.draft_id)
        await db.refresh(draft)
        assert draft.status == "sent"

    async def test_the_followup_is_scheduled_from_when_it_was_actually_sent(
        self, db, session_factory, org, campaign, mailbox, lead_factory
    ):
        """Not from recovery time, which would push every follow-up out by the outage."""
        campaign.settings = {"max_followups": 1, "followup_after_days": 3}
        await db.commit()

        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead, age=timedelta(hours=20))

        await recover_stuck_sends(
            session_factory,
            poller=FakePoller({"<stuck-1@outreach.example>": "Sent"}),
            org_id=org.id,
            now=NOW,
        )

        await db.refresh(lead)
        assert lead.next_action_at is not None
        gap = lead.next_action_at - row.created_at
        assert timedelta(days=2) < gap < timedelta(days=6), (
            "measured from the send, not from the recovery"
        )


class TestNotFound:
    async def test_it_becomes_unknown_for_a_human(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead)

        report = await recover_stuck_sends(
            session_factory, poller=FakePoller({}), org_id=org.id, now=NOW
        )

        assert report.needs_human == 1
        await db.refresh(row)
        assert row.status == "unknown"

    async def test_an_unknown_row_asks_for_a_person_in_the_audit_trail(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state=LeadState.APPROVED.value)
        await stuck_row(db, org, lead)

        await recover_stuck_sends(session_factory, poller=FakePoller({}), org_id=org.id, now=NOW)

        event = await db.scalar(
            select(Event).where(Event.org_id == org.id, Event.type == "message.unknown")
        )
        assert event is not None
        assert event.data["needs_human"] is True

    async def test_the_lead_does_not_move(self, db, session_factory, org, mailbox, lead_factory):
        """`unknown` is not `sent`: claiming it was sent would hide a lead nobody contacted."""
        lead = await lead_factory(state=LeadState.APPROVED.value)
        await stuck_row(db, org, lead)

        await recover_stuck_sends(session_factory, poller=FakePoller({}), org_id=org.id, now=NOW)

        await db.refresh(lead)
        assert lead.state == LeadState.APPROVED.value

    async def test_a_row_with_no_message_id_cannot_be_asked_about(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead, message_id=None)

        poller = FakePoller({})
        report = await recover_stuck_sends(session_factory, poller=poller, org_id=org.id, now=NOW)

        assert report.needs_human == 1
        assert poller.asked == [], "there was nothing to search for"
        await db.refresh(row)
        assert row.status == "unknown"


class TestWhatIsLeftAlone:
    async def test_a_recent_row_is_inside_the_grace_period(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        """It may be in flight on another worker right now."""
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead, age=timedelta(minutes=2))

        report = await recover_stuck_sends(
            session_factory, poller=FakePoller({}), org_id=org.id, now=NOW
        )

        assert report.checked == 0
        await db.refresh(row)
        assert row.status == "sending"

    async def test_rows_already_resolved_are_not_revisited(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state=LeadState.SENT.value)
        row = await stuck_row(db, org, lead)
        row.status = "sent"
        await db.commit()

        report = await recover_stuck_sends(
            session_factory, poller=FakePoller({}), org_id=org.id, now=NOW
        )
        assert report.checked == 0

    async def test_another_orgs_rows_are_not_touched(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        from app.db.models import Org

        other = Org(name="Someone Else")
        db.add(other)
        await db.commit()

        lead = await lead_factory(state=LeadState.APPROVED.value)
        await stuck_row(db, org, lead)

        report = await recover_stuck_sends(
            session_factory, poller=FakePoller({}), org_id=other.id, now=NOW
        )
        assert report.checked == 0


class TestItNeverSends:
    async def test_recovery_has_no_smtp_dependency_at_all(self):
        """Structural: the module cannot send because it imports nothing that can.

        `SendLoop` is the only code that calls SMTP (hard rule 1). A recovery pass that could
        re-send would turn an interrupted send into two emails.
        """
        import app.services.reply.recovery as module
        from tests.helpers import executable_source

        source = executable_source(module)
        for forbidden in ("smtplib", "SmtpSender", "send_message", "sendmail"):
            assert forbidden not in source

    async def test_a_recovered_row_is_not_re_claimable(
        self, db, session_factory, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state=LeadState.APPROVED.value)
        row = await stuck_row(db, org, lead)

        await recover_stuck_sends(
            session_factory,
            poller=FakePoller({"<stuck-1@outreach.example>": "Sent"}),
            org_id=org.id,
            now=NOW,
        )
        await db.refresh(row)

        stuck = await outbox.find_stuck_sending(db, org_id=org.id, now=NOW)
        assert stuck == [], "nothing is left for a second pass to pick up"
