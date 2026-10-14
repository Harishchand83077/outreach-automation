"""Two live campaigns on one mailbox: whose email goes first, and whose name is on it.

`find_candidates` has always pulled leads from **every** running campaign on a mailbox, while
`SendLoop` handed `send_one` a single persona taken from the *oldest* campaign. So a lead
belonging to campaign B was picked up and sent **signed by campaign A** — wrong sender name,
wrong agency, wrong footer — and scheduled under campaign A's window and follow-up rules.

That is not an ordering problem, it is a sender-identity one, and it is the kind that is
invisible until a recipient replies to the wrong person.

**The ordering decision, now that it has to be made.** The daily cap belongs to the mailbox,
because the cap exists to protect a *sending reputation* and reputation attaches to the address,
not to how many campaigns an operator happens to have split the work into. So the campaigns
share one cap and the loop interleaves them:

1. **Replies first, always, across every campaign.** Somebody who wrote to us is waiting, and
   which campaign they came from is our bookkeeping, not their problem.
2. **Then due follow-ups, then first emails** — unchanged (§9.2).
3. **Within a kind, round-robin across campaigns**, then oldest approval first.

Round-robin rather than strict first-approved-first-sent: with one cap, a campaign holding five
hundred approved drafts would otherwise starve a campaign holding five for a working week, and
an operator running two campaigns expects both to move.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.core.settings import Settings
from app.db.models import Campaign, Draft, Mailbox, Message
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.services.mail.smtp import SmtpSender
from app.worker import send_loop

pytestmark = pytest.mark.asyncio(loop_scope="session")

IST = ZoneInfo("Asia/Kolkata")
INSIDE = datetime(2026, 10, 12, 11, 0, tzinfo=IST).astimezone(UTC)

ACME = Persona(
    sender_name="Asha Rao",
    sender_role="Founder",
    agency_name="Acme Software",
    offer="Fixed-scope data pipeline projects",
    goal="book a 20-minute call",
    booking_url="https://cal.example.com/asha",
)
BOREALIS = Persona(
    sender_name="Vik Menon",
    sender_role="Partner",
    agency_name="Borealis Labs",
    offer="Embedded analytics for logistics",
    goal="book a 20-minute call",
    booking_url="https://cal.example.com/vik",
)


@pytest_asyncio.fixture(loop_scope="session")
async def smtp_server():
    import socket

    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    class Collector:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(
                {
                    "rcpt": list(envelope.rcpt_tos),
                    "raw": envelope.content.decode("utf-8", "replace"),
                }
            )
            return "250 OK"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    handler = Collector()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        authenticator=lambda *a: AuthResult(success=True),
        auth_require_tls=False,
    )
    controller.start()
    try:
        handler.host = "127.0.0.1"
        handler.port = port
        yield handler
    finally:
        controller.stop()


def sender_for(smtp_server, allow: str) -> SmtpSender:
    return SmtpSender(
        Settings(
            _env_file=None,
            GROQ_API_KEY="gsk_test",
            SMTP_EMAIL="asha@outreach.example",
            SMTP_PASSWORD="not-a-real-password",
            SMTP_HOST=smtp_server.host,
            SMTP_PORT=smtp_server.port,
            SMTP_USE_TLS=False,
            SMTP_START_TLS=False,
            SEND_MODE="allowlist",
            SEND_ALLOWLIST=allow,
        )
    )


@pytest_asyncio.fixture(loop_scope="session")
async def shared_mailbox(db, org, campaign):
    """One mailbox, and a second running campaign beside the fixture's."""
    mailbox = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(mailbox)
    await db.flush()

    campaign.mailbox_id = mailbox.id
    campaign.persona = ACME.model_dump(mode="json")
    campaign.status = "running"

    second = Campaign(
        org_id=org.id,
        mailbox_id=mailbox.id,
        name="Borealis Q4",
        status="running",
        persona=BOREALIS.model_dump(mode="json"),
        settings={},
    )
    db.add(second)
    await db.commit()
    return mailbox, campaign, second


async def approved_lead_in(db, org, campaign_id, email, *, approved_at):
    from app.db.models import Lead

    lead = Lead(
        org_id=org.id,
        campaign_id=campaign_id,
        state="APPROVED",
        email_norm=email,
        email_raw=email,
        full_name="A Lead",
        company_name="A Company",
    )
    db.add(lead)
    await db.flush()
    db.add(
        Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            version=1,
            subject="Your pipeline hiring",
            body="Hi,\n\nSaw you are hiring.\n\nRegards",
            status="approved",
            approved_by="reviewer@example.com",
            approved_at=approved_at,
        )
    )
    await db.commit()
    return lead


async def send_once(db, org, mailbox, smtp_server, *, allow):
    # No persona is passed: `send_one` takes the signature from the chosen candidate's own
    # campaign, which is the whole point of this file.
    return await send_loop.send_one(
        db,
        org_id=org.id,
        mailbox=mailbox,
        settings=CampaignSettings(),
        sender=sender_for(smtp_server, allow),
        now=INSIDE,
    )


class TestTheSenderIsTheLeadsOwnCampaign:
    async def test_a_second_campaigns_lead_is_signed_by_that_campaign(
        self, db, org, shared_mailbox, smtp_server
    ):
        """The defect: campaign B's lead went out signed by campaign A."""
        mailbox, _first, second = shared_mailbox
        await approved_lead_in(db, org, second.id, "borealis@example.com", approved_at=INSIDE)

        decision = await send_once(db, org, mailbox, smtp_server, allow="borealis@example.com")

        assert decision.sent, decision.reason
        raw = smtp_server.messages[-1]["raw"]
        assert "Borealis Labs" in raw, "the lead was signed by the wrong campaign"
        assert "Acme Software" not in raw

    async def test_the_first_campaigns_lead_is_still_signed_by_it(
        self, db, org, shared_mailbox, smtp_server
    ):
        mailbox, first, _second = shared_mailbox
        await approved_lead_in(db, org, first.id, "acme@example.com", approved_at=INSIDE)

        decision = await send_once(db, org, mailbox, smtp_server, allow="acme@example.com")

        assert decision.sent, decision.reason
        raw = smtp_server.messages[-1]["raw"]
        assert "Acme Software" in raw
        assert "Borealis Labs" not in raw


class TestTheCampaignsShareOneCapFairly:
    async def test_both_campaigns_make_progress(self, db, org, shared_mailbox, smtp_server):
        """Round-robin: a campaign with a backlog must not starve one with a few leads.

        Campaign A has three approved drafts, all approved before campaign B's single one.
        Strict first-approved-first-sent would send all of A before touching B.
        """
        mailbox, first, second = shared_mailbox
        base = INSIDE
        for n in range(3):
            await approved_lead_in(db, org, first.id, f"acme{n}@example.com", approved_at=base)
        await approved_lead_in(db, org, second.id, "borealis0@example.com", approved_at=base)

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )

        order = [str(c.lead.campaign_id) for c in candidates]
        assert order[0] != order[1], f"one campaign took both leading slots: {order}"

    async def test_a_reply_outranks_every_first_email(self, db, org, shared_mailbox, smtp_server):
        """Somebody who wrote to us is waiting; which campaign they came from is our problem."""
        from app.db.models import Lead

        mailbox, first, second = shared_mailbox
        await approved_lead_in(db, org, first.id, "acme9@example.com", approved_at=INSIDE)

        replier = Lead(
            org_id=org.id,
            campaign_id=second.id,
            state="REPLIED",
            email_norm="replier@example.com",
            email_raw="replier@example.com",
            full_name="Replier",
            company_name="Co",
        )
        db.add(replier)
        await db.flush()
        db.add(
            Draft(
                org_id=org.id,
                lead_id=replier.id,
                kind="reply",
                version=1,
                subject="Re: hello",
                body="Thanks for getting back.",
                status="approved",
                approved_by="reviewer@example.com",
                approved_at=INSIDE,
            )
        )
        await db.commit()

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )

        assert candidates[0].kind == "reply"
        assert candidates[0].lead.email_norm == "replier@example.com"

    async def test_the_cap_is_shared_across_campaigns(self, db, org, shared_mailbox, smtp_server):
        """One mailbox, one cap: reputation attaches to the address, not to our bookkeeping."""
        mailbox, first, second = shared_mailbox
        mailbox.daily_cap = 1
        await approved_lead_in(db, org, second.id, "borealis1@example.com", approved_at=INSIDE)
        db.add(
            Message(
                org_id=org.id,
                lead_id=(
                    await approved_lead_in(
                        db, org, first.id, "acme8@example.com", approved_at=INSIDE
                    )
                ).id,
                mailbox_id=mailbox.id,
                direction="out",
                status="sent",
                kind="initial",
                subject="earlier",
                body_text="hi",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                idempotency_key=str(uuid.uuid4()),
                sent_at=INSIDE,
            )
        )
        await db.commit()

        decision = await send_once(db, org, mailbox, smtp_server, allow="borealis1@example.com")

        assert not decision.sent
        assert "cap" in decision.reason


class TestACampaignThatCannotSignItsMailDoesNotSend:
    """The regression that reached production, and the one this file was supposed to prevent.

    PR #67 gave each candidate its own campaign so a lead could not go out signed by another
    campaign. It did that with two lazy properties -- `Candidate.persona` and
    `Candidate.settings` -- that called `model_validate` when read, which is *after* the
    candidate has been chosen, inside `send_one`, with no `try` between there and
    `WorkerTask.run`. So a campaign whose persona was missing a field raised, the error escaped
    `tick()`, the task recorded a failure and backed off, and **no mailbox sent anything at
    all** -- visible only as a traceback in the worker log.

    That is the identical shape as the `send_days` incident that produced per-mailbox reporting
    in the first place. A lazy property put it straight back, which is worth stating plainly:
    validation that happens where the failure cannot be contained is the bug, not the invalid
    data.

    The fallback was the other half. `send_one` resolved
    `chosen.persona if chosen.campaign.persona else persona`, so a campaign with an *empty*
    persona borrowed the mailbox's first campaign's identity instead -- the very thing the
    change was made to stop.
    """

    async def test_an_unreadable_persona_does_not_stop_the_other_campaign(
        self, db, org, shared_mailbox, smtp_server
    ):
        mailbox, first, second = shared_mailbox
        # Unreadable, not merely thin: since ADR-013 a persona with only a name is valid and
        # sends perfectly well -- the footer needs a name and nothing else. What the send loop
        # cannot work with is a row the model refuses outright, which is what a hand-edited
        # value produces.
        second.persona = {"sender_name": "", "max_words": 9000}
        await approved_lead_in(db, org, second.id, "broken@example.com", approved_at=INSIDE)
        await approved_lead_in(db, org, first.id, "fine@example.com", approved_at=INSIDE)
        await db.commit()

        decision = await send_once(db, org, mailbox, smtp_server, allow="fine@example.com")

        assert decision.sent, decision.reason
        raw = smtp_server.messages[-1]["raw"]
        assert "fine@example.com" in raw
        assert "Acme Software" in raw

    async def test_the_broken_campaigns_lead_is_not_sent_under_another_name(
        self, db, org, shared_mailbox, smtp_server
    ):
        """It must not send at all rather than send wearing the wrong identity. A recipient
        replying to the wrong person is how this is discovered otherwise."""
        mailbox, first, second = shared_mailbox
        second.persona = {}
        first.status = "paused"  # so the only candidate available is the broken campaign's
        await approved_lead_in(db, org, second.id, "orphan@example.com", approved_at=INSIDE)
        await db.commit()

        before = len(smtp_server.messages)
        decision = await send_once(db, org, mailbox, smtp_server, allow="orphan@example.com")

        assert not decision.sent
        assert len(smtp_server.messages) == before, "nothing may reach the wire"

    async def test_unreadable_settings_also_skip_only_that_campaign(
        self, db, org, shared_mailbox, smtp_server
    ):
        """`send_days` as weekday *names* is the original incident, in its per-campaign form."""
        mailbox, first, second = shared_mailbox
        second.settings = {"send_days": ["mon", "tue"]}
        await approved_lead_in(db, org, second.id, "badsettings@example.com", approved_at=INSIDE)
        await approved_lead_in(db, org, first.id, "good@example.com", approved_at=INSIDE)
        await db.commit()

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )

        assert {str(c.lead.campaign_id) for c in candidates} == {str(first.id)}

    async def test_the_whole_tick_survives_an_unreadable_campaign(
        self, db, org, shared_mailbox, smtp_server, session_factory
    ):
        """Through `SendLoop.send_pass`, not `send_one`: the crash-loop happened in the layers
        between them, so a test that calls `send_one` directly cannot see it. This is the same
        lesson as PR #54 -- exercise the method the worker actually calls."""
        from app.core.settings import config
        from app.worker.context import WorkerContext
        from app.worker.send_task import SendLoop

        _mailbox, _first, second = shared_mailbox
        second.persona = {"sender_name": "", "max_words": 9000}
        await approved_lead_in(db, org, second.id, "broken2@example.com", approved_at=INSIDE)
        await db.commit()

        ctx = WorkerContext(
            session_factory=session_factory,
            settings=config,
            poller=object(),  # type: ignore[arg-type]
            sender=sender_for(smtp_server, "nobody@example.com"),
        )
        loop = SendLoop(ctx)

        # No exception: that is the assertion. An exception here is the production failure.
        report = await loop.send_pass(now=INSIDE)

        assert report.sent == 0
        assert report.mailboxes >= 1


class TestAPausedCampaignStopsOnlyItself:
    async def test_pausing_one_campaign_leaves_the_other_sending(
        self, db, org, shared_mailbox, smtp_server
    ):
        mailbox, first, second = shared_mailbox
        await approved_lead_in(db, org, first.id, "acme7@example.com", approved_at=INSIDE)
        await approved_lead_in(db, org, second.id, "borealis7@example.com", approved_at=INSIDE)
        second.paused = True
        await db.commit()

        candidates = await send_loop.find_candidates(
            db, org_id=org.id, mailbox_id=mailbox.id, now=INSIDE
        )

        assert {str(c.lead.campaign_id) for c in candidates} == {str(first.id)}
