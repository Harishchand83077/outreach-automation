"""Resolving a send that was interrupted, by asking the mailbox what happened.

ADR-002. A row left in `sending` is the one genuinely ambiguous state in the outbox: the SMTP
call was made, and the worker died before recording the result. We cannot know from our own data
whether the mail left.

What we *can* do is look. The `Message-ID` is generated before the SMTP call and stored, so the
mailbox itself can be asked: if that identifier is in Sent, the message left; if it is nowhere
after enough time, it did not. This was confirmed against the real provider — a pre-generated
`Message-ID` was found in `[Gmail]/Sent Mail` by `BODY.PEEK` search — which is what closed the
`[VERIFY]` the target architecture left on this path.

Two rules hold regardless of what the search finds:

- **Nothing is ever re-sent automatically.** A positive find marks the row `sent`; anything else
  marks it `unknown` and asks a person. At-most-once is the guarantee, and a duplicate cold
  email is worse than a missing one.
- **`BCC_SELF` is not required.** The spec's original recovery depended on a blind copy landing
  in INBOX. Searching Sent directly works without it, so a mailbox that does not self-copy
  still recovers (ADR-002).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import outbox
from app.db.models import Campaign, Draft, Lead, Message
from app.domain.campaign import CampaignSettings
from app.domain.scheduling import followup_due_at
from app.services.mail.imap import ImapPoller

#: Folders to search, in the order that answers fastest. Sent is where a delivered message
#: belongs; INBOX catches a `BCC_SELF` copy for mailboxes that do use one.
SENT_FOLDERS: tuple[str, ...] = ("[Gmail]/Sent Mail", "Sent", "INBOX")

#: How long a `sending` row waits before recovery touches it. Long enough that an in-flight
#: send on another worker is not mistaken for a crashed one.
GRACE = timedelta(minutes=15)


@dataclass
class Resolution:
    """What was decided about one stuck row."""

    message_id: uuid.UUID
    outcome: str
    detail: str = ""


@dataclass
class RecoveryReport:
    checked: int = 0
    resolutions: list[Resolution] = field(default_factory=list)

    @property
    def confirmed_sent(self) -> int:
        return sum(1 for r in self.resolutions if r.outcome == "sent")

    @property
    def needs_human(self) -> int:
        return sum(1 for r in self.resolutions if r.outcome == "unknown")


async def recover_stuck_sends(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    poller: ImapPoller,
    org_id: uuid.UUID | None = None,
    folders: tuple[str, ...] = SENT_FOLDERS,
    grace: timedelta = GRACE,
    now: datetime | None = None,
) -> RecoveryReport:
    """Resolve every `sending` row older than the grace period.

    Each row is resolved in its own transaction, so one unresolvable message does not block the
    rest, and a crash mid-pass leaves the remainder exactly as it found them.
    """
    now = now or datetime.now(tz=UTC)
    report = RecoveryReport()

    async with session_factory() as session, session.begin():
        stuck = await outbox.find_stuck_sending(session, org_id=org_id, older_than=grace, now=now)
        # Read the identifiers out now: the rows belong to this session, and each resolution
        # opens its own.
        candidates = [(row.id, row.org_id, row.message_id_hdr or "") for row in stuck]

    report.checked = len(candidates)

    for message_id, row_org_id, header in candidates:
        if not header:
            # No identifier was stored, so the mailbox cannot be asked. A person has to decide.
            await _mark_unknown(
                session_factory,
                org_id=row_org_id,
                message_id=message_id,
                note="no Message-ID was stored, so delivery cannot be confirmed from the mailbox",
                now=now,
            )
            report.resolutions.append(Resolution(message_id, "unknown", "no Message-ID stored"))
            continue

        found_in = await poller.find_by_message_id(header, folders=folders)

        if found_in is None:
            await _mark_unknown(
                session_factory,
                org_id=row_org_id,
                message_id=message_id,
                note=(
                    f"Message-ID {header} was not found in {', '.join(folders)}; it probably "
                    "never left, but confirm before sending again"
                ),
                now=now,
            )
            report.resolutions.append(Resolution(message_id, "unknown", "not found in the mailbox"))
            continue

        async with session_factory() as session, session.begin():
            await _mark_sent(
                session,
                org_id=row_org_id,
                message_id=message_id,
                found_in=found_in,
                now=now,
            )
        report.resolutions.append(Resolution(message_id, "sent", f"found in {found_in}"))

    return report


async def _mark_unknown(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    note: str,
    now: datetime,
) -> None:
    async with session_factory() as session, session.begin():
        await outbox.mark_unknown(session, org_id=org_id, message_id=message_id, note=note, now=now)


async def _mark_sent(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    found_in: str,
    now: datetime,
) -> None:
    """Record a recovered send exactly as the send loop would have.

    Two details that are easy to miss and both cause a duplicate email if they are:

    - **The follow-up is scheduled from when the message was actually sent**, not from now. A
      row recovered two hours later would otherwise push its follow-up two hours out, and a row
      recovered the next morning would push it a day.
    - **The draft is marked `sent`.** The send loop does this, and a draft left `approved` is
      still eligible for sending — so skipping it here would turn a recovered send into a
      second one, which is the exact thing at-most-once exists to prevent.
    """
    message = await session.get(Message, message_id)
    if message is None or message.org_id != org_id:
        raise LookupError(f"message {message_id} not found in this org")

    follow_at: datetime | None = None
    if message.kind == outbox.KIND_INITIAL and message.lead_id is not None:
        lead = await session.get(Lead, message.lead_id)
        campaign = await session.get(Campaign, lead.campaign_id) if lead is not None else None
        if campaign is not None:
            settings = CampaignSettings.model_validate(campaign.settings or {})
            if settings.max_followups > 0:
                follow_at = followup_due_at(first_sent_at=message.created_at, settings=settings)

    await outbox.mark_sent(
        session,
        org_id=org_id,
        message_id=message_id,
        smtp_response=(f"recovered: found in {found_in} by Message-ID after an interrupted send"),
        next_action_at=follow_at,
        now=now,
    )

    if message.draft_id is not None:
        draft = await session.get(Draft, message.draft_id)
        if draft is not None and draft.org_id == org_id:
            draft.status = "sent"
