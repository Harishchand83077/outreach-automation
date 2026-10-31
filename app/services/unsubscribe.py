"""Signed one-click unsubscribe links (ADR-033, RFC 8058).

A recipient must be able to unsubscribe without signing in, so the link is **unauthenticated and
public**. The token therefore carries its own proof: who is unsubscribing, signed, so it cannot
be edited into somebody else's identity.

**Integrity, not confidentiality.** Anyone holding the email holds the token, and that is the
design rather than a weakness — the point is that the recipient can act. What must be impossible
is altering a token to unsubscribe a different lead, which is what the MAC prevents.

The signing key is **derived** from `CREDENTIAL_KEY` under a fixed label rather than being that
key. `CREDENTIAL_KEY` encrypts mailbox credentials; this signs public URLs. One secret used for
both would mean a flaw in either reached the other, and deriving costs nothing and adds no new
secret for the owner to manage or rotate.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid

#: Bound into the derived key so this signature can never be confused with another use of the
#: same secret, now or later. Versioned so the scheme can be rotated by changing one string.
_LABEL = b"outreach/unsubscribe/v1"

#: 16 bytes of a SHA-256 MAC. Truncation is standard and deliberate: 128 bits is far beyond
#: forgeable, and the token travels in a URL where every character is one more thing a mail
#: client can wrap, break across lines or mangle.
_MAC_BYTES = 16


def signing_key(credential_key: str) -> str:
    """The key this module signs with, derived from the deployment's credential key."""
    digest = hmac.new(credential_key.encode("utf-8"), _LABEL, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def _b64(raw: bytes) -> str:
    # Unpadded so the token carries no `=`, which is one fewer character to be escaped by
    # whatever rewrites links in a recipient's mail client.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def sign(*, org_id: uuid.UUID, lead_id: uuid.UUID, secret: str) -> str:
    """A token identifying one lead of one workspace, as `payload.mac`."""
    payload = org_id.bytes + lead_id.bytes
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_MAC_BYTES]
    return f"{_b64(payload)}.{_b64(mac)}"


def verify(token: str, *, secret: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """The `(org_id, lead_id)` a token proves, or `None`.

    **Every failure returns `None` and nothing raises.** The only caller is a public route that
    anybody may send anything to, and a route that can be made to raise by sending it rubbish is
    one somebody will make raise — which in this codebase means a 500 handled outside the CORS
    middleware, the trap that once made a duplicate key look like an unreachable backend.
    """
    payload_part, separator, mac_part = token.partition(".")
    if not separator or not payload_part or not mac_part:
        return None
    try:
        payload = _unb64(payload_part)
        mac = _unb64(mac_part)
    except (ValueError, TypeError):
        return None
    if len(payload) != 32 or len(mac) != _MAC_BYTES:
        return None

    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_MAC_BYTES]
    # Constant time, so the comparison cannot be used to discover a valid MAC byte by byte.
    if not hmac.compare_digest(mac, expected):
        return None
    return uuid.UUID(bytes=payload[:16]), uuid.UUID(bytes=payload[16:])


def link(base_url: str, token: str) -> str:
    """The public URL for a token, or an empty string when no public base is configured.

    An empty base is the ordinary state of a deployment that has not set `PUBLIC_API_URL`, and
    it means no one-click header is sent at all (ADR-033): a URL built against a guess would be
    worse than none, since the receiver would POST an opt-out into the void.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/u/{token}"
