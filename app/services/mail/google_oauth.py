"""Connecting a Gmail account by clicking a button instead of typing an app password.

The user presses **Connect Gmail**, Google asks them to approve, and we come away with a refresh
token. They never type a password into our software, and they can revoke us from their own Google
account page at any time -- which is the real improvement over an app password, not the
convenience.

**Why not the Gmail API.** Scope `https://mail.google.com/` authorises SMTP and IMAP over
XOAUTH2, so every line of the existing send loop, outbox, IMAP poller, reply matcher and ADR-002
recovery pass goes on working unchanged. Moving to the Gmail API would mean rewriting all of it
to reach the same place, and the API cannot do the one thing ADR-002 depends on: reading the Sent
folder to resolve a send whose outcome we never learned. What changes is one function -- how the
SMTP and IMAP clients authenticate.

**The scope costs something, and the plan says so out loud.** Google calls `gmail.send`
*sensitive* (free brand verification) and anything that reads a mailbox *restricted* -- which
needs an annual third-party security assessment that costs real money, and hard rule 5 says not
to spend it. An **unpublished** client in Testing status needs neither: up to 100 test users, any
scope. The price is that its refresh tokens expire after **7 days**.

That expiry is handled as a product state rather than hidden: the refresh fails, the mailbox is
paused with a reason, an alert is raised, and the dashboard asks for a reconnect. A weekly click
for ten people is acceptable. Silence would not be. `MULTI_TENANT_PLAN.md` §4 records both ways
out when this outgrows ten people.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.core.settings import Settings, config

logger = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
# The linter sees "TOKEN" in the name and assumes a secret. This is a public URL.
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v2/userinfo"

#: Mail access over SMTP and IMAP, plus enough identity to know which account was authorised.
#:
#: `https://mail.google.com/` is the broad one and it is what XOAUTH2 against SMTP and IMAP
#: requires -- there is no narrower scope that authorises those protocols. `email` is here so the
#: callback can check that the account the user picked is the mailbox they are connecting; with
#: several accounts signed in, choosing the wrong one is easy and otherwise only shows up as a
#: baffling failure at send time.
SCOPES = ("https://mail.google.com/", "email")

#: Refresh a little before the access token actually dies, so a send that starts at the boundary
#: does not fail on a token that expired mid-conversation.
REFRESH_MARGIN = timedelta(minutes=5)


class OAuthError(Exception):
    """The flow failed. The message is safe to show: it never contains a token."""


@dataclass(frozen=True)
class TokenSet:
    """What Google gave us back."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"TokenSet(expires_at={self.expires_at.isoformat()}, ...)"


def new_state() -> str:
    """An unguessable nonce tying a callback to the request that started it."""
    return secrets.token_urlsafe(32)


def authorize_url(state: str, settings: Settings | None = None) -> str:
    """Where to send the user's browser.

    `access_type=offline` with `prompt=consent` is what actually returns a **refresh** token.
    Without `prompt=consent` Google returns one only on the very first authorisation for that
    user and client, so a reconnect after an expiry -- the whole point of this flow in Testing
    status -- would come back with an access token and nothing to renew it. That failure is
    invisible for an hour and then permanent, which is the worst shape a bug can have.
    """
    settings = settings or config
    if not settings.google_oauth_configured:
        raise OAuthError(
            "Google sign-in is not configured on this server "
            "(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI)."
        )
    query = urlencode(
        {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{AUTH_ENDPOINT}?{query}"


async def exchange_code(
    code: str, settings: Settings | None = None, client: httpx.AsyncClient | None = None
) -> TokenSet:
    """Trade the one-time code for tokens."""
    settings = settings or config
    payload = {
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    data = await _post(TOKEN_ENDPOINT, payload, client)
    refresh = data.get("refresh_token")
    if not refresh:
        # Almost always `prompt=consent` missing, or a previously-authorised account. Said
        # plainly, because "it worked and then stopped an hour later" is otherwise unexplainable.
        raise OAuthError(
            "Google did not return a refresh token, so this connection could not be kept. "
            "Remove this app at myaccount.google.com/permissions and connect again."
        )
    return _token_set(data, refresh_token=str(refresh))


async def refresh_access_token(
    refresh_token: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> TokenSet:
    """Mint a new access token. The refresh token itself is normally unchanged."""
    settings = settings or config
    payload = {
        "refresh_token": refresh_token,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "grant_type": "refresh_token",
    }
    data = await _post(TOKEN_ENDPOINT, payload, client)
    rotated = data.get("refresh_token")
    # Google usually returns the same refresh token; it occasionally rotates one. Taking
    # whichever came back keeps a rotation from silently invalidating the stored copy.
    return _token_set(data, refresh_token=str(rotated) if rotated else refresh_token)


async def account_email(access_token: str, client: httpx.AsyncClient | None = None) -> str | None:
    """Which Google account this token belongs to."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await client.get(
            USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code != 200:
            return None
        email = response.json().get("email")
        return str(email).lower() if email else None
    except httpx.HTTPError:
        # Not fatal: it is a confirmation step, and failing it should not lose a token the user
        # has already approved.
        return None
    finally:
        if owned:
            await client.aclose()


def xoauth2_string(username: str, access_token: str) -> str:
    """The SASL XOAUTH2 initial response, as SMTP and IMAP both want it.

    `user=<email>^Aauth=Bearer <token>^A^A`, where ^A is 0x01. Returned unencoded: `aiosmtplib`
    and `imaplib` each base64 it themselves, and doing it here would double-encode.
    """
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01"


async def _post(
    url: str, payload: dict[str, str], client: httpx.AsyncClient | None
) -> dict[str, object]:
    owned = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await client.post(url, data=payload)
    except httpx.HTTPError as exc:
        raise OAuthError(f"could not reach Google ({type(exc).__name__})") from exc
    finally:
        if owned:
            await client.aclose()

    if response.status_code != 200:
        # Google's `error` field is a short machine string such as `invalid_grant`. The
        # description is included because `invalid_grant` alone has about six possible causes,
        # and neither field ever contains a token.
        try:
            body = response.json()
            detail = body.get("error_description") or body.get("error") or "unknown error"
        except ValueError:
            detail = f"HTTP {response.status_code}"
        raise OAuthError(f"Google refused the request: {detail}")

    result: dict[str, object] = response.json()
    return result


def _token_set(data: dict[str, object], *, refresh_token: str | None) -> TokenSet:
    access = data.get("access_token")
    if not access:
        raise OAuthError("Google's response contained no access token")
    try:
        lifetime = int(str(data.get("expires_in", 3600)))
    except ValueError:
        lifetime = 3600
    return TokenSet(
        access_token=str(access),
        refresh_token=refresh_token,
        expires_at=datetime.now(tz=UTC) + timedelta(seconds=lifetime),
        scopes=str(data.get("scope") or " ".join(SCOPES)),
    )
