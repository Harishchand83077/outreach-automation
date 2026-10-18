"""A follow-up continues the thread it is following up.

Reported from a real mailbox: the follow-up arrived as a separate conversation rather than under
the email it was chasing.

**Every outbound message went out with no `In-Reply-To` and no `References`.** `build_message`
has always accepted both, and `build_references` has always known how to extend a chain -- the
send loop simply never passed anything to `claim_send`. A correct producer and a correct
consumer with nothing connecting them, which is by now the signature defect of this project.

The subject was the other half: clients group on `References` first but fall back to the subject,
and the follow-up drafter was inventing a fresh one. Replies have forced the thread's subject
since Phase 6; follow-ups did not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.db import outbox
from app.db.models import Draft, Mailbox, Message
from app.services.mail.message import build_references

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)


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


async def message_on(
    db, org, lead, mailbox, *, direction, kind, when, subject="Your pipeline", references=None
):
    row = Message(
        org_id=org.id,
        lead_id=lead.id,
        mailbox_id=mailbox.id,
        direction=direction,
        status="sent" if direction == "out" else "received",
        kind=kind,
        subject=subject,
        body_text="Hello",
        message_id_hdr=f"<{uuid.uuid4()}@outreach.example>",
        references_hdr=references,
        idempotency_key=str(uuid.uuid4()) if direction == "out" else None,
        sent_at=when if direction == "out" else None,
        created_at=when,
    )
    db.add(row)
    await db.commit()
    return row


class TestWhatEachKindThreadsOnto:
    async def test_a_followup_threads_onto_our_last_message(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="SENT")
        first = await message_on(
            db, org, lead, mailbox, direction="out", kind="initial", when=NOW - timedelta(days=3)
        )

        in_reply_to, references = await outbox.thread_headers(db, lead_id=lead.id, kind="followup")

        assert in_reply_to == first.message_id_hdr
        assert references == first.message_id_hdr

    async def test_a_second_followup_threads_onto_the_first(self, db, org, lead_factory, mailbox):
        """The chain extends rather than restarting, so the third message still names the
        first -- which is how a client groups a thread when an intermediate message is missing
        from somebody's mailbox."""
        lead = await lead_factory(state="SENT")
        first = await message_on(
            db, org, lead, mailbox, direction="out", kind="initial", when=NOW - timedelta(days=6)
        )
        second = await message_on(
            db,
            org,
            lead,
            mailbox,
            direction="out",
            kind="followup",
            when=NOW - timedelta(days=3),
            references=first.message_id_hdr,
        )

        in_reply_to, references = await outbox.thread_headers(db, lead_id=lead.id, kind="followup")

        assert in_reply_to == second.message_id_hdr
        assert references == build_references(first.message_id_hdr, second.message_id_hdr)
        assert first.message_id_hdr in references

    async def test_a_reply_threads_onto_their_message(self, db, org, lead_factory, mailbox):
        lead = await lead_factory(state="REPLIED")
        await message_on(
            db, org, lead, mailbox, direction="out", kind="initial", when=NOW - timedelta(days=2)
        )
        theirs = await message_on(
            db, org, lead, mailbox, direction="in", kind="reply", when=NOW - timedelta(hours=2)
        )

        in_reply_to, _references = await outbox.thread_headers(db, lead_id=lead.id, kind="reply")

        assert in_reply_to == theirs.message_id_hdr, "we answer their message, not our own"

    async def test_a_first_email_starts_a_thread(self, db, org, lead_factory):
        lead = await lead_factory(state="APPROVED")

        assert await outbox.thread_headers(db, lead_id=lead.id, kind="initial") == (None, None)

    async def test_nothing_to_thread_onto_is_not_an_error(self, db, org, lead_factory):
        """A follow-up with no visible parent is still a useful email, and refusing to send it
        would be a worse outcome than sending it unthreaded."""
        lead = await lead_factory(state="SENT")

        assert await outbox.thread_headers(db, lead_id=lead.id, kind="followup") == (None, None)


class TestTheHeadersReachTheMessage:
    async def test_a_claimed_followup_carries_them(self, db, org, lead_factory, mailbox):
        """The gap that caused the bug: the lookup existing is not the same as the send loop
        using it, so this asserts on the stored row rather than on the helper."""
        lead = await lead_factory(state="SENT")
        first = await message_on(
            db, org, lead, mailbox, direction="out", kind="initial", when=NOW - timedelta(days=3)
        )
        draft = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="followup",
            version=1,
            subject="Re: Your pipeline",
            body="Following up.",
            status="approved",
        )
        db.add(draft)
        await db.flush()

        in_reply_to, references = await outbox.thread_headers(db, lead_id=lead.id, kind="followup")
        claim = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind="followup",
            mailbox_id=mailbox.id,
            draft_id=draft.id,
            subject=draft.subject,
            body_text=draft.body,
            in_reply_to=in_reply_to,
            references=references,
            now=NOW,
        )

        assert claim.claimed, claim.reason
        assert claim.message is not None
        assert claim.message.in_reply_to == first.message_id_hdr
        assert claim.message.references_hdr == first.message_id_hdr

    async def test_the_sent_email_carries_them_on_the_wire(self, db, org, lead_factory, mailbox):
        """Headers on the row are not headers in the mail. `build_message` is what a recipient
        actually sees."""
        from app.services.mail.message import FooterIdentity, build_message
        from app.services.mail.message import Mailbox as MailboxIdentity

        parent_id = f"<{uuid.uuid4()}@outreach.example>"
        built = build_message(
            mailbox=MailboxIdentity(
                from_name="Asha Rao",
                from_email="asha@outreach.example",
                reply_to="asha@outreach.example",
            ),
            to_email="lead@example.com",
            subject="Re: Your pipeline",
            body="Following up.",
            identity=FooterIdentity(sender_name="Asha Rao", agency_name="Arkvane"),
            message_id=f"<{uuid.uuid4()}@outreach.example>",
            in_reply_to=parent_id,
            references=parent_id,
            now=NOW,
        )

        raw = built.as_bytes().decode("utf-8", "replace")
        assert "In-Reply-To:" in raw
        assert "References:" in raw
        assert parent_id in raw


class TestTheSubjectKeepsTheThread:
    async def test_a_followup_must_keep_the_threads_subject(self):
        """Clients group on `References` first but fall back to the subject, so a follow-up
        with a fresh subject is filed as a new conversation by anything that does."""
        from app.domain.persona import Persona
        from app.services.drafting.schema import DraftOutput
        from app.services.drafting.validate import validate_draft

        persona = Persona(
            sender_name="Asha Rao",
            sender_role="Founder",
            agency_name="Arkvane",
            offer="a prototype",
            goal="book a call",
            cta_text="Worth a call?",
        )
        fresh = DraftOutput(subject="Checking in", body="Just following up.", claims=[])

        report = validate_draft(fresh, persona=persona, hooks=[], kind="followup")

        assert any("keep the thread" in failure for failure in report.failures), report.failures

    async def test_an_inherited_subject_may_be_long(self):
        """A follow-up inherits whatever subject the thread started with, and shortening it
        would break threading in some clients and look evasive in all."""
        from app.domain.persona import Persona
        from app.services.drafting.schema import DraftOutput
        from app.services.drafting.validate import validate_draft

        persona = Persona(
            sender_name="Asha Rao",
            sender_role="Founder",
            agency_name="Arkvane",
            offer="a prototype",
            goal="book a call",
            cta_text="Worth a call?",
        )
        long_subject = DraftOutput(
            subject="Re: a rather long original subject line about the pipeline work",
            body=(
                "Hi there,\n\nCircling back on the pipeline work we discussed. Happy to "
                "send over a short plan if it is still useful this quarter.\n\nAsha"
            ),
            claims=[],
        )

        report = validate_draft(long_subject, persona=persona, hooks=[], kind="followup")

        assert not any("words" in failure for failure in report.failures), report.failures

    async def test_a_first_email_still_may_not_fake_a_reply(self):
        """Unchanged, and the reason it matters: faking a reply to a conversation that never
        happened is a deception recipients recognise instantly."""
        from app.domain.persona import Persona
        from app.services.drafting.schema import DraftOutput
        from app.services.drafting.validate import validate_draft

        persona = Persona(
            sender_name="Asha Rao",
            sender_role="Founder",
            agency_name="Arkvane",
            offer="a prototype",
            goal="book a call",
            cta_text="Worth a call?",
        )
        faked = DraftOutput(subject="Re: our chat", body="Hello there.", claims=[])

        report = validate_draft(faked, persona=persona, hooks=[], kind="initial")

        assert any("must not begin" in failure for failure in report.failures)
