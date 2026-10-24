"""The campaign's file actually reaches the wire, and the tick box actually stops it.

**These tests drive `send_one` against a real SMTP server and assert on what the server
received**, not on what the send loop passed along. Every previous defect of this shape in this
project -- threading headers, the persona, the reply payload -- was a correct producer and a
correct consumer with nothing checking that they met, and each one was invisible to tests that
stopped at the boundary. So the assertion is on `envelope.content`.
"""

# Importing a fixture is how pytest shares one between modules, and taking it as a parameter
# then "redefines" the imported name -- which is exactly what is supposed to happen. Stated once
# here rather than on each of the thirty parameters it would otherwise decorate.
# ruff: noqa: F811

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.db import attachments as attachment_store
from app.db.models import Attachment, Draft
from app.db.repo import OrgContext
from app.domain.attachment import check
from app.domain.campaign import CampaignSettings
from app.services.mail.smtp import SmtpSender
from app.worker import send_loop

from .test_send_loop import (  # noqa: F401 — `mailbox` and `smtp_server` are used as fixtures
    INSIDE,
    allowlist_settings,
    approved_lead,
    mailbox,
    smtp_server,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

PDF = b"%PDF-1.7\nthe bytes are never parsed, only carried\n"


@pytest.fixture
def ctx(org):
    return OrgContext(org_id=org.id, actor_id="reviewer@example.com")


@pytest_asyncio.fixture(loop_scope="session")
async def with_deck(db, org, campaign):
    """The campaign has a deck uploaded."""
    checked = check("arkvane-deck.pdf", PDF)
    row = Attachment(
        org_id=org.id,
        campaign_id=campaign.id,
        filename=checked.filename,
        content_type=checked.content_type,
        size_bytes=checked.size_bytes,
        sha256=checked.sha256,
        content=checked.content,
        uploaded_by="owner@example.com",
    )
    db.add(row)
    await db.commit()
    return row


async def send(db, org, mailbox, smtp_server, *, now=INSIDE):
    """Drive the real `send_one`.

    **No settings parameter**, deliberately. `send_one` takes the mailbox's defaults and then
    rebinds `settings = chosen.settings` -- the campaign the chosen lead belongs to, which is
    what PR #67 established so that a lead could not be sent under another campaign's rules. A
    test that configured this through the parameter would be configuring something production
    throws away, which is the fixture-does-not-match-production trap that hid the persona
    crash-loop. To change the rules, change the campaign row: `configure` below.
    """
    sender = SmtpSender(allowlist_settings(smtp_server.host, smtp_server.port))
    return await send_loop.send_one(
        db,
        org_id=org.id,
        mailbox=mailbox,
        settings=CampaignSettings(),
        sender=sender,
        now=now,
        rng=random.Random(0),
    )


async def due_followup(db, ctx, lead_factory):
    """A lead whose follow-up has come due, with the draft approved and waiting.

    `next_action_at` is what makes it a candidate -- the send loop skips a follow-up whose clock
    has not arrived, which is the whole mechanism behind "one follow-up three days later".
    """
    lead, draft = await approved_lead(db, ctx, lead_factory, state="SENT", kind="followup")
    lead.next_action_at = INSIDE - timedelta(days=1)
    await db.commit()
    return lead, draft


async def configure(db, campaign, **settings):
    """Set the campaign's stored settings, the way the settings screen does."""
    campaign.settings = {**(campaign.settings or {}), **settings}
    await db.commit()


class TestTheFileReachesTheRecipient:
    async def test_a_first_email_carries_the_campaigns_deck(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        await approved_lead(db, ctx, lead_factory)

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        raw = smtp_server.messages[0]["raw"]
        assert "multipart/mixed" in raw
        assert "arkvane-deck.pdf" in raw

    async def test_the_body_is_still_plain_text_beside_it(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        """The locked rule is plain text only. An attachment adds a part; it does not turn the
        message into HTML and it does not hide the text from a client that cannot read MIME."""
        await approved_lead(db, ctx, lead_factory)

        await send(db, org, mailbox, smtp_server)

        raw = smtp_server.messages[0]["raw"]
        assert "I saw you are hiring backend engineers." in raw
        assert "text/html" not in raw

    async def test_a_campaign_with_no_file_sends_what_it_always_did(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
    ):
        await approved_lead(db, ctx, lead_factory)

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        raw = smtp_server.messages[0]["raw"]
        assert "multipart" not in raw


class TestWhoGetsIt:
    async def test_a_followup_does_not_carry_it_by_default(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        """Re-sending the same megabytes inside a thread that already has a copy is noise, and
        an attachment repeated on every message of a thread scores badly with filters."""
        await due_followup(db, ctx, lead_factory)

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert "arkvane-deck.pdf" not in smtp_server.messages[0]["raw"]

    async def test_a_campaign_may_choose_to_attach_to_followups_too(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        await due_followup(db, ctx, lead_factory)

        await configure(db, campaign, attach_followups=True)

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert "arkvane-deck.pdf" in smtp_server.messages[0]["raw"]

    async def test_turning_it_off_for_the_campaign_stops_every_email(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        await approved_lead(db, ctx, lead_factory)

        await configure(db, campaign, attach_initial=False)

        await send(db, org, mailbox, smtp_server)

        assert "arkvane-deck.pdf" not in smtp_server.messages[0]["raw"]


class TestTheReviewersTickBox:
    async def test_unticking_it_keeps_the_file_off_this_one_email(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        """The whole point of the override: the same campaign, the same deck, and one lead who
        should not get it."""
        _lead, draft = await approved_lead(db, ctx, lead_factory)
        draft.attach = False
        await db.commit()

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert "arkvane-deck.pdf" not in smtp_server.messages[0]["raw"]

    async def test_ticking_it_puts_the_file_on_a_reply(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        """Replies never carry it automatically, because whether a document belongs in an
        answer is a judgement about that one message. Somebody asking for the deck is the case
        this exists for."""
        _lead, draft = await approved_lead(db, ctx, lead_factory, state="REPLIED", kind="reply")
        draft.attach = True
        await db.commit()

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert "arkvane-deck.pdf" in smtp_server.messages[0]["raw"]

    async def test_a_reply_carries_nothing_when_nobody_ticked_it(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        await approved_lead(db, ctx, lead_factory, state="REPLIED", kind="reply")

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert "arkvane-deck.pdf" not in smtp_server.messages[0]["raw"]


class TestTheStore:
    async def test_uploading_twice_leaves_one_file(self, db, ctx, campaign):
        """Replacing rather than versioning: a second row would mean deciding which one a send
        uses, and the honest answer is what one row already says."""
        await attachment_store.put(
            db, ctx, campaign_id=campaign.id, checked=check("v1.pdf", PDF), uploaded_by="a@b.c"
        )
        await attachment_store.put(
            db,
            ctx,
            campaign_id=campaign.id,
            checked=check("v2.pdf", b"%PDF-1.7\nsecond"),
            uploaded_by="a@b.c",
        )
        await db.commit()

        info = await attachment_store.describe(db, ctx, campaign_id=campaign.id)
        assert info is not None
        assert info.filename == "v2.pdf"

    async def test_describe_never_loads_the_bytes(self, db, ctx, campaign, with_deck):
        """A review queue that pulled five megabytes per draft is a review queue nobody opens
        twice. `describe` answers from the columns beside the blob."""
        info = await attachment_store.describe(db, ctx, campaign_id=campaign.id)

        assert info is not None
        assert info.size_bytes == len(PDF)
        assert not hasattr(info, "content")

    async def test_another_orgs_attachment_is_invisible(self, db, ctx, campaign, with_deck):
        import uuid as _uuid

        stranger = OrgContext(org_id=_uuid.uuid4())

        assert await attachment_store.describe(db, stranger, campaign_id=campaign.id) is None
        assert await attachment_store.remove(db, stranger, campaign_id=campaign.id) is False

    async def test_deleting_the_file_does_not_stop_the_campaign_sending(
        self,
        db,
        ctx,
        org,
        campaign,
        mailbox,
        lead_factory,
        smtp_server,
        with_deck,
    ):
        """An operator who removes the attachment while drafts wait has decided those emails go
        without it. Refusing to send them would be the system breaking a campaign over an
        absent nicety."""
        await approved_lead(db, ctx, lead_factory)
        await attachment_store.remove(db, ctx, campaign_id=campaign.id)
        await db.commit()

        decision = await send(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert "arkvane-deck.pdf" not in smtp_server.messages[0]["raw"]


class TestTheDraftToggleRoute:
    async def test_a_judged_draft_may_no_longer_change_what_it_carries(self, db, ctx, lead_factory):
        """A control that silently did nothing would be worse than one that says no."""
        from app.db import drafts as draft_store

        lead = await lead_factory(state="SENT")
        draft = Draft(
            org_id=ctx.org_id,
            lead_id=lead.id,
            kind="initial",
            version=1,
            subject="Sent already",
            body="Gone.",
            status="sent",
            approved_at=datetime.now(tz=UTC),
        )
        db.add(draft)
        await db.commit()

        assert await draft_store.set_attach(db, ctx, draft_id=draft.id, attach=False) is False

    async def test_an_unknown_draft_is_not_found_rather_than_an_error(self, db, ctx):
        import uuid as _uuid

        from app.db import drafts as draft_store

        assert await draft_store.set_attach(db, ctx, draft_id=_uuid.uuid4(), attach=True) is None
