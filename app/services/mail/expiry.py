"""When a Gmail connection lapses, and saying so before it does.

ADR-018 left the OAuth client **unpublished**, deliberately: Google calls reading a mailbox a
*restricted* scope, publishing one needs an annual paid security assessment, and hard rule 5 says
no. The price is written down there rather than discovered: an unpublished client's refresh tokens
expire **seven days** after the person consented, whatever they do in between. Refreshing an
access token does not extend it.

So a connection lapsing weekly is not a fault. It is a product state with a known fix, and the
only thing that makes it unacceptable is finding out afterwards — a campaign that stopped on
Friday, a reply nobody fetched over the weekend, and a dashboard that said nothing until somebody
opened it on Monday.

**Two halves, and only the second one talks to anything.** The arithmetic is pure functions, so
"how long is left" is testable against a clock and nothing else; `warn_about_expiring_grants` is
the sweep that turns it into an alert. The dashboard reads the same functions, so the sentence a
person sees and the moment an alert fires cannot drift apart — which is the defect this project
keeps producing when a rule gets a second implementation.

**Nothing here can un-pause or re-send anything.** The worst it does is write an alert row.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings, config
from app.db.models import Mailbox, MailCredential
from app.db.state import record_event
from app.services.alerts import Notifier, raise_alert

logger = logging.getLogger(__name__)

#: Days remaining at which somebody is told, largest first. `1` is the day before it lapses and
#: `0` is the day it does — the two the owner asked for. A third, earlier warning was considered
#: and left out: with a seven-day life, a notice on day three is noise by day six.
WARN_AT_DAYS: tuple[int, ...] = (1, 0)

#: One warning per threshold per day. `raise_alert`'s default hour would repeat the same sentence
#: on every hourly sweep, and an alert that arrives twenty times is an alert people filter.
WARN_COOLDOWN = timedelta(hours=20)


@dataclass(frozen=True)
class GrantStatus:
    """What can be said about one mailbox's connection without asking Google."""

    #: None when this credential does not expire: an app password, or a published OAuth client.
    expires_at: datetime | None
    #: Whole days remaining, rounded **up**, so "0 days left" means today and nothing else.
    #: None when there is no expiry.
    days_left: int | None

    @property
    def expires(self) -> bool:
        return self.expires_at is not None

    @property
    def expired(self) -> bool:
        return self.days_left is not None and self.days_left <= 0

    @property
    def notice(self) -> str | None:
        """The sentence the dashboard shows, or None when there is nothing to say.

        Only ever about reconnecting. It never implies the mailbox is broken: until the moment it
        lapses, everything works, and copy that cries wolf for six days is copy nobody reads on
        the seventh.
        """
        if self.days_left is None:
            return None
        if self.days_left <= 0:
            return (
                "This Gmail connection has expired. Reconnect it to start sending and "
                "reading replies again."
            )
        if self.days_left > max(WARN_AT_DAYS):
            return None
        day = "day" if self.days_left == 1 else "days"
        return (
            f"This Gmail connection expires in {self.days_left} {day}. "
            "Reconnect it before then and nothing stops."
        )


def grant_status(
    credential: MailCredential | None,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> GrantStatus:
    """How much of this grant is left.

    `granted_at` is the only honest input. `created_at` stops moving once the row exists, so a
    mailbox reconnected this morning would look as stale as it did yesterday; `last_refreshed_at`
    moves every hour as access tokens are minted, so it describes a token that was never the one
    that expires. It falls back to `created_at` only for rows written before that column existed.
    """
    settings = settings or config
    now = now or datetime.now(tz=UTC)

    if credential is None or credential.kind != "google_oauth":
        return GrantStatus(expires_at=None, days_left=None)

    days = settings.GOOGLE_GRANT_DAYS
    if days <= 0:
        # A published client. The warnings stop with a configuration change and no code change.
        return GrantStatus(expires_at=None, days_left=None)

    # `created_at` is NOT NULL, so this is never None in practice -- the fallback is for rows
    # written before `granted_at` existed, and mypy already knows the column cannot be null.
    granted = credential.granted_at or credential.created_at
    if granted.tzinfo is None:
        granted = granted.replace(tzinfo=UTC)

    expires_at = granted + timedelta(days=days)
    remaining = expires_at - now
    # Rounded **up**, so a connection with eleven hours left reports 1 rather than 0. Reporting
    # 0 for something that still works is the error that makes a countdown untrustworthy.
    left = max(0, math.ceil(remaining / timedelta(days=1)))
    return GrantStatus(expires_at=expires_at, days_left=left)


async def warn_about_expiring_grants(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    notifier: Notifier | None = None,
) -> list[str]:
    """Raise one alert per mailbox whose connection is about to lapse. Returns the kinds raised.

    Idempotent within the cool-down, so the hourly sweep that calls this produces one warning a
    day rather than twenty. The caller commits.

    A credential already carrying an `invalid_reason` is skipped: the send loop found out the
    hard way and the dashboard is already showing Reconnect, so a second message saying the same
    thing adds nothing but noise.
    """
    settings = settings or config
    now = now or datetime.now(tz=UTC)

    rows = (
        await session.execute(
            select(MailCredential, Mailbox)
            .join(Mailbox, Mailbox.id == MailCredential.mailbox_id)
            .where(MailCredential.kind == "google_oauth")
        )
    ).all()

    raised: list[str] = []
    for credential, mailbox in rows:
        if credential.invalid_reason:
            continue
        status = grant_status(credential, now=now, settings=settings)
        if status.days_left is None or status.days_left > max(WARN_AT_DAYS):
            continue
        threshold = min(day for day in WARN_AT_DAYS if status.days_left <= day)

        # The mailbox id is in the kind, not only in the detail, because the cool-down dedupes
        # by kind: without it the first mailbox warned would silence every other mailbox in the
        # deployment for twenty hours.
        kind = f"mailbox_grant_expiring:{mailbox.id}:{threshold}"
        alert = await raise_alert(
            session,
            org_id=mailbox.org_id,
            kind=kind,
            message=(
                f"{mailbox.from_email}: the Gmail connection "
                + ("has expired" if status.expired else f"expires in {status.days_left} day(s)")
                + ". Reconnect it from the campaign screen."
            ),
            severity="critical" if status.expired else "warning",
            detail={
                "mailbox_id": str(mailbox.id),
                "days_left": status.days_left,
                "expires_at": status.expires_at.isoformat() if status.expires_at else None,
            },
            cooldown=WARN_COOLDOWN,
            now=now,
            notifier=notifier,
        )
        if alert is None:
            continue

        raised.append(kind)
        # An event as well as an alert: alerts are acknowledged and age out, and "when were we
        # told?" is a question the lead timeline already answers for everything else.
        await record_event(
            session,
            org_id=mailbox.org_id,
            type="mailbox.grant_expiring",
            actor="system",
            data={"mailbox_id": str(mailbox.id), "days_left": status.days_left},
        )

    return raised


async def status_for_mailbox(
    session: AsyncSession, *, mailbox_id: uuid.UUID, settings: Settings | None = None
) -> GrantStatus:
    """`grant_status` for a mailbox the caller has already proved belongs to its org."""
    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox_id)
    )
    return grant_status(credential, settings=settings)
