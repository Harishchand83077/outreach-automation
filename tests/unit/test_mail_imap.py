r"""The poller, against a fake `imaplib`. No network, ever (test conventions).

These tests are mostly about what the poller does *not* do. The prototype searched `UNSEEN` and
fetched with `RFC822`, which sets `\Seen` as a side effect, so:

- a reply arriving while its lead was not in the one state the monitor watched was discarded
  *and* marked read, invisible for ever;
- unrelated mail in the operator's own inbox was marked read too (issue B10).

So the fake records every command it receives, and the tests assert on that record: `readonly`
on SELECT, `BODY.PEEK[]` on FETCH, and no `STORE` of any kind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest

from app.services.mail import imap as imap_module
from app.services.mail.imap import RESCAN_DAYS, ImapPoller, _quote_folder

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def raw(message_id: str, body: str = "Yes, keen.") -> bytes:
    return (
        "From: Ben Shah <ben@globex.example>\r\n"
        "To: asha@outreach.example\r\n"
        "Subject: Re: quick question\r\n"
        f"Message-ID: <{message_id}>\r\n"
        "Date: Mon, 6 Oct 2025 11:02:00 +0530\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode()


class FakeIMAP:
    """Just enough of `imaplib.IMAP4_SSL`, recording every call."""

    error = Exception
    instances: ClassVar[list[FakeIMAP]] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.selected_readonly: bool | None = None
        self.uidvalidity = 777
        self.messages: dict[int, bytes] = {}
        self.login_fails = False
        self.logged_out = False
        FakeIMAP.instances.append(self)

    # -- commands ---------------------------------------------------------
    def login(self, user: str, password: str):
        self.calls.append(("login", (user,)))
        if self.login_fails:
            raise imap_module.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"welcome"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", (folder, readonly)))
        self.selected_readonly = readonly
        return "OK", [f"[UIDVALIDITY {self.uidvalidity}] 3 EXISTS".encode()]

    def response(self, key):
        return key, [f"[UIDVALIDITY {self.uidvalidity}]".encode()]

    def uid(self, command, *args):
        self.calls.append((f"uid {command}", args))
        if command == "SEARCH":
            criteria = " ".join(str(a) for a in args)
            if "SINCE" in criteria:
                hits = sorted(self.messages)
            else:
                low = int(criteria.split(":")[0])
                hits = [uid for uid in sorted(self.messages) if uid >= low]
            return "OK", [" ".join(str(uid) for uid in hits).encode()]
        if command == "FETCH":
            uid = int(str(args[0]))
            if uid not in self.messages:
                return "OK", [None]
            return "OK", [(f"{uid} (BODY[] {{x}})".encode(), self.messages[uid]), b")"]
        raise AssertionError(f"unexpected uid command {command}")

    def search(self, charset, *criteria):
        self.calls.append(("search", criteria))
        wanted = str(criteria[-1]).strip('"')
        hits = [uid for uid, body in self.messages.items() if wanted.encode() in body]
        return "OK", [" ".join(str(uid) for uid in hits).encode()]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP.instances = []
    monkeypatch.setattr(imap_module.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


class FakeSettings:
    IMAP_HOST = "imap.example.com"
    IMAP_PORT = 993
    IMAP_EMAIL = "asha@outreach.example"
    # A fake, and the tests below assert it never appears in an error message.
    IMAP_PASSWORD = "not-a-real-password"


def poller() -> ImapPoller:
    return ImapPoller(settings=FakeSettings())  # type: ignore[arg-type]


class TestTheMailboxIsNeverModified:
    async def test_select_is_readonly(self, fake_imap):
        await poller().poll(folder="INBOX", now=NOW)
        connection = fake_imap.instances[0]
        assert connection.selected_readonly is True

    async def test_fetch_uses_peek(self, fake_imap):
        """`BODY[]` would set \\Seen even in a readonly session on some servers."""
        FakeIMAP.instances = []
        monkey = poller()
        connection_messages = {5: raw("in-1@globex.example")}

        result = await _poll_with(monkey, connection_messages)

        assert len(result.messages) == 1
        fetches = [args for name, args in fake_imap.instances[0].calls if name == "uid FETCH"]
        assert fetches, "something was fetched"
        assert all("(BODY.PEEK[])" in args for args in fetches)

    async def test_nothing_is_ever_stored_or_flagged(self, fake_imap):
        await _poll_with(poller(), {5: raw("in-1@globex.example")})
        names = [name for name, _ in fake_imap.instances[0].calls]
        assert not any("STORE" in name.upper() for name in names)
        assert not any("COPY" in name.upper() for name in names)

    async def test_unseen_is_never_searched_for(self, fake_imap):
        """Progress is tracked by UID, not by somebody else's read state."""
        await _poll_with(poller(), {5: raw("in-1@globex.example")})
        searches = [
            " ".join(str(a) for a in args)
            for name, args in fake_imap.instances[0].calls
            if name == "uid SEARCH"
        ]
        assert searches
        assert not any("UNSEEN" in s.upper() for s in searches)

    async def test_the_connection_is_always_closed(self, fake_imap):
        await poller().poll(folder="INBOX", now=NOW)
        assert fake_imap.instances[0].logged_out


class TestUidProgress:
    async def test_only_messages_above_the_cursor_are_returned(self, fake_imap):
        result = await _poll_with(
            poller(),
            {3: raw("old@globex.example"), 7: raw("new@globex.example")},
            last_uid=5,
        )
        assert [m.uid for m in result.messages] == [7]
        assert result.highest_uid == 7

    async def test_an_empty_poll_leaves_the_cursor_where_it_was(self, fake_imap):
        result = await _poll_with(poller(), {}, last_uid=42)
        assert result.messages == []
        assert result.highest_uid == 42

    async def test_the_limit_is_respected(self, fake_imap):
        messages = {uid: raw(f"m{uid}@globex.example") for uid in range(1, 11)}
        result = await _poll_with(poller(), messages, limit=4)
        assert len(result.messages) == 4


class TestUidvalidity:
    async def test_a_changed_uidvalidity_triggers_a_dated_rescan(self, fake_imap):
        """Every stored UID is meaningless, so fall back to a bounded date search."""
        result = await _poll_with(
            poller(),
            {1: raw("a@globex.example"), 2: raw("b@globex.example")},
            last_uid=99,
            known_uidvalidity=111,
        )

        assert result.rescanned
        assert [m.uid for m in result.messages] == [1, 2], "low UIDs are replayed"
        searches = [
            " ".join(str(a) for a in args)
            for name, args in fake_imap.instances[0].calls
            if name == "uid SEARCH"
        ]
        assert any("SINCE" in s for s in searches)

    async def test_an_unchanged_uidvalidity_does_not_rescan(self, fake_imap):
        result = await _poll_with(
            poller(), {1: raw("a@globex.example")}, last_uid=99, known_uidvalidity=777
        )
        assert not result.rescanned
        assert result.messages == []

    async def test_the_rescan_window_is_bounded(self):
        assert 1 <= RESCAN_DAYS <= 30, "replaying the whole mailbox is not a recovery plan"


class TestFailuresAreReportedNotRaised:
    async def test_a_rejected_login_explains_what_to_fix(self, fake_imap):
        FakeIMAP.instances = []
        result_holder = {}

        class Rejecting(FakeIMAP):
            def __init__(self, host, port):
                super().__init__(host, port)
                self.login_fails = True

        import app.services.mail.imap as module

        original = module.imaplib.IMAP4_SSL
        module.imaplib.IMAP4_SSL = Rejecting  # type: ignore[misc]
        try:
            result_holder["result"] = await poller().poll(folder="INBOX", now=NOW)
        finally:
            module.imaplib.IMAP4_SSL = original  # type: ignore[misc]

        result = result_holder["result"]
        assert not result.ok
        assert "App Password" in (result.error or ""), "names the human fix"

    async def test_missing_credentials_are_a_result_not_an_exception(self):
        class NoCredentials(FakeSettings):
            IMAP_EMAIL = ""

        result = await ImapPoller(settings=NoCredentials()).poll(folder="INBOX")  # type: ignore[arg-type]
        assert not result.ok
        assert "not configured" in (result.error or "")

    async def test_the_password_is_never_in_an_error_message(self, fake_imap):
        """Hard rule 2: never print, log or paste a secret."""

        class Rejecting(FakeIMAP):
            def __init__(self, host, port):
                super().__init__(host, port)
                self.login_fails = True

        import app.services.mail.imap as module

        original = module.imaplib.IMAP4_SSL
        module.imaplib.IMAP4_SSL = Rejecting  # type: ignore[misc]
        try:
            result = await poller().poll(folder="INBOX", now=NOW)
        finally:
            module.imaplib.IMAP4_SSL = original  # type: ignore[misc]

        assert FakeSettings.IMAP_PASSWORD not in (result.error or "")


class TestFolderNames:
    async def test_a_gmail_special_folder_is_quoted(self):
        """`[Gmail]/Sent Mail` unquoted gives `BAD Could not parse command`."""
        assert _quote_folder("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'

    async def test_an_already_quoted_name_is_left_alone(self):
        assert _quote_folder('"INBOX"') == '"INBOX"'


class TestFindByMessageId:
    async def test_it_reports_the_folder_it_was_found_in(self, fake_imap):
        """ADR-002's crash recovery, confirmed against the real provider."""
        poll = poller()

        async def run():
            return await poll.find_by_message_id(
                "ours-1@outreach.example", folders=("[Gmail]/Sent Mail",)
            )

        FakeIMAP.instances = []
        original_init = FakeIMAP.__init__

        def seeded_init(self, host, port):
            original_init(self, host, port)
            self.messages = {1: raw("ours-1@outreach.example")}

        FakeIMAP.__init__ = seeded_init  # type: ignore[method-assign]
        try:
            found = await run()
        finally:
            FakeIMAP.__init__ = original_init  # type: ignore[method-assign]

        assert found == "[Gmail]/Sent Mail"

    async def test_a_message_that_is_nowhere_returns_none(self, fake_imap):
        found = await poller().find_by_message_id("missing@outreach.example")
        assert found is None

    async def test_it_searches_readonly(self, fake_imap):
        await poller().find_by_message_id("x@outreach.example", folders=("Sent",))
        connection = fake_imap.instances[0]
        assert connection.selected_readonly is True


async def _poll_with(
    poll: ImapPoller,
    messages: dict[int, bytes],
    *,
    last_uid: int = 0,
    known_uidvalidity: int | None = None,
    limit: int = 200,
):
    """Poll with a connection pre-seeded with `messages`."""
    original_init = FakeIMAP.__init__

    def seeded_init(self, host, port):
        original_init(self, host, port)
        self.messages = dict(messages)

    FakeIMAP.instances = []
    FakeIMAP.__init__ = seeded_init  # type: ignore[method-assign]
    try:
        return await poll.poll(
            folder="INBOX",
            last_uid=last_uid,
            known_uidvalidity=known_uidvalidity,
            limit=limit,
            now=NOW,
        )
    finally:
        FakeIMAP.__init__ = original_init  # type: ignore[method-assign]
