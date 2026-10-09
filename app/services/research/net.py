"""SSRF protection for outbound fetches.

The URLs we fetch come from an uploaded spreadsheet, which is to say from outside. A hostile or
merely careless `company_url` must not be able to make the worker fetch something on our own
network — cloud metadata, an internal admin page, a database on localhost. The worker runs on the
same VM as Postgres, so "localhost" is a real target rather than a theoretical one.

`ARCHITECTURE_TARGET.md` §12 and §16. Every rule here exists because of a specific attack:

- **Private, loopback, link-local and reserved ranges are refused.** `169.254.169.254` is the
  cloud metadata endpoint on AWS, GCP and Azure, and it lives inside link-local.
- **Every resolved address must be safe**, not merely one of them. A hostile DNS record can
  return a public address *and* a private one; picking the safe one would be a coin flip we
  would sometimes lose.
- **Only http and https, only ports 80 and 443.** `file://`, `gopher://` and a Redis port are
  all reachable from a naive fetcher.
- **Every redirect hop is revalidated.** A public URL that 302s to `http://127.0.0.1:6379` is
  the classic bypass, and it defeats any check done only on the original URL.

**Known residual risk, stated plainly:** between resolving a hostname and connecting to it, DNS
could change (a rebinding attack). Pinning the connection to the resolved IP would break TLS
certificate validation for https, which is a worse trade for fetching public company sites. The
window is narrowed by refusing a host when *any* of its addresses is unsafe, and by
revalidating each hop. It is not closed.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

#: Hosts we never fetch. LinkedIn is here because of hard rule 4 — the URL is stored and
#: displayed, never retrieved — and the rest are login walls or endless feeds that would waste
#: the evidence budget on a cookie banner.
BLOCKED_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com",
        "www.linkedin.com",
        "lnkd.in",
        "facebook.com",
        "www.facebook.com",
        "fb.com",
        "m.facebook.com",
        "instagram.com",
        "www.instagram.com",
        "twitter.com",
        "www.twitter.com",
        "x.com",
        "www.x.com",
        "t.co",
        "tiktok.com",
        "www.tiktok.com",
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata.goog",
    }
)

#: Suffixes that are internal by definition, so no public company site can legitimately use one.
BLOCKED_SUFFIXES: tuple[str, ...] = (
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".lan",
    ".corp",
    ".intranet",
)

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})

#: RFC 6598 carrier-grade NAT. Python's `is_private` does not cover it, and an ISP-internal
#: address is no more a legitimate fetch target than an RFC 1918 one.
CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")

#: Explicitly named because it is the single most valuable SSRF target on a cloud VM: it serves
#: instance credentials to anything that can make an HTTP request. It is inside link-local and so
#: already refused, but naming it keeps the intent legible.
CLOUD_METADATA_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "fd00:ec2::254",
    }
)


class UnsafeTarget(Exception):
    """This URL must not be fetched. The message always says which rule applied."""


@dataclass(frozen=True)
class SafeTarget:
    """A URL that passed every check, with the addresses it resolved to."""

    url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def ip_is_safe(address: str) -> tuple[bool, str]:
    """Is this address a legitimate public destination? Returns (safe, reason-if-not)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False, f"{address!r} is not an IP address"

    if address in CLOUD_METADATA_IPS:
        return False, f"{address} is the cloud metadata endpoint"

    # Before the generic checks: an IPv4-mapped or 6to4 address can smuggle a private IPv4
    # inside an IPv6 literal, and resolving it first makes the refusal name the real problem
    # rather than reporting the wrapper as merely "private".
    if isinstance(ip, ipaddress.IPv6Address):
        nested = ip.ipv4_mapped or getattr(ip, "sixtofour", None)
        if nested is not None:
            nested_safe, nested_reason = ip_is_safe(str(nested))
            if not nested_safe:
                return False, f"{address} maps to {nested_reason}"

    if isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT_V4:
        return False, f"{address} is carrier-grade NAT"

    if ip.is_loopback:
        return False, f"{address} is loopback"
    if ip.is_private:
        # Covers RFC 1918, carrier-grade NAT, and IPv6 unique-local.
        return False, f"{address} is a private address"
    if ip.is_link_local:
        return False, f"{address} is link-local"
    if ip.is_multicast:
        return False, f"{address} is multicast"
    if ip.is_reserved or ip.is_unspecified:
        return False, f"{address} is reserved"

    return True, ""


def host_is_blocked(host: str) -> tuple[bool, str]:
    """Reject hosts by name, before any DNS lookup."""
    lowered = host.strip().lower().rstrip(".")
    if not lowered:
        return True, "the URL has no host"
    if lowered in BLOCKED_HOSTS:
        return True, f"{lowered} is on the blocked-host list"
    if any(lowered.endswith(suffix) for suffix in BLOCKED_SUFFIXES):
        return True, f"{lowered} uses an internal-only domain suffix"
    return False, ""


def resolve(host: str) -> list[str]:
    """Every address this host resolves to.

    All of them matter: a host is only safe if *every* address is, so returning the full set is
    the point rather than an implementation detail.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeTarget(f"{host} does not resolve ({exc.strerror or exc})") from exc

    seen: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in seen:
            seen.append(address)
    if not seen:
        raise UnsafeTarget(f"{host} resolved to no addresses")
    return seen


def validate_url(url: str, *, resolver: object = None) -> SafeTarget:
    """Check a URL against every rule, or raise `UnsafeTarget`.

    `resolver` is injectable so tests can simulate hostile DNS — including a host that returns
    one public and one private address — without needing real records to exist.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeTarget(
            f"scheme {parsed.scheme!r} is not allowed; only http and https are fetched"
        )

    host = (parsed.hostname or "").strip()
    blocked, why = host_is_blocked(host)
    if blocked:
        raise UnsafeTarget(why)

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if port not in ALLOWED_PORTS:
        raise UnsafeTarget(f"port {port} is not allowed; only 80 and 443 are fetched")

    # A bare IP in the URL skips DNS entirely, so check it directly as well.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        safe, reason = ip_is_safe(host)
        if not safe:
            raise UnsafeTarget(reason)
        return SafeTarget(url=url, host=host, port=port, addresses=(host,))

    lookup = resolver if resolver is not None else resolve
    addresses = lookup(host)  # type: ignore[operator]
    if not addresses:
        # Vacuously "nothing unsafe" would otherwise let an empty answer through.
        raise UnsafeTarget(f"{host} resolved to no addresses")

    # Every address, not just the first: a hostile record can return a public address alongside
    # a private one, and choosing between them would be a gamble.
    for address in addresses:
        safe, reason = ip_is_safe(address)
        if not safe:
            raise UnsafeTarget(f"{host} resolves to {reason}")

    return SafeTarget(url=url, host=host, port=port, addresses=tuple(addresses))


def registrable_domain(host: str) -> str:
    """The site a host belongs to, using the public suffix list.

    `tld` knows that `.co.uk` is a suffix, so `a.co.uk` and `b.co.uk` are correctly different
    sites. A naive last-two-labels rule would call them the same and let research wander from
    one company onto another's pages.

    `tld` raises for domains that are not under a real public suffix, which includes the
    `.example` names used in tests, so those fall back to last-two-labels.
    """
    cleaned = host.strip().lower().rstrip(".")
    try:
        from tld import get_fld
        from tld.exceptions import TldBadUrl, TldDomainNotFound

        fld = get_fld(cleaned, fix_protocol=True)
        if fld:
            return str(fld).lower()
    except (TldBadUrl, TldDomainNotFound, ValueError):
        pass

    labels = cleaned.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else cleaned


def same_registrable_domain(a: str, b: str) -> bool:
    """Whether two hosts belong to the same site.

    Used to keep research on the company's *own* pages: a link to a CDN or a partner is not
    evidence about this company, and following it would also widen the fetch surface.
    """
    return registrable_domain(a) == registrable_domain(b)
