"""Mailbox credentials: encrypted at rest, decrypted only where a connection is made.

Until now there was exactly one sending account and its password came from the environment. Ten
people cannot share one, so credentials move into the database -- and a refresh token sitting in
plain text in a table that is dumped nightly and shipped to S3 is not a thing to do by accident.

**Fernet, with the key in the environment.** Symmetric authenticated encryption from
`cryptography`, which this project already depends on. The key never goes in the database, so a
dump on its own is useless; losing the key means every mailbox must be reconnected, which is a
click each and strictly better than the alternative failure.

**What is deliberately *not* here.** No route returns a token, no log line contains one, no
`events` payload carries one, and `__repr__` is never allowed near one. The API answers *whether*
a mailbox is connected, as what address, and when it was last refreshed -- which is everything a
person needs and nothing an attacker does. Hard rule 2 covers secrets in logs; this is the same
rule applied to a secret that now lives in a row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from app.core.settings import Settings, config

logger = logging.getLogger(__name__)


class CredentialError(Exception):
    """Encryption is not configured, or the stored value cannot be read."""


@dataclass(frozen=True)
class Sealed:
    """An encrypted secret. Carries no plaintext and prints none."""

    token: bytes

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # A dataclass would otherwise print the ciphertext in every traceback and every debugger
        # frame. It is not plaintext, but it is also not something to scatter through logs.
        return "Sealed(...)"


def _cipher(settings: Settings | None = None) -> Fernet:
    settings = settings or config
    key = (settings.CREDENTIAL_KEY or "").strip()
    if not key:
        raise CredentialError(
            "CREDENTIAL_KEY is not set, so mailbox credentials cannot be stored. "
            "Generate one with `python -m app.scripts.make_key`."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        # The message says what is wrong with the key without quoting any of it.
        raise CredentialError(
            "CREDENTIAL_KEY is not a valid Fernet key (32 url-safe base64-encoded bytes)."
        ) from exc


def seal(secret: str, settings: Settings | None = None) -> Sealed:
    """Encrypt a secret for storage."""
    if not secret:
        raise CredentialError("refusing to store an empty credential")
    return Sealed(_cipher(settings).encrypt(secret.encode("utf-8")))


def unseal(sealed: bytes, settings: Settings | None = None) -> str:
    """Decrypt a stored secret.

    `InvalidToken` means the value was written under a different key -- almost always because
    `CREDENTIAL_KEY` was rotated or lost. That is reported as its own thing, because the fix is
    "reconnect the mailbox" and nothing about a generic decryption failure says so.
    """
    try:
        return _cipher(settings).decrypt(sealed).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialError(
            "this credential cannot be decrypted with the current CREDENTIAL_KEY; "
            "the mailbox needs to be reconnected"
        ) from exc


def is_configured(settings: Settings | None = None) -> bool:
    """Whether credentials can be stored at all. For the health screen, which should say so
    before somebody discovers it at the end of an OAuth round trip."""
    try:
        _cipher(settings)
    except CredentialError:
        return False
    return True
