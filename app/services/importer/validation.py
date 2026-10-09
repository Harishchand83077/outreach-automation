"""Per-row validation for an import.

The prototype's entire check was `'@' in email` (issue D4). A row that failed was silently
skipped, so an import could quietly drop half a sheet and report success.

Here, every row gets an explicit verdict with reasons, and the user sees them before committing.
Nothing is silently dropped.

DNS is the only I/O, and it sits behind `MxResolver` so tests inject a fake and never touch the
network. That matters beyond test hygiene: MX lookups are slow and rate-limited, so the real
resolver caches per domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlparse, urlunparse

from email_validator import EmailNotValidError
from email_validator import validate_email as _validate_syntax

#: Mailbox names that are almost never a person. Emailing them is a waste and a spam signal.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "info",
        "sales",
        "support",
        "contact",
        "admin",
        "hello",
        "hi",
        "team",
        "office",
        "enquiries",
        "inquiries",
        "help",
        "service",
        "services",
        "billing",
        "accounts",
        "finance",
        "hr",
        "careers",
        "jobs",
        "recruitment",
        "marketing",
        "press",
        "media",
        "legal",
        "privacy",
        "security",
        "abuse",
        "postmaster",
        "webmaster",
        "noreply",
        "no-reply",
        "donotreply",
        "mailer-daemon",
    }
)

#: Free mail providers. Not a rejection — a founder may genuinely use one — but it means we
#: cannot infer a company website from the address, and we must not try.
FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.in",
        "yahoo.co.uk",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "rediffmail.com",
        "zoho.com",
        "yandex.com",
        "gmx.com",
        "mail.com",
    }
)

#: Obvious disposable-address providers.
DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "10minutemail.com",
        "tempmail.com",
        "throwaway.email",
        "yopmail.com",
        "trashmail.com",
        "sharklasers.com",
    }
)

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class RowVerdict(StrEnum):
    """What the importer will do with a row."""

    OK = "ok"
    #: Importable, but something is worth the user's attention first.
    WARN = "warn"
    #: Not importable. Always reported with a reason, never silently dropped.
    REJECT = "reject"


class MxResolver(Protocol):
    """Can this domain receive mail? Injected, so tests never hit DNS."""

    def has_mx(self, domain: str) -> bool: ...


class NullMxResolver:
    """Assume every domain is deliverable. The default when MX checking is switched off."""

    def has_mx(self, domain: str) -> bool:
        return True


class DnsMxResolver:
    """Real DNS, cached per domain for the life of one import.

    A sheet usually contains several people at the same company, and MX lookups are slow and
    rate-limited, so the same domain is never queried twice.
    """

    def __init__(self, timeout: float = 3.0) -> None:
        self._timeout = timeout
        self._cache: dict[str, bool] = {}

    def has_mx(self, domain: str) -> bool:
        if domain in self._cache:
            return self._cache[domain]
        self._cache[domain] = self._lookup(domain)
        return self._cache[domain]

    def _lookup(self, domain: str) -> bool:
        import dns.exception
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.timeout = self._timeout
        resolver.lifetime = self._timeout
        try:
            return bool(resolver.resolve(domain, "MX"))
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            # A definitive "no": this domain cannot receive mail.
            return False
        except dns.exception.DNSException:
            # A timeout or server failure is not evidence of absence. Treat it as deliverable
            # and let the user decide, rather than rejecting a real lead over a flaky lookup.
            return True


@dataclass
class ValidatedRow:
    """One row's verdict, with everything needed to show the user why."""

    row_number: int
    verdict: RowVerdict = RowVerdict.OK
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    email_norm: str | None = None
    email_raw: str | None = None
    full_name: str | None = None
    company_name: str | None = None
    company_url: str | None = None
    domain: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    notes: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.verdict = RowVerdict.REJECT
        self.reasons.append(reason)

    def warn(self, reason: str, flag: str | None = None) -> None:
        # A warning never downgrades a rejection.
        if self.verdict is RowVerdict.OK:
            self.verdict = RowVerdict.WARN
        self.reasons.append(reason)
        if flag and flag not in self.flags:
            self.flags.append(flag)

    @property
    def importable(self) -> bool:
        return self.verdict is not RowVerdict.REJECT


def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


def domain_of(email: str) -> str:
    return email.rpartition("@")[2]


def check_email(row: ValidatedRow, raw: str, *, mx: MxResolver) -> None:
    """Validate one address: syntax, then role, disposable, free-mail and MX."""
    candidate = (raw or "").strip()
    if not candidate:
        row.reject("email is empty")
        return

    try:
        # Deliverability is checked separately through the injected resolver, so this is syntax
        # and normalisation only — no network from inside the library.
        validated = _validate_syntax(candidate, check_deliverability=False)
    except EmailNotValidError as exc:
        row.reject(f"email is not valid: {exc}")
        return

    row.email_raw = candidate
    row.email_norm = validated.normalized.lower()
    row.domain = domain_of(row.email_norm)

    local = row.email_norm.partition("@")[0]
    if local in ROLE_LOCAL_PARTS:
        # Importable, but these are shared inboxes: low reply rate and a spam signal.
        row.warn(f"{local}@ is a role address, not a person", flag="role_address")

    if row.domain in DISPOSABLE_DOMAINS:
        row.reject(f"{row.domain} is a disposable mail provider")
        return

    if row.domain in FREE_MAIL_DOMAINS:
        row.warn(
            f"{row.domain} is a free mail provider, so the company site cannot be inferred",
            flag="free_mail",
        )

    if not mx.has_mx(row.domain):
        row.reject(f"{row.domain} has no MX record, so it cannot receive mail")


def normalise_url(raw: str) -> str | None:
    """Return a canonical `https://host/path`, or None if it is not a usable URL."""
    candidate = (raw or "").strip()
    if not candidate:
        return None

    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    host = parsed.netloc.lower().removeprefix("www.")
    if ":" in host:
        host, _, port = host.partition(":")
        if not port.isdigit():
            return None

    labels = host.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None

    # Always https: the fetcher only speaks http(s), and upgrading costs nothing.
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", parsed.query, ""))


def check_company_url(row: ValidatedRow, raw: str | None) -> None:
    """Normalise the company URL, and refuse to invent one.

    The prototype had no company URL at all, so "research" worked from the company *name* alone
    (issue A1). Having the field is what makes real research possible — but a missing one must
    never be guessed from the email domain, because for a free-mail address that would send us
    off to fetch gmail.com.
    """
    if not raw or not str(raw).strip():
        row.warn(
            "no company_url: research will be thin, and it will not be guessed from the "
            "email domain",
            flag="no_company_url",
        )
        return

    normalised = normalise_url(str(raw))
    if normalised is None:
        row.warn(f"company_url {str(raw)[:60]!r} is not a usable URL", flag="bad_company_url")
        return
    row.company_url = normalised


def check_linkedin_url(row: ValidatedRow, raw: str | None) -> None:
    """Store a LinkedIn URL for reference. **Never fetched** — hard rule 4."""
    if not raw or not str(raw).strip():
        return
    normalised = normalise_url(str(raw))
    if normalised is None:
        row.warn("linkedin_url is not a usable URL", flag="bad_linkedin_url")
        return
    if "linkedin." not in normalised:
        row.warn("linkedin_url does not look like a LinkedIn address", flag="bad_linkedin_url")
        return
    row.linkedin_url = normalised
