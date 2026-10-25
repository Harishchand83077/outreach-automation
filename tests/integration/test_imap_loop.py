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
from cryptography.fernet import Fernet

from app.core.settings import Settings
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
        #: Who each poll authenticated as. The point of the per-mailbox work: a second org's
        #: replies are in their inbox, not the operator's.
        self.logins: list[str | None] = []

    async def folders(self, *, login=None) -> tuple[str, ...]:
        self.listed += 1
        if self._raises:
            raise OSError("imap unreachable")
        return self._advertise if self._advertise is not None else ("INBOX",)

    async def poll(self, *, folder, last_uid, known_uidvalidity, limit, now=None, login=None):
        self.polled.append(folder)
        self.logins.append(login.username if login is not None else None)
        return PollResult(folder=folder, uidvalidity=known_uidvalidity, highest_uid=last_uid)

    async def find_by_message_id(self, message_id, *, folders=(), login=None):
        return None


#: The deployment's own account. `login_for` hands out the environment credentials only to an
#: org the migration granted them to, and only for the address `SMTP_EMAIL` logs in as, so these
#: two values are what make the ordinary single-tenant path the ordinary path here too.
ENV_ADDRESS = "asha@outreach.example"

ENV_SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    GROQ_API_KEY="gsk_test",
    SMTP_EMAIL=ENV_ADDRESS,
    SMTP_PASSWORD="from-the-environment",
    IMAP_EMAIL=ENV_ADDRESS,
    IMAP_PASSWORD="from-the-environment",
)


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org):
    row = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email=ENV_ADDRESS,
        reply_to=ENV_ADDRESS,
        daily_cap=100,
    )
    db.add(row)
    await db.commit()
    return row


def loop_with(session_factory, poller, settings: Settings | None = None) -> ImapLoop:
    return ImapLoop(
        WorkerContext(
            session_factory=session_factory,
            settings=settings or ENV_SETTINGS,
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


class TestWhoseInboxIsRead:
    """Every mailbox used to be polled with the one account in `.env`.

    `ImapPoller` read `IMAP_EMAIL`/`IMAP_PASSWORD` directly and never asked `login_for`, so the
    loop's careful "once per mailbox" pass logged into the **same inbox** every time. For a
    single-tenant install that is correct and invisible. With a second workspace it is two
    separate faults at once: their replies, bounces and opt-outs are in *their* inbox and were
    never fetched, and the operator's own mail was ingested into the stranger's org as orphaned
    inbound messages.

    An unhonoured opt-out is the part that matters most. It is not merely a missing feature.
    """

    async def test_a_connected_mailbox_is_read_as_itself(self, db, org, session_factory):
        from app.db.models import MailCredential, Org
        from app.services.credentials import seal

        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            GROQ_API_KEY="gsk_test",
            CREDENTIAL_KEY=Fernet.generate_key().decode(),
        )
        theirs = Org(name="Somebody who signed up")
        db.add(theirs)
        await db.flush()
        mailbox = Mailbox(
            org_id=theirs.id,
            from_name="Dev",
            from_email="dev@theirco.example",
            reply_to="dev@theirco.example",
            daily_cap=100,
        )
        db.add(mailbox)
        await db.flush()
        db.add(
            MailCredential(
                org_id=theirs.id,
                mailbox_id=mailbox.id,
                kind="app_password",
                account_email="dev@theirco.example",
                secret=seal("their-own-password", settings).token,
            )
        )
        await db.commit()

        poller = RecordingPoller()
        report = await loop_with(session_factory, poller, settings).poll_all()

        assert report.mailboxes == 1
        # Not the operator's address. This is the whole feature in one assertion.
        assert poller.logins == ["dev@theirco.example"]

    async def test_an_unconnected_mailbox_is_skipped_not_read_as_the_operator(
        self, db, org, session_factory
    ):
        from app.db.models import Org

        theirs = Org(name="Signed up and never connected")
        db.add(theirs)
        await db.flush()
        db.add(
            Mailbox(
                org_id=theirs.id,
                from_name="Nobody",
                from_email="nobody@theirco.example",
                reply_to="nobody@theirco.example",
                daily_cap=100,
            )
        )
        await db.commit()

        poller = RecordingPoller()
        report = await loop_with(session_factory, poller).poll_all()

        assert poller.polled == [], "the operator's inbox must not be read for somebody else"
        assert report.skipped == 1
        # Counted as skipped, and reported — but it is an ordinary state, not a crash.
        assert any("nobody@theirco.example" in e for e in report.errors)

    async def test_one_unconnected_mailbox_does_not_stop_the_others(
        self, db, org, mailbox, session_factory
    ):
        """The lesson this project keeps relearning: contain a failure to the row it belongs to."""
        from app.db.models import Org

        theirs = Org(name="Signed up and never connected")
        db.add(theirs)
        await db.flush()
        db.add(
            Mailbox(
                org_id=theirs.id,
                from_name="Nobody",
                from_email="nobody@theirco.example",
                reply_to="nobody@theirco.example",
                daily_cap=100,
            )
        )
        await db.commit()

        poller = RecordingPoller()
        report = await loop_with(session_factory, poller).poll_all()

        assert report.mailboxes == 1
        assert report.skipped == 1
        assert poller.logins == [ENV_ADDRESS]
