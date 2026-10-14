r"""Polling the mailbox without touching it.

`ARCHITECTURE_TARGET.md` §10.1. Two rules, and the first is the reason this module exists:

**Nothing is ever marked read.** The prototype searched `UNSEEN` and fetched with `RFC822`, which
implicitly sets the `\Seen` flag. So a reply that arrived while its lead was not in the single
state the monitor watched was discarded *and* marked read — invisible forever. It also marked
unrelated mail in the operator's own inbox as read, which is somebody's real inbox (issue B10).

Here: `readonly=True` on SELECT, `BODY.PEEK[]` on FETCH, and progress tracked by UID rather than
by flags. We could poll the same folder a thousand times and the mailbox would look untouched.

**Matching is independent of lead state.** The poller hands every message to the matcher, so a
late reply to a lead already written off as `NO_RESPONSE` is still found (issue A6).

`imaplib` is synchronous, so calls run in a thread. That is honest about what the library is
rather than wrapping it in something that looks async and is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.settings import Settings, config
from app.services.mail.parse import ParsedMessage, parse_message

logger = logging.getLogger(__name__)

#: INBOX first, then whatever this server calls its junk folder. **Spam is not optional.** The
#: first real reply this system ever received was put in Spam by the receiving account -- a
#: consequence of sending `From: rahul@arkvane.in` through an unrelated Gmail, so the message was
#: unauthenticated -- and because the loop read only INBOX, the reply engine never saw it. The
#: dashboard showed no reply, which is indistinguishable from no reply having been sent.
#:
#: Reading a message out of Spam is safe here, and cheap. A reply is matched to a lead by
#: `In-Reply-To`/`References` against a `Message-ID` we generated, before any model is called, so
#: unrelated junk costs one row and nothing else -- already demonstrated at scale when 1,400
#: unrelated messages were ingested from a personal inbox and classified none of them.
DEFAULT_FOLDERS: tuple[str, ...] = ("INBOX",)

#: Folder names providers use for junk, tried in order when the server does not advertise
#: `\\Junk` itself. Gmail's is `[Gmail]/Spam` in an English account and translated otherwise,
#: which is exactly why the advertised attribute is tried first.
JUNK_FOLDER_GUESSES: tuple[str, ...] = (
    "[Gmail]/Spam",
    "Junk",
    "Junk E-mail",
    "Spam",
    "INBOX.Junk",
    "INBOX.spam",
)

#: RFC 6154 special-use attribute for the junk folder. A server that supports it names its own
#: folder, whatever the language, which beats guessing.
_JUNK_ATTRIBUTE = rb"\\Junk"
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.*)$')


def junk_folder_from_list(lines: list[bytes | None] | None) -> str | None:
    """This server's junk folder, from the raw `LIST` response, or None if it has none.

    Pure, so the part that actually goes wrong -- parsing one provider's reply -- is testable
    without a network or an account. The attribute is preferred over the name because Gmail
    calls the folder `[Gmail]/Spam` in an English account and translates it otherwise, so a
    hardcoded name is correct for exactly one locale.

    A name is only returned if the server listed it. Polling a folder that is not there is an
    error on every cycle for ever, which is a worse failure than not reading spam at all.
    """
    names: list[str] = []
    advertised: str | None = None
    for raw in lines or []:
        if not isinstance(raw, bytes):
            continue
        match = _LIST_LINE.match(raw.strip())
        if match is None:
            continue
        names.append(_folder_name(match.group("name")))
        if re.search(_JUNK_ATTRIBUTE, match.group("flags"), re.IGNORECASE):
            advertised = names[-1]

    if advertised:
        return advertised

    lowered = {name.lower(): name for name in names}
    for guess in JUNK_FOLDER_GUESSES:
        if guess.lower() in lowered:
            return lowered[guess.lower()]
    return None


def _folder_name(raw: bytes) -> str:
    name = raw.strip()
    if name.startswith(b'"') and name.endswith(b'"'):
        name = name[1:-1]
    return name.decode("utf-8", "replace")


#: How far back to look when UIDVALIDITY changes and every stored UID becomes meaningless.
RESCAN_DAYS = 14

#: Cap on one poll, so a long-neglected mailbox cannot produce an unbounded batch.
MAX_PER_POLL = 200


class ImapError(Exception):
    """A poll failed. Distinct from finding nothing."""


@dataclass
class FetchedMessage:
    """One message, with the IMAP coordinates needed to record where we got to."""

    uid: int
    folder: str
    raw: bytes
    parsed: ParsedMessage


@dataclass
class PollResult:
    """What one poll found, and where the cursor should now point."""

    folder: str
    messages: list[FetchedMessage] = field(default_factory=list)
    uidvalidity: int | None = None
    highest_uid: int = 0
    rescanned: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _quote_folder(folder: str) -> str:
    """Quote a folder name for IMAP.

    Gmail's special folders contain spaces and brackets — `[Gmail]/Sent Mail` — and an unquoted
    name produces `BAD Could not parse command`, which is how the first manual check failed.
    """
    if folder.startswith('"') and folder.endswith('"'):
        return folder
    return f'"{folder}"'


def _parse_uidvalidity(responses: list[bytes | None] | None) -> int | None:
    for raw in responses or []:
        match = re.search(rb"UIDVALIDITY\s+(\d+)", raw or b"")
        if match:
            return int(match.group(1))
    return None


class ImapPoller:
    """Reads new mail from one mailbox. Holds no state: the cursor lives in the database."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or config

    async def folders(self) -> tuple[str, ...]:
        """INBOX plus this server's junk folder, discovered rather than guessed at.

        An unreachable server returns just INBOX: a folder list is not worth failing a poll
        over, and the next cycle tries again.
        """
        return await asyncio.to_thread(self._folders_sync)

    def _folders_sync(self) -> tuple[str, ...]:
        settings = self._settings
        if not settings.IMAP_EMAIL or not settings.IMAP_PASSWORD:
            return DEFAULT_FOLDERS

        try:
            connection = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
        except Exception:
            return DEFAULT_FOLDERS

        try:
            try:
                connection.login(settings.IMAP_EMAIL, settings.IMAP_PASSWORD)
            except imaplib.IMAP4.error:
                return DEFAULT_FOLDERS

            status, lines = connection.list()
            if status != "OK":
                return DEFAULT_FOLDERS

            junk = junk_folder_from_list(lines)
            return ("INBOX", junk) if junk else DEFAULT_FOLDERS
        finally:
            try:
                connection.logout()
            except Exception as exc:
                logger.debug("imap logout failed after listing", extra={"error": str(exc)})

    async def poll(
        self,
        *,
        folder: str = "INBOX",
        last_uid: int = 0,
        known_uidvalidity: int | None = None,
        limit: int = MAX_PER_POLL,
        now: datetime | None = None,
    ) -> PollResult:
        """Fetch messages newer than `last_uid`, without altering the mailbox.

        Returns a result rather than raising for an expected failure: IMAP being unreachable is
        normal operation and must not kill the worker. §18 alerts after three consecutive
        failures, which needs them counted rather than thrown.
        """
        return await asyncio.to_thread(
            self._poll_sync, folder, last_uid, known_uidvalidity, limit, now
        )

    def _poll_sync(
        self,
        folder: str,
        last_uid: int,
        known_uidvalidity: int | None,
        limit: int,
        now: datetime | None,
    ) -> PollResult:
        result = PollResult(folder=folder)
        settings = self._settings

        if not settings.IMAP_EMAIL or not settings.IMAP_PASSWORD:
            result.error = "IMAP credentials are not configured"
            return result

        try:
            connection = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
        except Exception as exc:
            result.error = f"could not connect: {type(exc).__name__}: {exc}"
            return result

        try:
            try:
                connection.login(settings.IMAP_EMAIL, settings.IMAP_PASSWORD)
            except imaplib.IMAP4.error as exc:
                # Almost always a missing App Password or IMAP disabled on the account, both of
                # which a human must fix — so say which rather than retrying blindly.
                result.error = (
                    f"login rejected ({exc}). Check the App Password and that IMAP is enabled "
                    "for this mailbox."
                )
                return result

            # readonly=True is half of the no-flag-changes guarantee.
            status, responses = connection.select(_quote_folder(folder), readonly=True)
            if status != "OK":
                result.error = f"could not select {folder}: {status}"
                return result

            result.uidvalidity = _parse_uidvalidity(
                connection.response("UIDVALIDITY")[1]
            ) or _parse_uidvalidity(responses)

            search_from = last_uid
            if (
                known_uidvalidity is not None
                and result.uidvalidity is not None
                and result.uidvalidity != known_uidvalidity
            ):
                # Every stored UID is now meaningless. Rescan a bounded window and let the
                # matcher's own Message-ID deduplication absorb the overlap, rather than
                # replaying the entire mailbox.
                result.rescanned = True
                search_from = 0

            uids = self._search(connection, search_from, result.rescanned, now)
            if uids is None:
                result.error = "search failed"
                return result

            for uid in uids[:limit]:
                fetched = self._fetch_one(connection, uid, folder)
                if fetched is not None:
                    result.messages.append(fetched)
                    result.highest_uid = max(result.highest_uid, uid)

            if not result.messages:
                result.highest_uid = last_uid

            return result
        finally:
            # A failed logout tells us nothing useful and must not mask the poll's own result.
            with contextlib.suppress(Exception):
                connection.logout()

    def _search(
        self,
        connection: imaplib.IMAP4_SSL,
        from_uid: int,
        rescanning: bool,
        now: datetime | None,
    ) -> list[int] | None:
        """Find candidate UIDs.

        By UID range in the normal case, which is exact and cheap. On a rescan, by date
        instead, because there is no trustworthy UID to count from.
        """
        try:
            if rescanning:
                cutoff = (now or datetime.now(tz=UTC)) - timedelta(days=RESCAN_DAYS)
                criteria = ["SINCE", cutoff.strftime("%d-%b-%Y")]
            else:
                # UID n:* always returns at least n even when nothing is newer, so the caller
                # filters. That is the documented behaviour, not a quirk to work around.
                criteria = [f"{from_uid + 1}:*"]

            # No charset argument: the criteria are plain ASCII, and imaplib builds
            # `UID SEARCH <criteria>` from exactly what is passed.
            status, data = connection.uid("SEARCH", *criteria)
        except imaplib.IMAP4.error:
            return None

        if status != "OK" or not data or not data[0]:
            return []

        uids = sorted(int(token) for token in data[0].split())
        return [uid for uid in uids if uid > from_uid]

    def _fetch_one(
        self, connection: imaplib.IMAP4_SSL, uid: int, folder: str
    ) -> FetchedMessage | None:
        r"""Fetch one message with `BODY.PEEK[]`.

        `PEEK` is the other half of the guarantee: `BODY[]` would set `\Seen` even inside a
        readonly session on some servers, and the prototype's `RFC822` fetch did exactly that.
        """
        try:
            status, parts = connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except imaplib.IMAP4.error:
            return None

        if status != "OK" or not parts:
            return None

        for part in parts:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            raw = part[1]
            if not isinstance(raw, bytes) or not raw.strip():
                continue
            try:
                return FetchedMessage(uid=uid, folder=folder, raw=raw, parsed=parse_message(raw))
            except Exception:
                # One unparseable message must not stop the batch; the rest may be replies
                # somebody is waiting on.
                return None
        return None

    async def find_by_message_id(
        self, message_id: str, *, folders: tuple[str, ...] = ("[Gmail]/Sent Mail", "INBOX")
    ) -> str | None:
        """Look for a specific `Message-ID`, returning the folder it was found in.

        This is ADR-002's crash recovery: a row stuck in `sending` is resolved by asking the
        mailbox whether the message actually left. Confirmed working against Google Workspace,
        which copies SMTP-sent mail into Sent.
        """
        return await asyncio.to_thread(self._find_sync, message_id, folders)

    def _find_sync(self, message_id: str, folders: tuple[str, ...]) -> str | None:
        settings = self._settings
        if not settings.IMAP_EMAIL or not settings.IMAP_PASSWORD or not message_id:
            return None

        try:
            connection = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
        except Exception:
            return None

        try:
            connection.login(settings.IMAP_EMAIL, settings.IMAP_PASSWORD)
            for folder in folders:
                try:
                    status, _ = connection.select(_quote_folder(folder), readonly=True)
                    if status != "OK":
                        continue
                    # The header value is quoted: Message-IDs contain characters the IMAP
                    # command parser would otherwise choke on.
                    status, data = connection.search(
                        None, "HEADER", "Message-ID", f'"{message_id}"'
                    )
                except imaplib.IMAP4.error:
                    continue
                if status == "OK" and data and data[0].split():
                    return folder
            return None
        except Exception:
            return None
        finally:
            # A failed logout tells us nothing useful and must not mask the poll's own result.
            with contextlib.suppress(Exception):
                connection.logout()
