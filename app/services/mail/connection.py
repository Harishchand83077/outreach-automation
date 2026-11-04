"""Which credentials a mailbox sends with.

**Every mailbox carries its own, and there is no deployment-wide account any more.**

For most of this project there was one: `SMTP_EMAIL` and `SMTP_PASSWORD` in the environment,
which is correct for one person and impossible for ten. ADR-020 narrowed the fallback to the one
org entitled to it; ADR-023 removes it. A mailbox with no credential cannot send and cannot be
read, whoever it belongs to, including the owner's.

That is a deliberate and visible loss of convenience, and it buys the property the product needs:
**there is no identity here that is not somebody's own.** Nobody can send on an account they did
not personally authorise, no configuration mistake can make a stranger's campaign go out under
the operator's address, and the question "whose Gmail sent this?" has exactly one answer -- the
one named on the credential row.

A mailbox nobody has connected is not an error. It is a product state with a one-click fix, and
`unavailable_reason()` is the sentence the dashboard shows for it.

**The send gate does not move.** `effective_send_mode()` and `allowlist_permits()` stay in
`smtp.py` and go on deciding for every send, whoever it belongs to -- hard rule 1, and the
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
from app.db.models import Mailbox, MailCredential
from app.services.credentials import CredentialError, seal, unseal
from app.services.mail.expiry import grant_status
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
        raise MailboxUnavailable(_not_connected(mailbox))

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

    So the knowable cases are answered here and `login_for` asks the same helper, rather than a
    second copy of the rule that would drift from it. The unknowable case — a refresh Google
    will not honour for some other reason — surfaces through `invalid_reason` the moment the
    send loop finds out.

    **An expired grant is one of the knowable cases, and it was missing.** `invalid_reason` is
    set only once a provider has *refused* a credential, so a connection that lapsed on Friday
    and has not been used since carries no reason at all — and this function returned None,
    which every reader takes to mean "this mailbox works". The campaign screen, the sending
    account panel and `GET /mailboxes` all said **connected** about a mailbox that could
    neither send nor read, while the alert sweep was raising `mailbox_grant_expiring` about the
    same row every hour. Two correct functions answering adjacent questions, with the screens
    asking the wrong one: this project's signature defect, and the same shape as the pool
    blocker that reported nothing for a *paused* mailbox because `unavailable_reason` answers
    whether a credential can log in.

    Reported from the arithmetic rather than from Google, deliberately. `login_for` still asks
    Google, because Google is the authority on whether a token works — but "should this person
    be told to reconnect?" is a question we can answer without a network call, and the honest
    answer the moment a seven-day grant is seven days old is yes.
    """
    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
    )
    if credential is None:
        return _not_connected(mailbox)
    if credential.invalid_reason:
        return credential.invalid_reason
    grant = grant_status(credential, settings=settings)
    if grant.expired:
        # `notice` is the same sentence the dashboard and the alert already use, so a person
        # does not see three wordings for one state and assume three problems.
        return grant.notice
    return None


def _not_connected(mailbox: Mailbox) -> str:
    """The sentence shown for a mailbox nobody has authorised yet.

    One function, because the campaign blocker, the mailbox list and the send loop's pause reason
    must all say the same thing. A person who sees three different wordings for one state assumes
    they are three different problems.
    """
    return (
        f"{mailbox.from_email} has not been connected yet. "
        "Connect this Google account from the campaign screen before sending."
    )


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


async def connection_status(
    session: AsyncSession, *, mailbox_id: uuid.UUID, settings: Settings | None = None
) -> dict[str, object]:
    """What the dashboard may say about a mailbox's connection. Never a secret.

    **`connected` and `needs_reconnect` are two views of one answer**, and they are computed
    from one so they cannot contradict each other. They used to be computed from
    `invalid_reason` alone, which meant a lapsed grant reported `connected: true` beside a
    `days_left: 0` and an `expiry_notice` saying it had expired -- one row disagreeing with
    itself in three fields.

    **`invalid_reason` is no longer returned.** It can quote a provider's own reply, and this
    response reaches a browser, a screenshot and anything that logs a page. That rule is kept
    everywhere else in this system -- `GET /mailboxes` carries a boolean instead and has a test
    asserting the stored text is absent -- and this route was the one place still echoing it.
    """
    credential = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox_id)
    )
    if credential is None:
        return {"connected": False, "kind": None, "account_email": None, "needs_reconnect": False}

    grant = grant_status(credential, settings=settings)
    works = credential.invalid_reason is None and not grant.expired
    return {
        "connected": works,
        "kind": credential.kind,
        "account_email": credential.account_email,
        "needs_reconnect": not works,
        "last_refreshed_at": (
            credential.last_refreshed_at.isoformat() if credential.last_refreshed_at else None
        ),
        "connected_at": credential.created_at.isoformat(),
    }
