"""`ImapLoop`: which folders get polled, and what happens when the server cannot be asked.

The loop had no test at all, which is how it came to read only INBOX in production. The first
real reply this system received was put in Spam by the receiving account -- a consequence of
sending `From: rahul@arkvane.in` through an unrelated Gmail, so the message was unauthenticated
-- and the reply engine never saw it. The dashboard showed no reply, which is indistinguishable
from no reply having been sent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from app.core.settings import config
from app.db.models import Mailbox
from app.services.mail.imap import PollResult
from app.worker.context import WorkerContext
from app.worker.imap_loop import FOLDERS, ImapLoop

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 15, 11, 0, tzinfo=UTC)


class RecordingPoller:
    """Records every folder it is asked to poll, and what it advertises."""

    def __init__(self, *, advertise: tuple[str, ...] | None = None, raises: bool = False) -> None:
        self._advertise = advertise
        self._raises = raises
        self.polled: list[str] = []
        self.listed = 0

    async def folders(self) -> tuple[str, ...]:
        self.listed += 1
        if self._raises:
            raise OSError("imap unreachable")
        return self._advertise if self._advertise is not None else ("INBOX",)

    async def poll(self, *, folder, last_uid, known_uidvalidity, limit, now=None):
        self.polled.append(folder)
        return PollResult(folder=folder, uidvalidity=known_uidvalidity, highest_uid=last_uid)

    async def find_by_message_id(self, message_id, *, folders=()):
        return None


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


def loop_with(session_factory, poller) -> ImapLoop:
    return ImapLoop(
        WorkerContext(
            session_factory=session_factory,
            settings=config,
            poller=poller,  # type: ignore[arg-type]
            sender=object(),  # type: ignore[arg-type]
        )
    )


class TestWhichFoldersArePolled:
    async def test_the_junk_folder_is_polled_as_well_as_inbox(
        self, db, org, mailbox, session_factory
    ):
        poller = RecordingPoller(advertise=("INBOX", "[Gmail]/Spam"))
        loop = loop_with(session_factory, poller)

        await loop.poll_all(NOW)

        assert poller.polled == ["INBOX", "[Gmail]/Spam"]

    async def test_inbox_is_still_polled_when_there_is_no_junk_folder(
        self, db, org, mailbox, session_factory
    ):
        poller = RecordingPoller(advertise=("INBOX",))
        loop = loop_with(session_factory, poller)

        await loop.poll_all(NOW)

        assert poller.polled == ["INBOX"]

    async def test_a_failed_listing_does_not_stop_the_poll(self, db, org, mailbox, session_factory):
        """A folder list is not worth failing a cycle over. INBOX is the fallback, and the next
        expiry tries again."""
        poller = RecordingPoller(raises=True)
        loop = loop_with(session_factory, poller)

        report = await loop.poll_all(NOW)

        assert poller.polled == list(FOLDERS)
        assert report.mailboxes == 1

    async def test_the_folder_list_is_not_fetched_on_every_cycle(
        self, db, org, mailbox, session_factory
    ):
        """A LIST means a login, and the provider counts those. Folders do not come and go."""
        poller = RecordingPoller(advertise=("INBOX", "[Gmail]/Spam"))
        loop = loop_with(session_factory, poller)

        await loop.poll_all(NOW)
        await loop.poll_all(NOW)
        await loop.poll_all(NOW)

        assert poller.listed == 1
        assert poller.polled.count("[Gmail]/Spam") == 3

    async def test_a_paused_mailbox_is_still_read(self, db, org, mailbox, session_factory):
        """Pausing stops *sending*, deliberately -- not reading. A reply or an opt-out that
        arrives while a mailbox is paused still has to be honoured, and an opt-out we refused to
        read because we had stopped sending is the one we would breach on starting again."""
        from sqlalchemy import update

        await db.execute(update(Mailbox).values(paused=True))
        await db.commit()
        poller = RecordingPoller(advertise=("INBOX", "[Gmail]/Spam"))
        loop = loop_with(session_factory, poller)

        report = await loop.poll_all(NOW)

        assert poller.polled == ["INBOX", "[Gmail]/Spam"]
        assert report.mailboxes >= 1
