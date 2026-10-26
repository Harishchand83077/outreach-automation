"""Which credentials a mailbox sends with.

One sending account came from the environment. Ten people need their own, so this resolves a
mailbox to the credentials it should authenticate with, **falling back to the environment only
for the org that owns it** (`orgs.uses_environment_mailbox`, and only for the address
`SMTP_EMAIL` actually logs in as).

The fallback is not politeness. It is what keeps the live deployment sending while all of this
lands: the operator's own mailbox behaves exactly as every mailbox did before this module
existed. What it is *not* is a default for strangers — an unconnected mailbox in a workspace
somebody signed up for would otherwise send through the operator's Gmail, on the operator's
quota and reputation, and nothing afterwards would say it had.

**The send gate does not move.** `effective_send_mode()` and `allowlist_permits()` stay in
`smtp.py` and go on deciding for every send, whoever it belongs to — hard rule 1, and the
structural test over the whole tree still asserts that nothing else decides. This module answers
a different question: not *may* we send, but *as whom*.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings, config
from app.db.models import Mailbox, MailCredential, Org
from app.services.credentials import CredentialError, seal, unseal
from app.services.mail.google_oauth import (
    REFRESH_MARGIN,
    OAuthError,
    refresh_access_token,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailLogin:
    """How to authenticate as one mailbox."""

    username: str
    #: Exactly one of these is set.
    password: str | None = None
    access_token: str | None = None

    @property
    def uses_oauth(self) -> bool:
        return self.access_token is not None

    def __repr__(self) -> str:  # pragma: no cover - defensive
        kind = "oauth" if self.uses_oauth else "password"
        return f"MailLogin(username={self.username!r}, {kind})"


class MailboxUnavailable(Exception):
    """This mailbox cannot authenticate, and a person has to do something about it.

    Raised for a credential that cannot be decrypted or refreshed — which with an OAuth client
    in Testing status is an ordinary weekly event, not an emergency. The caller pauses the
    mailbox with this message and the dashboard turns it into a Reconnect button.
    """


async def login_for(
    session: AsyncSession,
    *,
    mailbox: Mailbox,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> MailLogin:
    """Resolve how this mailbox authenticates, refreshing an OAuth token if it is due.

    May write: a refreshed access token is stored so that the next send does not mint another.
    The caller commits — this is called from inside the send loop's own transaction, and opening
    a second one here to save a token would be a write that outlives a send that failed.
    """
    settings = settings or config
    now = now or datetime.now(tz=UTC)

    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
    )
    if credential is None:
        return await _environment_login(session, mailbox, settings)

    if credential.invalid_reason:
        raise MailboxUnavailable(credential.invalid_reason)

    if credential.kind == "app_password":
        try:
            return MailLogin(
                username=credential.account_email,
                password=unseal(credential.secret, settings),
            )
        except CredentialError as exc:
            raise MailboxUnavailable(str(exc)) from exc

    return await _google_login(session, credential, settings, now)


async def unavailable_reason(
    session: AsyncSession, *, mailbox: Mailbox, settings: Settings | None = None
) -> str | None:
    """Why this mailbox cannot authenticate, or None. **Never touches the network.**

    The dashboard needs this to say "connect this mailbox" instead of showing a running campaign
    with approved drafts and no explanation. It cannot get it by calling `login_for`: that mints
    an access token, so a `GET` of the campaign screen would make a request to Google and could
    write an `invalid_reason` — a read path with a side effect, fired by anybody refreshing a
    page.

    So the two knowable cases are answered here and `login_for` asks the same helper for the
    first of them, rather than a second copy of the rule that would drift from it. The third
    case — a refresh Google will not honour — is unknowable without asking, and surfaces through
    `invalid_reason` the moment the send loop finds out.
    """
    settings = settings or config
    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
    )
    if credential is not None:
        return credential.invalid_reason
    if await _environment_entitles(session, mailbox, settings):
        return None
    return (
        f"{mailbox.from_email} has not been connected yet. "
        "Connect this Gmail account from the campaign screen before sending."
    )


async def _environment_entitles(
    session: AsyncSession, mailbox: Mailbox, settings: Settings
) -> bool:
    """May this mailbox authenticate with the credentials in `.env`?

    **Two conditions, and each closes a different hole.**

    `orgs.uses_environment_mailbox` says whose they are. Without it any stranger's unconnected
    mailbox authenticates as the deployment's own account: their campaign spends the operator's
    daily cap, carries the operator's sending reputation, and arrives signed by SPF and DKIM
    records the operator published — mail the operator never wrote, from an address they have
    never seen, and nothing afterwards to say it happened. Matching on the address alone would
    not do, because `uq_mailboxes_org_from_email` is scoped per org: a second workspace may
    create a mailbox named after the operator's address, and the guard would become the attack.

    The address must *also* match `SMTP_EMAIL`, because an owner may hold several mailboxes and
    only one of them is the account these credentials log in as. Sending `rahul@arkvane.in`
    through a Gmail authenticated as somebody else is the mismatched-envelope problem already on
    record: Gmail accepts it, nothing bounces, and the message is simply unauthenticated against
    a domain whose SPF and DKIM say otherwise — at volume a suspended account, not a spam folder.
    """
    configured = (settings.SMTP_EMAIL or "").strip().lower()
    if not configured or mailbox.from_email.lower() != configured:
        return False
    org = await session.get(Org, mailbox.org_id)
    return bool(org and org.uses_environment_mailbox)


async def _environment_login(
    session: AsyncSession, mailbox: Mailbox, settings: Settings
) -> MailLogin:
    """The credentials in `.env`, for the one org entitled to them.

    **Two conditions, and each closes a different hole.**

    `orgs.uses_environment_mailbox` says whose they are. Without it any stranger's unconnected
    mailbox authenticates as the deployment's own account: their campaign spends the operator's
    daily cap, carries the operator's sending reputation, and arrives signed by SPF and DKIM
    records the operator published — mail the operator never wrote, from an address they have
    never seen, and no way to tell afterwards that it happened.

    The address must also match `SMTP_EMAIL`, because an owner may hold several mailboxes and
    only one of them is the account these credentials log in as. Sending `rahul@arkvane.in`
    through a Gmail authenticated as somebody else is the mismatched-envelope problem already on
    record: Gmail accepts it, nothing bounces, and the message is simply unauthenticated — which
    at volume is a suspended account rather than a spam folder.

    The entitlement rule itself lives in `_environment_entitles`, because the dashboard has to
    ask the same question without sending anything.

    Refusing is a `MailboxUnavailable`, so the send loop pauses that one mailbox with a reason
    the dashboard turns into a Connect button, and every other mailbox keeps its turn.
    """
    if await _environment_entitles(session, mailbox, settings):
        # A single-tenant deployment never has to know any of this exists.
        return MailLogin(username=settings.SMTP_EMAIL, password=settings.SMTP_PASSWORD)

    reason = await unavailable_reason(session, mailbox=mailbox, settings=settings)
    raise MailboxUnavailable(reason or f"{mailbox.from_email} cannot authenticate.")


async def _google_login(
    session: AsyncSession,
    credential: MailCredential,
    settings: Settings,
    now: datetime,
) -> MailLogin:
    """An access token for this mailbox, minted from the stored refresh token when due.

    **Stored tokens are the refresh token, not the access token.** An access token lives an hour
    and is worthless to keep; the refresh token is the thing worth encrypting. The short-lived
    one is minted on demand and cached in the row only so that a burst of sends does not make a
    round trip to Google per message.
    """
    try:
        refresh_token = unseal(credential.secret, settings)
    except CredentialError as exc:
        raise MailboxUnavailable(str(exc)) from exc

    cached = credential.scopes and credential.expires_at
    # `access:` prefixed values live in `last_access`, below. Kept on the row rather than in
    # process memory because the api and the worker are separate processes and both send.
    if cached and credential.expires_at and credential.expires_at - REFRESH_MARGIN > now:
        access = _cached_access(credential, settings)
        if access is not None:
            return MailLogin(username=credential.account_email, access_token=access)

    try:
        tokens = await refresh_access_token(refresh_token, settings)
    except OAuthError as exc:
        # **The expected failure, not an exceptional one.** A Testing-status OAuth client expires
        # refresh tokens after 7 days, so this happens weekly by design. It is recorded on the
        # row so the dashboard can ask for a reconnect instead of the worker retrying a token
        # that will never work again.
        credential.invalid_reason = (
            f"Google would not renew this connection ({exc}). Reconnect the mailbox."
        )
        await session.flush()
        raise MailboxUnavailable(credential.invalid_reason) from exc

    credential.expires_at = tokens.expires_at
    credential.last_refreshed_at = now
    credential.scopes = tokens.scopes
    if tokens.refresh_token and tokens.refresh_token != refresh_token:
        # Google rotated it. Storing the new one matters: the old one may stop working, and
        # a stored token that no longer refreshes is a mailbox that dies silently in a week.
        credential.secret = seal(tokens.refresh_token, settings).token
    _cache_access(credential, tokens.access_token, settings)
    await session.flush()

    return MailLogin(username=credential.account_email, access_token=tokens.access_token)


#: The cached access token rides in `scopes` after a separator, so that caching one needed no
#: extra column. That is a compromise and it is written down rather than hidden: a column would
#: be cleaner, and this is a migration that can happen whenever something else touches the table.
_SEP = "\n#access#\n"


def _cache_access(credential: MailCredential, access: str, settings: Settings) -> None:
    scopes = (credential.scopes or "").split(_SEP)[0]
    try:
        credential.scopes = scopes + _SEP + seal(access, settings).token.decode("ascii")
    except CredentialError:
        # Caching is an optimisation. Failing to cache must never fail a send.
        credential.scopes = scopes


def _cached_access(credential: MailCredential, settings: Settings) -> str | None:
    parts = (credential.scopes or "").split(_SEP)
    if len(parts) != 2:
        return None
    try:
        return unseal(parts[1].encode("ascii"), settings)
    except (CredentialError, UnicodeEncodeError):
        return None


async def mark_unavailable(
    session: AsyncSession, *, mailbox: Mailbox, reason: str, now: datetime | None = None
) -> None:
    """Pause a mailbox whose credentials stopped working, with a reason a person can act on.

    Pausing rather than deleting: the credential row holds what the mailbox was connected as and
    when, and throwing that away would make "what happened last Tuesday?" unanswerable. Reads of
    the mailbox still work, which matters — pausing stops sending, and an opt-out we refused to
    read because we had stopped sending is the one we would breach on starting again.
    """
    mailbox.paused = True
    mailbox.pause_reason = reason
    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
    )
    if credential is not None and not credential.invalid_reason:
        credential.invalid_reason = reason
    await session.flush()
    logger.warning("mailbox paused", extra={"mailbox_id": str(mailbox.id), "reason": reason})


async def connection_status(session: AsyncSession, *, mailbox_id: uuid.UUID) -> dict[str, object]:
    """What the dashboard may say about a mailbox's connection. Never a secret."""
    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox_id)
    )
    if credential is None:
        return {"connected": False, "kind": None, "account_email": None, "needs_reconnect": False}
    return {
        "connected": credential.invalid_reason is None,
        "kind": credential.kind,
        "account_email": credential.account_email,
        "needs_reconnect": credential.invalid_reason is not None,
        "reason": credential.invalid_reason,
        "last_refreshed_at": (
            credential.last_refreshed_at.isoformat() if credential.last_refreshed_at else None
        ),
        "connected_at": credential.created_at.isoformat(),
    }
