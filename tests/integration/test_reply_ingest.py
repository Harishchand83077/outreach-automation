"""The ingest loop: poll, process, then move the cursor — in that order.

§10.1. The ordering is the point. The cursor trails the work, so a crash repeats a message
instead of skipping one, and `already_stored` makes the repeat harmless. The prototype had no
cursor at all: it searched `UNSEEN` and fetched with `RFC822`, which marked mail read as a side
effect, so a message it chose not to act on was gone for good (B10).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import ImapCursor, Mailbox, Message
from app.domain.campaign import CampaignSettings
from app.domain.reply import ReplyLabel
from app.domain.state import LeadState
from app.services.mail.imap import FetchedMessage, PollResult
from app.services.mail.parse import parse_message
from app.services.reply.classify import ClassifyResult
from app.services.reply.ingest import ALERT_AFTER_FAILURES, ingest_folder

pytestmark = pytest.mark.asyncio(loop_scope="session")

SETTINGS = CampaignSettings()


def raw_reply(*, uid: int, message_id: str, body: str, in_reply_to: str = "") -> FetchedMessage:
    headers = [
        "From: Ben Shah <lead1@example.com>",
        "To: asha@outreach.example",
        "Subject: Re: quick question",
        f"Message-ID: <{message_id}>",
        "Date: Mon, 6 Oct 2025 11:02:00 +0530",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: <{in_reply_to}>")
    raw = ("\r\n".join(headers) + "\r\n\r\n" + body + "\r\n").encode()
    return FetchedMessage(uid=uid, folder="INBOX", raw=raw, parsed=parse_message(raw))


class FakePoller:
    """Returns queued poll results. Records what it was asked for, so the cursor can be checked."""

    def __init__(self, *results: PollResult) -> None:
        self.results = list(results)
        self.asked: list[tuple[int, int | None]] = []
        self.found: dict[str, str | None] = {}

    async def poll(self, *, folder, last_uid, known_uidvalidity, limit, now=None):
        self.asked.append((last_uid, known_uidvalidity))
        if self.results:
            return self.results.pop(0)
        return PollResult(folder=folder, uidvalidity=known_uidvalidity, highest_uid=last_uid)

    async def find_by_message_id(self, message_id, *, folders=()):
        return self.found.get(message_id)


class FakeClassifier:
    """Stands in for the LLM. `labels` is consumed in order."""

    def __init__(self, *labels: ReplyLabel) -> None:
        self.queue = list(labels)
        self.calls = 0

    async def structured_with_entry(self, schema, *, task, system, user, lead_id=None, **kw):
        raise AssertionError("the ingest tests patch classify_reply instead")

    def result(self) -> ClassifyResult:
        self.calls += 1
        label = self.queue.pop(0) if self.queue else ReplyLabel.OTHER
        return ClassifyResult(
            label=label,
            confidence=0.9,
            rationale="fake",
            llm_entry="fake-entry",
            prompt_version="classify_reply.test",
        )


@pytest.fixture
def patched_classifier(monkeypatch):
    """Patch the classifier at its call site, so the loop is tested without a model."""
    classifier = FakeClassifier()

    async def fake_classify(router, parsed, *, lead_id=None):
        return classifier.result()

    monkeypatch.setattr("app.services.reply.ingest.classify_reply", fake_classify)
    return classifier


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


async def sent_message(db, org, lead, message_id: str) -> Message:
    """An outbound message for the inbound mail to thread against."""
    row = Message(
        org_id=org.id,
        lead_id=lead.id,
        direction="out",
        kind="initial",
        message_id_hdr=f"<{message_id}>",
        subject="quick question",
        body_text="our pitch",
        from_addr="asha@outreach.example",
        to_addr=lead.email_norm,
        status="sent",
        idempotency_key=f"initial:{lead.id}:1",
    )
    db.add(row)
    await db.commit()
    return row


async def run(session_factory, poller, org, mailbox, **kw):
    return await ingest_folder(
        session_factory,
        poller=poller,
        router=object(),
        org_id=org.id,
        mailbox_id=mailbox.id,
        settings=SETTINGS,
        **kw,
    )


class TestTheCursor:
    async def test_it_advances_only_after_a_message_is_processed(
        self, db, session_factory, org, mailbox, lead_factory, patched_classifier
    ):
        patched_classifier.queue = [ReplyLabel.INTERESTED]
        lead = await lead_factory(state=LeadState.SENT.value, email="lead1@example.com")
        await sent_message(db, org, lead, "ours-1@outreach.example")

        poller = FakePoller(
            PollResult(
                folder="INBOX",
                messages=[
                    raw_reply(
                        uid=42,
                        message_id="in-1@globex.example",
                        body="Yes, keen to talk.",
                        in_reply_to="ours-1@outreach.example",
                    )
                ],
                uidvalidity=777,
                highest_uid=42,
            )
        )

        report = await run(session_factory, poller, org, mailbox)

        assert report.polled == 1
        assert report.last_uid == 42
        cursor = await db.scalar(select(ImapCursor).where(ImapCursor.org_id == org.id))
        await db.refresh(cursor)
        assert cursor.last_uid == 42
        assert cursor.uidvalidity == 777

    async def test_the_next_poll_asks_from_where_it_got_to(
        self, db, session_factory, org, mailbox, patched_classifier
    ):
        poller = FakePoller(
            PollResult(folder="INBOX", uidvalidity=777, highest_uid=0),
            PollResult(folder="INBOX", uidvalidity=777, highest_uid=0),
        )
        await run(session_factory, poller, org, mailbox)
        await run(session_factory, poller, org, mailbox)

        assert poller.asked[0] == (0, None)
        assert poller.asked[1] == (0, 777), "the UIDVALIDITY is remembered"

    async def test_it_stops_at_a_message_it_could_not_process(
        self, db, session_factory, org, mailbox, lead_factory, monkeypatch
    ):
        """Advancing past an unprocessable message would lose it for ever."""
        await lead_factory(state=LeadState.SENT.value, email="lead1@example.com")

        calls = {"n": 0}

        async def exploding_classify(router, parsed, *, lead_id=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("a bug, not a provider failure")
            return ClassifyResult(
                label=ReplyLabel.OTHER,
                confidence=0.9,
                rationale="",
                llm_entry="fake",
                prompt_version="test",
            )

        monkeypatch.setattr("app.services.reply.ingest.classify_reply", exploding_classify)

        poller = FakePoller(
            PollResult(
                folder="INBOX",
                messages=[
                    raw_reply(uid=10, message_id="in-a@globex.example", body="First one."),
                    raw_reply(uid=11, message_id="in-b@globex.example", body="Second one."),
                    raw_reply(uid=12, message_id="in-c@globex.example", body="Third one."),
                ],
                uidvalidity=777,
                highest_uid=12,
            )
        )

        report = await run(session_factory, poller, org, mailbox)

        assert report.errors, "the failure is reported"
        assert report.last_uid == 10, "the cursor stopped before the one that failed"
        cursor = await db.scalar(select(ImapCursor).where(ImapCursor.org_id == org.id))
        await db.refresh(cursor)
        assert cursor.last_uid == 10

    async def test_it_never_moves_backwards_on_a_rescan(
        self, db, session_factory, org, mailbox, patched_classifier
    ):
        """A UIDVALIDITY rescan replays old UIDs; the cursor must not regress."""
        poller = FakePoller(
            PollResult(folder="INBOX", uidvalidity=777, highest_uid=90),
            PollResult(
                folder="INBOX",
                messages=[raw_reply(uid=3, message_id="in-old@globex.example", body="Hello.")],
                uidvalidity=888,
                highest_uid=3,
                rescanned=True,
            ),
        )
        await run(session_factory, poller, org, mailbox)
        report = await run(session_factory, poller, org, mailbox)

        assert report.rescanned
        cursor = await db.scalar(select(ImapCursor).where(ImapCursor.org_id == org.id))
        await db.refresh(cursor)
        assert cursor.last_uid == 90, "a replayed low UID does not rewind the cursor"
        assert cursor.uidvalidity == 888


class TestRepeatsAreHarmless:
    async def test_the_same_message_twice_is_stored_once(
        self, db, session_factory, org, mailbox, lead_factory, patched_classifier
    ):
        """What makes the cursor-trails-work ordering safe."""
        lead = await lead_factory(state=LeadState.SENT.value, email="lead1@example.com")
        await sent_message(db, org, lead, "ours-1@outreach.example")
        duplicate = raw_reply(
            uid=5,
            message_id="in-dup@globex.example",
            body="Interested, yes.",
            in_reply_to="ours-1@outreach.example",
        )

        poller = FakePoller(
            PollResult(folder="INBOX", messages=[duplicate], uidvalidity=1, highest_uid=5),
            PollResult(folder="INBOX", messages=[duplicate], uidvalidity=1, highest_uid=5),
        )
        first = await run(session_factory, poller, org, mailbox)
        second = await run(session_factory, poller, org, mailbox)

        assert first.duplicates == 0
        assert second.duplicates == 1
        stored = (
            (
                await db.execute(
                    select(Message).where(
                        Message.org_id == org.id,
                        Message.direction == "in",
                        Message.message_id_hdr == "<in-dup@globex.example>",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stored) == 1


class TestFailures:
    async def test_a_failed_poll_is_counted_not_raised(self, db, session_factory, org, mailbox):
        """IMAP being unreachable is normal operation and must not kill the worker."""
        poller = FakePoller(PollResult(folder="INBOX", error="could not connect: timeout"))
        report = await run(session_factory, poller, org, mailbox)

        assert report.poll_error
        assert not report.should_alert, "one failure is not an alert"
        cursor = await db.scalar(select(ImapCursor).where(ImapCursor.org_id == org.id))
        await db.refresh(cursor)
        assert cursor.consecutive_failures == 1
        assert cursor.last_error

    async def test_three_consecutive_failures_ask_for_a_human(
        self, db, session_factory, org, mailbox
    ):
        poller = FakePoller(
            *[PollResult(folder="INBOX", error="down") for _ in range(ALERT_AFTER_FAILURES)]
        )
        reports = [await run(session_factory, poller, org, mailbox) for _ in range(3)]

        assert [r.should_alert for r in reports] == [False, False, True]

    async def test_a_success_clears_the_failure_count(
        self, db, session_factory, org, mailbox, patched_classifier
    ):
        poller = FakePoller(
            PollResult(folder="INBOX", error="down"),
            PollResult(folder="INBOX", uidvalidity=1, highest_uid=0),
        )
        await run(session_factory, poller, org, mailbox)
        await run(session_factory, poller, org, mailbox)

        cursor = await db.scalar(select(ImapCursor).where(ImapCursor.org_id == org.id))
        await db.refresh(cursor)
        assert cursor.consecutive_failures == 0
        assert cursor.last_error is None


class TestOrphans:
    async def test_mail_matching_no_lead_is_stored_not_dropped(
        self, db, session_factory, org, mailbox, patched_classifier
    ):
        """§10.2: unmatched mail is never deleted and never marked read."""
        poller = FakePoller(
            PollResult(
                folder="INBOX",
                messages=[
                    raw_reply(uid=7, message_id="in-orphan@globex.example", body="Who is this?")
                ],
                uidvalidity=1,
                highest_uid=7,
            )
        )
        report = await run(session_factory, poller, org, mailbox)

        assert report.orphans == 1
        stored = await db.scalar(
            select(Message).where(Message.message_id_hdr == "<in-orphan@globex.example>")
        )
        assert stored is not None
        assert stored.lead_id is None
