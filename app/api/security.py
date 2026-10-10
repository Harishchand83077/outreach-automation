"""Verifying a Supabase access token, locally.

`ARCHITECTURE_TARGET.md` §14. The browser logs in with Supabase; the API never talks to Supabase
to check a token. It fetches the project's JWKS once, caches it for the vendor's edge-cache
window, and verifies signatures itself. A network round trip per request would put an outage in
somebody else's infrastructure directly in the path of every page load.

What is checked, and why each one matters:

- **Signature**, against a key from the JWKS, selected by the token's `kid`.
- **Algorithm**, from an allowlist of asymmetric algorithms. This is the important one: without
  it, an attacker sends `alg: none`, or signs with HMAC using the *public* key as the secret, and
  the library happily agrees. Never trust the header's own claim about how to verify it.
- **`iss`**, so a token minted by a different Supabase project is not accepted here.
- **`exp`**, with no leeway worth mentioning.
- **`sub`**, because a token with no subject identifies nobody.

What is deliberately *not* checked: `aud`. Supabase issues `authenticated` for every signed-in
user, so it distinguishes nothing, and pretending otherwise would be security theatre.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

#: Asymmetric only. HS256 is absent on purpose: a shared secret that the API must hold is a
#: secret that can sign tokens, not merely verify them.
ALLOWED_ALGORITHMS = ("RS256", "RS512", "ES256", "ES512")

#: §14: do not cache the key set longer than the vendor's edge cache.
JWKS_CACHE_SECONDS = 600

#: A signing key can be rotated at any time. One refetch per unknown `kid`, rate-limited, so a
#: rotation recovers in seconds without letting an attacker drive unbounded fetches.
MIN_SECONDS_BETWEEN_REFETCHES = 30


class AuthError(Exception):
    """The token is not acceptable. The reason is for the log, never for the response body."""


@dataclass(frozen=True)
class TokenClaims:
    """The parts of a verified token this application uses."""

    subject: str
    email: str | None
    expires_at: int

    @property
    def user_id(self) -> str:
        return self.subject


class JwksCache:
    """Holds the project's public keys, refetched when they expire or a `kid` is unknown."""

    def __init__(self, url: str, *, ttl_seconds: int = JWKS_CACHE_SECONDS) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._client: PyJWKClient | None = None
        self._fetched_at = 0.0
        self._last_refetch = 0.0

    def _now(self) -> float:
        return time.monotonic()

    def _fresh_client(self) -> PyJWKClient:
        # PyJWKClient does its own caching; this wrapper owns the TTL so the window is ours to
        # state and to test, rather than a library default that may change.
        self._client = PyJWKClient(self._url, cache_keys=False)
        self._fetched_at = self._now()
        return self._client

    def signing_key(self, token: str) -> Any:
        if self._client is None or self._now() - self._fetched_at > self._ttl:
            self._fresh_client()

        assert self._client is not None
        try:
            return self._client.get_signing_key_from_jwt(token).key
        except Exception:
            # An unknown `kid` usually means the keys rotated since the last fetch.
            if self._now() - self._last_refetch < MIN_SECONDS_BETWEEN_REFETCHES:
                raise
            self._last_refetch = self._now()
            client = self._fresh_client()
            return client.get_signing_key_from_jwt(token).key


class TokenVerifier:
    """Verifies access tokens for one Supabase project."""

    def __init__(self, *, jwks_url: str, issuer: str, cache: JwksCache | None = None) -> None:
        self._issuer = issuer
        self._cache = cache or JwksCache(jwks_url)

    def verify(self, token: str) -> TokenClaims:
        """Return the claims, or raise `AuthError`. Never returns for an unverified token."""
        if not token:
            raise AuthError("no token supplied")

        try:
            key = self._cache.signing_key(token)
        except httpx.HTTPError as exc:
            raise AuthError(f"could not fetch the key set: {exc}") from exc
        except Exception as exc:
            raise AuthError(f"no signing key for this token: {exc}") from exc

        try:
            payload = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=self._issuer,
                options={
                    "require": ["exp", "sub", "iss"],
                    "verify_exp": True,
                    "verify_iss": True,
                    # See the module docstring: `aud` is the same for every signed-in user.
                    "verify_aud": False,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"token rejected: {type(exc).__name__}: {exc}") from exc

        subject = str(payload.get("sub") or "")
        if not subject:
            raise AuthError("token has no subject")

        email = payload.get("email")
        return TokenClaims(
            subject=subject,
            email=str(email) if email else None,
            expires_at=int(payload.get("exp", 0)),
        )


def bearer_token(header_value: str | None) -> str:
    """Pull the token out of an `Authorization` header, or raise.

    Case-insensitive on the scheme because clients vary, strict about the rest.
    """
    if not header_value:
        raise AuthError("no Authorization header")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Authorization header is not a bearer token")
    token = parts[1].strip()
    if not token:
        raise AuthError("bearer token is empty")
    return token
