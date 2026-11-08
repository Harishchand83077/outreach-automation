"""The parts of OAuth 2.1 that are rules rather than rows (F14, ADR-058). No I/O (§4).

**Everything here is a refusal.** An authorization server is mostly a list of things that must
not be accepted, and each one below exists because accepting it hands somebody else's credential
to somebody. They are in this module, pure and with an injected clock, so each refusal is a test
that needs no database.

The four that matter most, and why:

- **PKCE with S256, always.** OAuth 2.1 removes the implicit grant and requires PKCE for every
  client, because an authorization code travelling through a browser redirect can be stolen --
  from a log, from `Referer`, from another app registered on the same URI scheme. `plain` is
  refused outright: a challenge equal to its verifier protects against nothing and exists only
  for clients that could not hash, which is no client we will ever see.
- **The `resource` parameter is checked** (RFC 8707). **This is the whole reason we are running
  an authorization server at all**: Supabase accepts `resource` and ignores it, issuing a token
  whose audience is the constant `"authenticated"` (supabase/auth#2610), so a token obtained for
  any other service would be accepted here. A token minted by us says which resource it is for,
  and `/mcp` refuses one minted for something else.
- **The redirect URI must match exactly**, byte for byte, against one registered for the client.
  Not a prefix, not a subdomain, not "same origin": every loosening of this rule is a published
  attack, because the redirect is where the code goes.
- **A code is single-use and short-lived.** Replaying one is how a code captured from a log
  becomes a session.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

#: How long an authorization code lives. The spec says "short"; a code is exchanged within
#: milliseconds of being issued by a client that is already waiting for it, so a minute is
#: generous and ten would only widen the window for a code read out of a log.
CODE_TTL = timedelta(seconds=60)

#: Access tokens are short because a refresh token exists. The old hand-minted tokens live 90
#: days because there is nothing to refresh them with; these do not need to.
ACCESS_TTL = timedelta(hours=1)

#: A refresh token lives as long as the hand-minted tokens did, and is rotated on every use.
REFRESH_TTL = timedelta(days=90)

#: The only challenge method. See the module docstring for why `plain` is not here.
CHALLENGE_METHOD = "S256"

#: The only grant types. OAuth 2.1 removes `implicit` and `password`; `client_credentials` is
#: absent because every token here acts for a person and must resolve to their membership.
GRANT_TYPES: tuple[str, ...] = ("authorization_code", "refresh_token")

CODE_BYTES = 32
TOKEN_BYTES = 32


class OAuthError(Exception):
    """A refusal, carrying the code the spec names for it.

    `error` is what goes back to the client, and it is deliberately coarse: `invalid_grant`
    covers unknown, expired, replayed and wrong-verifier, because telling a caller which of
    those it was tells somebody guessing which half of their guess was right.
    """

    def __init__(self, error: str, description: str, *, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


def new_code() -> tuple[str, str]:
    """`(code, hash)`. Only the hash is stored, as for every other credential here."""
    code = secrets.token_urlsafe(CODE_BYTES)
    return code, hash_secret(code)


def new_refresh_token() -> tuple[str, str]:
    token = f"oar_{secrets.token_urlsafe(TOKEN_BYTES)}"
    return token, hash_secret(token)


def hash_secret(value: str) -> str:
    """`sha256` hex.

    Not a password hash, for the reason `api_tokens` gives: 32 bytes of `secrets` output has no
    dictionary to slow down, and the lookup has to be deterministic to be a lookup.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_challenge(verifier: str, challenge: str, method: str = CHALLENGE_METHOD) -> bool:
    """Whether a verifier produces this challenge under S256.

    The comparison is `secrets.compare_digest`, so it does not leak how much of the challenge
    matched through timing -- the same care the signature checks in the booking webhook take.
    """
    if method != CHALLENGE_METHOD:
        return False
    if not verifier or not challenge:
        return False
    # RFC 7636: 43 to 128 characters. A short verifier is brute-forceable and a client sending
    # one is a client that generated it wrong.
    if not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode("ascii", "ignore")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


def check_redirect_uri(given: str, registered: list[str]) -> str:
    """The redirect URI, or raise. **Exact match only.**

    Every loosening of this rule -- prefix matching, "same host", ignoring the query -- is a
    published attack, because the redirect is where the code is delivered. A client with several
    redirect URIs registers all of them.
    """
    if not given:
        raise OAuthError("invalid_request", "A redirect_uri is required.")
    if given not in registered:
        raise OAuthError(
            "invalid_request",
            "That redirect_uri is not registered for this client. It must match one of them "
            "exactly, including the scheme, the port and any path.",
        )
    return given


def check_resource(given: str | None, expected: str) -> str:
    """The RFC 8707 `resource`, or raise.

    **The reason this server exists.** A token has to say which API it was minted for, or it is
    a token any other API could be persuaded to accept -- which is exactly the confused-deputy
    problem resource indicators were added to solve, and exactly what Supabase's implementation
    does not do.

    A missing `resource` is refused rather than defaulted. Defaulting it would mean a client
    that never asked for an audience gets one anyway, and the token it receives would then be
    indistinguishable from one that did.
    """
    if not expected:
        raise OAuthError(
            "server_error",
            "This deployment has no PUBLIC_API_URL, so it cannot say which resource a token "
            "would be for. Set it before enabling the connector.",
            status=500,
        )
    if not given:
        raise OAuthError(
            "invalid_target",
            "A resource parameter is required (RFC 8707). It says which API the token is for, "
            "and a token that does not say is one another API could be talked into accepting.",
        )
    if given.rstrip("/") != expected.rstrip("/"):
        raise OAuthError("invalid_target", "That resource is not this server.")
    return expected


def check_scopes(requested: str | None, allowed: tuple[str, ...]) -> tuple[str, ...]:
    """The scopes to grant, or raise.

    A request for a scope this deployment does not have is refused rather than trimmed: a client
    that asked for something and silently received less will behave as though it has it, and the
    failure surfaces later as a tool that does not work for reasons nobody can see.
    """
    wanted = tuple(part for part in (requested or "").split() if part)
    if not wanted:
        raise OAuthError("invalid_scope", "At least one scope is required.")
    unknown = [scope for scope in wanted if scope not in allowed]
    if unknown:
        raise OAuthError(
            "invalid_scope",
            f"This deployment does not have {', '.join(unknown)}. It has: {', '.join(allowed)}.",
        )
    return wanted


@dataclass(frozen=True)
class Grant:
    """What an authorization code stands for, once it has been checked."""

    client_id: str
    user_id: str
    scopes: tuple[str, ...]
    redirect_uri: str
    resource: str
    code_challenge: str
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def expiry(now: datetime, ttl: timedelta) -> datetime:
    return now + ttl
