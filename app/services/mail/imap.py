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
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.settings import Settings, config
from app.services.mail.parse import ParsedMessage, parse_message

#: INBOX first. Spam is worth polling because genuine replies land there more often than anyone
#: would like, and a reply we never see looks identical to no reply at all.
DEFAULT_FOLDERS: tuple[str, ...] = ("INBOX",)

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
