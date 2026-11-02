"""Verifying that a booking webhook really came from this workspace's tool (F15, ADR-045).

Two independent things have to be true before a booking is believed, and neither alone is enough:

1. **The URL names a workspace, provably.** `/hooks/booking/{token}` carries an HMAC over the
   org id under a key derived from `CREDENTIAL_KEY` -- the same device as the one-click
   unsubscribe link (ADR-033), and for the same reason: a public route must not take an `org_id`
   from a caller (ADR-004). The token tells us *whose secret to check with*; it is not itself
   the authorisation.
2. **The body is signed with that workspace's own secret.** HMAC-SHA256 over the **raw bytes**,
   hex, in a header. This is deliberately the scheme Cal.com already implements
   (`X-Cal-Signature-256` is exactly "HMAC-SHA256 of the raw body with your secret"), so the
   commonest tool needs no adapter -- but nothing here is Cal.com-specific, and any tool that
   can sign a body with a secret you give it works.

**Why the body signature and not just the URL.** A URL-only scheme makes the URL the credential,
and that URL ends up pasted into a third party's settings page, their delivery logs and anybody's
screenshot of either. The body signature means a leaked URL alone books nothing.

**The raw bytes, never the re-serialised object.** Verifying a signature against JSON we parsed
and dumped again is the classic way a correct-looking implementation accepts a forged body: key
order, spacing and number formatting all change under a round trip, so the only way to make it
work is to be lax, and lax is what the signature was for.

**Replay is bounded by a timestamp, not by remembering every delivery.** A signed body is valid
for ever otherwise, so a copy captured from a provider's log could be replayed a year later. The
timestamp header is signed as part of the payload, so it cannot be edited forward. Idempotency is
a separate guarantee and lives in the database: `uq_booking_events_org_provider_ext` makes a
retried delivery -- which every provider does -- land on the row it already wrote.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

#: Bound into the derived key so a booking token can never be mistaken for an unsubscribe token
#: signed with the same secret. Versioned, so the scheme is rotated by changing one string.
_LABEL = b"outreach/booking/v1"

_MAC_BYTES = 16

#: The header a tool puts the body signature in. Cal.com's own name, so a Cal.com webhook needs
#: nothing configured beyond the secret; `X-Booking-Signature` is accepted as the generic
#: alternative for everything else.
SIGNATURE_HEADERS = ("x-cal-signature-256", "x-booking-signature")
TIMESTAMP_HEADER = "x-booking-timestamp"

#: How far out of date a signed delivery may be. Generous, because a provider's retry queue can
#: be minutes behind and refusing a legitimate retry loses a real meeting; short enough that a
#: body lifted from a log months later is useless.
MAX_SKEW = timedelta(minutes=15)

#: 32 bytes of `secrets` output. Long enough that there is no dictionary to slow down, which is
#: why this is compared with a MAC rather than stored as a password hash.
SECRET_BYTES = 32


def signing_key(credential_key: str) -> str:
    """The key the URL token is signed with, derived from the deployment's credential key.

    Derived rather than reused: `CREDENTIAL_KEY` encrypts mailbox credentials, and one secret
    doing both jobs means a flaw in either reaches the other.
    """
    digest = hmac.new(credential_key.encode("utf-8"), _LABEL, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def new_secret() -> str:
    """A fresh webhook secret for a workspace to paste into its booking tool."""
    return secrets.token_urlsafe(SECRET_BYTES)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign_token(*, org_id: uuid.UUID, secret: str) -> str:
    """The token that goes in the webhook URL: which workspace, signed."""
    payload = org_id.bytes
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_MAC_BYTES]
    return f"{_b64(payload)}.{_b64(mac)}"


def verify_token(token: str, *, secret: str) -> uuid.UUID | None:
    """The workspace a URL token proves, or `None`.

    **Every failure returns `None` and nothing raises.** The only caller is a public route that
    anybody may send anything to, and a public route that can be made to raise is a 500 handled
    outside the CORS middleware -- the trap that once made a duplicate key look like an
    unreachable backend.
    """
    payload_part, separator, mac_part = (token or "").partition(".")
    if not separator or not payload_part or not mac_part:
        return None
    try:
        payload = _unb64(payload_part)
        mac = _unb64(mac_part)
    except (ValueError, TypeError):
        return None
    if len(payload) != 16 or len(mac) != _MAC_BYTES:
        return None

    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_MAC_BYTES]
    if not hmac.compare_digest(mac, expected):
        return None
    return uuid.UUID(bytes=payload)


def body_signature(body: bytes, *, secret: str, timestamp: str = "") -> str:
    """What a correctly signed body's header should read.

    The timestamp is part of the signed payload, so it cannot be moved forward to make an old
    delivery look fresh. An empty timestamp signs the body alone, which is what Cal.com does.
    """
    payload = f"{timestamp}.".encode() + body if timestamp else body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def fresh_enough(timestamp: str, *, now: datetime | None = None) -> bool:
    """Whether a signed timestamp is inside the skew window. No timestamp is accepted.

    Accepting a missing timestamp is a deliberate compromise and worth naming: Cal.com signs the
    body alone, so requiring one would mean refusing the tool this is built for. A delivery with
    no timestamp is still protected by the secret and by the idempotency key; what it loses is
    the replay bound. A tool that can send one gets the stronger guarantee.
    """
    raw = (timestamp or "").strip()
    if not raw:
        return True
    now = now or datetime.now(tz=UTC)
    try:
        # Seconds since the epoch, which is what every provider that sends one uses.
        moment = datetime.fromtimestamp(int(float(raw)), tz=UTC)
    except (ValueError, OverflowError, OSError):
        return False
    return abs(now - moment) <= MAX_SKEW


def verify_body(
    body: bytes, *, headers: dict[str, str], secret: str, now: datetime | None = None
) -> bool:
    """Whether this raw body carries a valid signature from the workspace's own tool."""
    lowered = {key.lower(): value for key, value in headers.items()}
    offered = ""
    for name in SIGNATURE_HEADERS:
        if lowered.get(name):
            offered = lowered[name].strip()
            break
    if not offered:
        return False

    timestamp = lowered.get(TIMESTAMP_HEADER, "")
    if not fresh_enough(timestamp, now=now):
        return False

    # Some tools prefix the algorithm (`sha256=...`). Accepted, because refusing it would be a
    # refusal nobody could debug from the provider's side.
    if "=" in offered and offered.lower().startswith("sha256="):
        offered = offered.split("=", 1)[1].strip()

    expected = body_signature(body, secret=secret, timestamp=timestamp)
    return hmac.compare_digest(offered.lower(), expected)
