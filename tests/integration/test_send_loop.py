"""The send loop, against a real database and a real SMTP server.

`aiosmtpd` runs in-process, so the whole path is exercised — MIME construction, the SMTP
conversation, the outbox transitions — without a packet leaving the machine. Every recipient is
`@example.com` and `SEND_MODE` is never `live`.

The tests that matter most here are the refusals: outside the window, over the cap, suppressed,
paused. Each one is a way the prototype would have sent mail it should not have.
"""

from __future__ import annotations

import random
import socket
import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.settings import Settings
from app.db import drafts as drafts_repo
from app.db import repo
from app.db.models import Draft, Mailbox, Message
from app.db.repo import OrgContext
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.services.mail.smtp import SmtpSender
from app.worker import send_loop

pytestmark = pytest.mark.asyncio(loop_scope="session")

IST = ZoneInfo("Asia/Kolkata")
SETTINGS = CampaignSettings()

PERSONA = Persona(
    sender_name="Asha Rao",
    sender_role="Founder",
    agency_name="Acme Software",
    offer="Fixed-scope data pipeline projects",
    goal="book a 20-minute call",
    booking_url="https://cal.example.com/asha",
)

#: A Monday, 11:00 IST — inside the window.
INSIDE = datetime(2026, 10, 12, 11, 0, tzinfo=IST).astimezone(UTC)


def allowlist_settings(host: str, port: int, *, allow: str = "ben@example.com") -> Settings:
    """Real SMTP against the fake server, with the allowlist as the only gate."""
    return Settings(
        _env_file=None,
        GROQ_API_KEY="gsk_test",
        SMTP_EMAIL="asha@outreach.example",
        SMTP_PASSWORD="not-a-real-password",
        SMTP_HOST=host,
        SMTP_PORT=port,
        SMTP_USE_TLS=False,
        SMTP_START_TLS=False,
        SEND_MODE="allowlist",
        SEND_ALLOWLIST=allow,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def smtp_server():
    """An in-process SMTP server that records what it received."""
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    class Collector:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(
                {
                    "from": envelope.mail_from,
                    "rcpt": list(envelope.rcpt_tos),
                    "raw": envelope.content.decode("utf-8", errors="replace"),
                }
            )
            return "250 OK"

    def accept_any(server, session, envelope, mechanism, auth_data):
        return AuthResult(success=True)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    handler = Collector()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        authenticator=accept_any,
        auth_require_tls=False,
    )
    controller.start()
    try:
        handler.host = "127.0.0.1"
        handler.port = port
        yield handler
    finally:
        controller.stop()


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


@pytest.fixture
def ctx(org):
    return OrgContext(org_id=org.id)


async def approved_lead(
    db,
    ctx,
    lead_factory,
    *,
    state="AWAITING_APPROVAL",
    kind="initial",
    email="ben@example.com",
    **lead_kw,
):
    """A lead with an approved draft, ready for the loop to pick up."""
    lead = await lead_factory(state=state, email=email, **lead_kw)
    draft = Draft(
        org_id=ctx.org_id,
        lead_id=lead.id,
        kind=kind,
        version=1,
        subject="Your pipeline hiring",
        body=(
            "Hi Ben,\n\nI saw you are hiring backend engineers.\n\n"
            "Worth a short call? https://cal.example.com/asha\n\nAsha"
        ),
        status="approved",
        approved_by="reviewer@example.com",
        approved_at=datetime.now(tz=UTC),
    )
    db.add(draft)
    await db.flush()
    if state == "AWAITING_APPROVAL":
        from app.db.state import transition
        from app.domain.state import LeadState

        await transition(db, org_id=ctx.org_id, lead_id=lead.id, to_state=LeadState.APPROVED)
    await db.commit()
    return lead, draft


async def run_once(
    db, org, mailbox, smtp_server, *, now=INSIDE, settings=SETTINGS, allow="ben@example.com"
):
    sender = SmtpSender(allowlist_settings(smtp_server.host, smtp_server.port, allow=allow))
    return await send_loop.send_one(
        db,
        org_id=org.id,
        mailbox=mailbox,
        persona=PERSONA,
        settings=settings,
        sender=sender,
        now=now,
        rng=random.Random(0),
    )


class TestAnActualSend:
    async def test_an_approved_lead_is_sent_and_recorded(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        lead, _ = await approved_lead(db, ctx, lead_factory)

        decision = await run_once(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert len(smtp_server.messages) == 1
        assert smtp_server.messages[0]["rcpt"] == ["ben@example.com"]

        await db.refresh(lead)
        assert lead.state == "SENT"
        assert lead.last_message_at is not None

    async def test_the_message_row_records_what_was_sent(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """E3: 'what exactly did we send to this person?' must be answerable."""
        lead, _ = await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)

        row = await db.scalar(select(Message).where(Message.lead_id == lead.id))
        assert row.status == "sent"
        assert row.sent_at is not None
        assert row.message_id_hdr
        assert row.subject == "Your pipeline hiring"
        assert row.to_addr == "ben@example.com"

    async def test_the_message_id_on_the_wire_matches_the_database(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """§9.5's crash recovery searches the mailbox by this id, so they must agree."""
        lead, _ = await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)

        row = await db.scalar(select(Message).where(Message.lead_id == lead.id))
        assert row.message_id_hdr in smtp_server.messages[0]["raw"]

    async def test_the_sent_email_carries_the_compliance_headers(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """D1: no unsubscribe mechanism existed at all before."""
        await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)

        raw = smtp_server.messages[0]["raw"]
        assert "List-Unsubscribe:" in raw
        assert "mailto:asha@outreach.example?subject=unsubscribe" in raw
        assert "no thanks" in raw, "the opt-out line must be in the body"
        assert "Acme Software" in raw, "the sender identity must be in the body"

    async def test_a_followup_is_scheduled_after_the_first_send(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """A7: there was no follow-up sequence at all."""
        lead, _ = await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)
        await db.refresh(lead)

        assert lead.next_action_at is not None
        due_local = lead.next_action_at.astimezone(IST)
        assert due_local.date() == date(2026, 10, 15), "Monday + 3 days = Thursday"
        assert due_local.weekday() < 5

    async def test_the_draft_is_marked_sent(self, db, ctx, org, mailbox, lead_factory, smtp_server):
        _, draft = await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)
        await db.refresh(draft)
        assert draft.status == "sent"

    async def test_pacing_is_set_for_the_next_send(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """100/day over 7.5 hours is a send every few minutes, never a burst."""
        await approved_lead(db, ctx, lead_factory)
        decision = await run_once(db, org, mailbox, smtp_server)
        await db.refresh(mailbox)

        assert mailbox.next_send_at is not None
        assert mailbox.next_send_at > INSIDE
        assert decision.wait > timedelta(seconds=30)


class TestRefusals:
    """Each of these is a way the prototype would have sent mail it should not have."""

    async def test_nothing_is_sent_outside_the_window(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        await approved_lead(db, ctx, lead_factory)
        saturday = datetime(2026, 10, 10, 11, 0, tzinfo=IST).astimezone(UTC)

        decision = await run_once(db, org, mailbox, smtp_server, now=saturday)

        assert not decision.sent
        assert "window" in decision.reason
        assert smtp_server.messages == []
        # And it waits until Monday rather than spinning.
        assert decision.wait > timedelta(hours=24)

    async def test_nothing_is_sent_after_hours(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        await approved_lead(db, ctx, lead_factory)
        late = datetime(2026, 10, 12, 22, 0, tzinfo=IST).astimezone(UTC)

        decision = await run_once(db, org, mailbox, smtp_server, now=late)
        assert not decision.sent
        assert smtp_server.messages == []

    async def test_a_recipient_outside_the_allowlist_is_never_sent_to(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """The last line of defence, checked at the SMTP boundary itself."""
        lead, _ = await approved_lead(db, ctx, lead_factory, email="stranger@example.com")

        decision = await run_once(db, org, mailbox, smtp_server, allow="someone-else@example.com")

        assert not decision.sent
        assert "blocked" in decision.reason
        assert smtp_server.messages == [], "nothing may reach the server"

        await db.refresh(lead)
        assert lead.state == "SKIPPED"
        assert "not_allowlisted" in lead.flags

    async def test_a_suppressed_recipient_is_not_sent_to(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """Re-checked immediately before sending: an approval can be hours old, and an opt-out
        may have arrived since."""
        lead, _ = await approved_lead(db, ctx, lead_factory)
        await repo.add_suppression(ctx, db, value=lead.email_norm, reason="opt_out")
        await db.commit()

        decision = await run_once(db, org, mailbox, smtp_server)

        assert not decision.sent
        assert "suppressed" in decision.reason
        assert smtp_server.messages == []

    async def test_a_paused_org_stops_everything(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """The global kill switch, checked first so one flip stops all of it."""
        await approved_lead(db, ctx, lead_factory)
        org.paused = True
        await db.commit()

        decision = await run_once(db, org, mailbox, smtp_server)
        assert not decision.sent
        assert "organisation is paused" in decision.reason
        assert smtp_server.messages == []

    async def test_a_paused_mailbox_stops_sending(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        await approved_lead(db, ctx, lead_factory)
        mailbox.paused = True
        mailbox.pause_reason = "bounce rate above 5%"
        await db.commit()

        decision = await run_once(db, org, mailbox, smtp_server)
        assert not decision.sent
        assert "bounce rate" in decision.reason

    async def test_a_paused_campaign_stops_its_leads(
        self, db, ctx, org, campaign, mailbox, lead_factory, smtp_server
    ):
        await approved_lead(db, ctx, lead_factory)
        campaign.paused = True
        await db.commit()

        decision = await run_once(db, org, mailbox, smtp_server)
        assert not decision.sent
        assert smtp_server.messages == []

    async def test_pacing_holds_the_next_send_back(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        await approved_lead(db, ctx, lead_factory)
        mailbox.next_send_at = INSIDE + timedelta(minutes=4)
        await db.commit()

        decision = await run_once(db, org, mailbox, smtp_server)
        assert not decision.sent
        assert decision.reason == "pacing"
        assert smtp_server.messages == []

    async def test_nothing_ready_is_not_an_error(self, db, org, mailbox, smtp_server):
        decision = await run_once(db, org, mailbox, smtp_server)
        assert not decision.sent
        assert "nothing is ready" in decision.reason


class TestTheCap:
    async def test_the_cap_is_counted_from_messages_not_a_counter(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """§9.2: a stored counter drifts the first time a crash lands between the send and the
        increment, and a drifted cap is how a mailbox gets suspended."""
        await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)

        counted = await send_loop.sent_today(
            db, mailbox_id=mailbox.id, settings=SETTINGS, now=INSIDE
        )
        assert counted == 1

    async def test_reaching_the_cap_stops_sending_until_tomorrow(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        mailbox.daily_cap = 1
        await db.commit()

        await approved_lead(db, ctx, lead_factory, email="ben@example.com")
        first = await run_once(db, org, mailbox, smtp_server)
        assert first.sent

        await approved_lead(db, ctx, lead_factory, email="ben2@example.com")
        mailbox.next_send_at = None
        await db.commit()

        second = await run_once(
            db, org, mailbox, smtp_server, allow="ben@example.com,ben2@example.com"
        )
        assert not second.sent
        assert "daily cap" in second.reason
        assert len(smtp_server.messages) == 1

    async def test_the_warmup_ramp_limits_a_new_mailbox(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """A mailbox that sends 100 on day one looks exactly like a compromised one."""
        mailbox.warmup_started_on = INSIDE
        mailbox.warmup_start = 1
        mailbox.warmup_step = 10
        mailbox.warmup_step_days = 2
        await db.commit()

        cap = await send_loop.effective_cap(db, mailbox=mailbox, settings=SETTINGS, now=INSIDE)
        assert cap == 1, "day one of the ramp"

        await approved_lead(db, ctx, lead_factory, email="ben@example.com")
        assert (await run_once(db, org, mailbox, smtp_server)).sent

        await approved_lead(db, ctx, lead_factory, email="ben2@example.com")
        mailbox.next_send_at = None
        await db.commit()
        second = await run_once(
            db, org, mailbox, smtp_server, allow="ben@example.com,ben2@example.com"
        )
        assert not second.sent
        assert "daily cap" in second.reason

    async def test_the_cap_counts_all_outbound_kinds(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """Q1, as answered: 100/day covers initial, follow-up and reply alike, so steady state
        is about 50 new leads plus 50 follow-ups."""
        lead, _ = await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)

        counted = await send_loop.sent_today(
            db, mailbox_id=mailbox.id, settings=SETTINGS, now=INSIDE
        )
        assert counted == 1

        # A follow-up for the same lead counts against the same allowance.
        lead.next_action_at = INSIDE
        draft = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="followup",
            version=1,
            subject="Following up",
            body="Hi Ben,\n\nStill worth a chat?\n\nAsha",
            status="approved",
            approved_at=datetime.now(tz=UTC),
        )
        db.add(draft)
        mailbox.next_send_at = None
        await db.commit()

        assert (await run_once(db, org, mailbox, smtp_server)).sent
        counted = await send_loop.sent_today(
            db, mailbox_id=mailbox.id, settings=SETTINGS, now=INSIDE
        )
        assert counted == 2


class TestPriority:
    async def test_a_reply_goes_before_a_new_lead(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """§9.2: ignoring someone who wrote to us is the worst outcome available."""
        await approved_lead(db, ctx, lead_factory, email="newlead@example.com")

        replier = await lead_factory(state="REPLIED", email="replier@example.com")
        db.add(
            Draft(
                org_id=org.id,
                lead_id=replier.id,
                kind="reply",
                version=1,
                subject="Re: your question",
                body="Hi,\n\nHere is the answer.\n\nAsha",
                status="approved",
                approved_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )
        assert candidates[0].kind == "reply"

    async def test_a_followup_that_is_not_due_is_not_a_candidate(
        self, db, ctx, org, mailbox, lead_factory
    ):
        lead = await lead_factory(state="SENT", email="later@example.com")
        lead.next_action_at = INSIDE + timedelta(days=2)
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="followup",
                version=1,
                subject="Following up",
                body="Hi,\n\nStill interested?\n\nAsha",
                status="approved",
                approved_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )
        assert candidates == []

    async def test_a_stale_draft_for_a_moved_on_lead_is_not_a_candidate(
        self, db, ctx, org, mailbox, lead_factory
    ):
        """An initial draft for a lead that has already been sent to must not go out again."""
        lead = await lead_factory(state="SENT", email="already@example.com")
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="initial",
                version=1,
                subject="Hello again",
                body="Hi,\n\nFirst contact.\n\nAsha",
                status="approved",
                approved_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )
        assert candidates == []


class TestNoDuplicates:
    async def test_a_second_pass_does_not_send_the_same_email_again(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        """B12: the prototype sent, then wrote the database, so a crash in between resent."""
        await approved_lead(db, ctx, lead_factory)
        assert (await run_once(db, org, mailbox, smtp_server)).sent

        mailbox.next_send_at = None
        await db.commit()
        second = await run_once(db, org, mailbox, smtp_server)

        assert not second.sent
        assert len(smtp_server.messages) == 1

        count = await db.scalar(select(func.count()).select_from(Message))
        assert count == 1

    async def test_an_approved_draft_cannot_be_sent_twice_via_the_review_queue(
        self, db, ctx, org, mailbox, lead_factory, smtp_server
    ):
        _, draft = await approved_lead(db, ctx, lead_factory)
        await run_once(db, org, mailbox, smtp_server)

        with pytest.raises(ValueError):
            await drafts_repo.approve(
                db, ctx, draft_id=draft.id, approved_by="reviewer@example.com"
            )


class TestFailureHandling:
    async def test_an_unreachable_server_records_a_failure_without_moving_the_lead(
        self, db, ctx, org, mailbox, lead_factory
    ):
        lead, _ = await approved_lead(db, ctx, lead_factory)
        broken = SmtpSender(
            Settings(
                _env_file=None,
                GROQ_API_KEY="gsk_test",
                SMTP_EMAIL="asha@outreach.example",
                SMTP_PASSWORD="x",
                SMTP_HOST="127.0.0.1",
                SMTP_PORT=1,
                SMTP_TIMEOUT=2,
                SMTP_START_TLS=False,
                SEND_MODE="allowlist",
                SEND_ALLOWLIST="ben@example.com",
            )
        )

        decision = await send_loop.send_one(
            db,
            org_id=org.id,
            mailbox=mailbox,
            persona=PERSONA,
            settings=SETTINGS,
            sender=broken,
            now=INSIDE,
            rng=random.Random(0),
        )

        assert not decision.sent
        assert "failed" in decision.reason

        await db.refresh(lead)
        assert lead.state == "APPROVED", "retryable, so the lead must not advance"

        row = await db.scalar(select(Message).where(Message.lead_id == lead.id))
        assert row.status == "failed"

    async def test_another_orgs_lead_is_never_a_candidate(self, db, org, mailbox, lead_factory):
        candidates = await send_loop.find_candidates(
            db, org_id=uuid.uuid4(), mailbox_id=mailbox.id, now=INSIDE
        )
        assert candidates == []
