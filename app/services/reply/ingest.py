"""The ingest loop: poll a folder, process each message, then move the cursor.

`ARCHITECTURE_TARGET.md` §10.1 and §10.2. The ordering here is the whole design, and it is
chosen so that every failure mode is a *repeat*, never a *loss*:

1. Poll above the stored UID, touching no flags.
2. For each message, in UID order: classify, then `handle_inbound`, then **commit**.
3. Advance the cursor to that UID, in its own commit.

If the process dies between 2 and 3, the message is fetched again on the next poll and
`already_stored` recognises it by `Message-ID` and does nothing. The reverse order — cursor
first — would skip a message on a crash, and a skipped message is a reply nobody ever sees.
That asymmetry is why the cursor trails the work rather than leading it.

One message per transaction, not one batch: a single message that cannot be processed must not
roll back the twenty before it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ImapCursor
from app.domain.campaign import CampaignSettings
from app.services.mail.connection import MailLogin
from app.services.mail.imap import FetchedMessage, ImapPoller, PollResult
from app.services.reply.classify import Router, classify_reply
from app.services.reply.service import ReplyOutcome, handle_inbound

#: Consecutive failures before §18 says a human should be told. Mail being briefly unreachable
#: is normal; three in a row is not.
ALERT_AFTER_FAILURES = 3


@dataclass
class IngestReport:
    """What one pass over one folder did."""

    folder: str
    polled: int = 0
    processed: list[ReplyOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    last_uid: int = 0
    rescanned: bool = False
    poll_error: str | None = None
    should_alert: bool = False

    @property
    def duplicates(self) -> int:
        return sum(1 for outcome in self.processed if outcome.duplicate)

    @property
    def orphans(self) -> int:
        return sum(1 for outcome in self.processed if outcome.orphan)


async def get_or_create_cursor(
    session: AsyncSession, *, org_id: uuid.UUID, mailbox_id: uuid.UUID, folder: str
) -> ImapCursor:
    cursor = await session.scalar(
        select(ImapCursor).where(
            ImapCursor.org_id == org_id,
            ImapCursor.mailbox_id == mailbox_id,
            ImapCursor.folder == folder,
        )
    )
    if cursor is None:
        cursor = ImapCursor(org_id=org_id, mailbox_id=mailbox_id, folder=folder, last_uid=0)
        session.add(cursor)
        await session.flush()
    return cursor


async def ingest_folder(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    poller: ImapPoller,
    router: Router,
    org_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    folder: str = "INBOX",
    settings: CampaignSettings,
    limit: int = 200,
    now: datetime | None = None,
    login: MailLogin | None = None,
) -> IngestReport:
    """Run one pass. Returns a report; raises only on a programming error.

    Takes a session *factory* rather than a session because each message needs its own
    transaction, and because the cursor update must commit separately from the work it records.
    """
    now = now or datetime.now(tz=UTC)
    report = IngestReport(folder=folder)

    async with session_factory() as session, session.begin():
        cursor = await get_or_create_cursor(
            session, org_id=org_id, mailbox_id=mailbox_id, folder=folder
        )
        last_uid = int(cursor.last_uid or 0)
        known_uidvalidity = cursor.uidvalidity

    result = await poller.poll(
        folder=folder,
        last_uid=last_uid,
        known_uidvalidity=known_uidvalidity,
        limit=limit,
        now=now,
        # Whose mailbox this is. `None` means the environment account, which only the org
        # entitled to it ever reaches here with.
        login=login,
    )

    if not result.ok:
        report.poll_error = result.error
        report.should_alert = await _record_failure(
            session_factory,
            org_id=org_id,
            mailbox_id=mailbox_id,
            folder=folder,
            error=result.error or "unknown error",
            now=now,
        )
        return report

    report.polled = len(result.messages)
    report.rescanned = result.rescanned
    report.last_uid = last_uid

    for fetched in sorted(result.messages, key=lambda m: m.uid):
        try:
            outcome = await _process_one(
                session_factory,
                router=router,
                org_id=org_id,
                fetched=fetched,
                settings=settings,
                now=now,
            )
        except Exception as exc:  # one bad message must not stop the batch
            # The cursor stops here on purpose. Advancing past a message we could not process
            # would lose it permanently; stalling means a human sees the same error next poll,
            # which is noisy and recoverable.
            report.errors.append(f"uid {fetched.uid}: {type(exc).__name__}: {exc}")
            break

        report.processed.append(outcome)
        report.last_uid = fetched.uid
        await _advance_cursor(
            session_factory,
            org_id=org_id,
            mailbox_id=mailbox_id,
            folder=folder,
            uid=fetched.uid,
            uidvalidity=result.uidvalidity,
            now=now,
        )

    if not result.messages:
        # Nothing new, but a UIDVALIDITY change still has to be recorded, or every poll
        # rescans two weeks of mail for ever.
        await _advance_cursor(
            session_factory,
            org_id=org_id,
            mailbox_id=mailbox_id,
            folder=folder,
            uid=max(last_uid, result.highest_uid),
            uidvalidity=result.uidvalidity,
            now=now,
        )

    return report


async def _process_one(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    router: Router,
    org_id: uuid.UUID,
    fetched: FetchedMessage,
    settings: CampaignSettings,
    now: datetime,
) -> ReplyOutcome:
    """Classify and handle one message, in its own transaction.

    Classification happens outside the transaction that writes, because an LLM call can take
    seconds and holding a row lock across it would block the sender for no reason.
    """
    classified = await classify_reply(router, fetched.parsed)

    async with session_factory() as session, session.begin():
        return await handle_inbound(
            session,
            org_id=org_id,
            parsed=fetched.parsed,
            folder=fetched.folder,
            uid=fetched.uid,
            settings=settings,
            model_label=classified.label,
            confidence=classified.confidence,
            llm_entry=classified.llm_entry,
            prompt_version=classified.prompt_version,
            now=now,
        )


async def _advance_cursor(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    folder: str,
    uid: int,
    uidvalidity: int | None,
    now: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        cursor = await get_or_create_cursor(
            session, org_id=org_id, mailbox_id=mailbox_id, folder=folder
        )
        # Never move backwards: a rescan replays old UIDs, and letting the cursor regress would
        # make the next poll replay them again.
        cursor.last_uid = max(int(cursor.last_uid or 0), uid)
        if uidvalidity is not None:
            cursor.uidvalidity = uidvalidity
        cursor.last_polled_at = now
        cursor.consecutive_failures = 0
        cursor.last_error = None


async def _record_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    mailbox_id: uuid.UUID,
    folder: str,
    error: str,
    now: datetime,
) -> bool:
    """Count a failed poll. Returns True when it is time to tell somebody."""
    async with session_factory() as session, session.begin():
        cursor = await get_or_create_cursor(
            session, org_id=org_id, mailbox_id=mailbox_id, folder=folder
        )
        cursor.consecutive_failures = int(cursor.consecutive_failures or 0) + 1
        cursor.last_error = error[:1000]
        cursor.last_polled_at = now
        return cursor.consecutive_failures >= ALERT_AFTER_FAILURES


def poll_summary(result: PollResult) -> str:
    """One line for the log. No addresses and no subjects: logs are not a mail archive."""
    if not result.ok:
        return f"imap {result.folder}: failed ({result.error})"
    return f"imap {result.folder}: {len(result.messages)} new, uid<={result.highest_uid}" + (
        " (rescan after UIDVALIDITY change)" if result.rescanned else ""
    )
