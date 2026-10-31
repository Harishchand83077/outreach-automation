"""What a sending domain looks like to Gmail and Yahoo (F4).

The bulk-sender rules ask for SPF, DKIM, DMARC and a one-click unsubscribe. This checks the
first three in DNS and the fourth in configuration, and it is built around one honesty rule:
**say what DNS can prove and no more.**

That rule is not pedantry here. This repo has already been caught by the gap between *published*
and *working*: mail went out as `From: rahul@arkvane.in` while SMTP authenticated as an unrelated
Gmail account, so the SPF and DKIM records verified for that domain applied to none of it — and
the first reply the system ever received was sitting in Spam. A report that had said "DKIM: pass"
would have been true about the record and wrong about the mail.

So:

- **SPF** is checkable. Exactly one record passes; two is a permanent error rather than a merge,
  and is the single most common way a domain's mail breaks.
- **DKIM** is only *probeable*. DNS cannot enumerate selectors, so a miss is `UNKNOWN` — "not at
  the selectors we tried" — and a key that is published is reported as published, never as
  signing. Only a received message shows that (`GO_LIVE.md` §1).
- **DMARC** is checkable, and a missing `rua` is a warning: aggregate reports are the one
  deliverability signal that works at our volume, where Postmaster Tools shows nothing (ADR-029).
- A **failed lookup is `UNKNOWN`, never `FAIL`.** A timeout is not evidence of absence, which is
  the rule the importer's MX check already follows.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

#: Domains a provider owns, where the sender does not control DNS and does not need to.
#:
#: **This exists because the product is multi-user.** Most people who sign up will connect a
#: Gmail address, and `gmail.com` answers an SPF and a DMARC record but not any selector we could
#: guess -- Google signs outbound Gmail with its own rotating selectors. Checked as an ordinary
#: domain, such a mailbox is reported "not ready" over a DKIM record the user **cannot publish
#: and does not need**. Telling the majority of signups to fix something unfixable is how a
#: report stops being read.
#:
#: What is true of a shared domain is different rather than absent, and §`_shared` says it: the
#: authentication is the provider's and is fine, the *reputation* is shared with every other
#: sender on it, and it cannot be verified in Postmaster Tools because it is not yours.
PROVIDER_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.in",
        "ymail.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "zoho.com",
        "gmx.com",
        "yandex.com",
    }
)

#: Selectors worth probing. Ours is Google's; the rest are the common defaults of providers
#: somebody might reasonably be sending through. Not exhaustive and cannot be — hence `UNKNOWN`
#: rather than `FAIL` when none answers.
DKIM_SELECTORS: tuple[str, ...] = ("google", "default", "selector1", "selector2", "s1", "k1")

DNS_TIMEOUT_SECONDS = 3.0


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 - a verdict, not a credential
    WARN = "warn"
    FAIL = "fail"
    #: We could not find out. Distinct from `FAIL` on purpose: a lookup that timed out and a
    #: record that is absent are different facts, and conflating them turns a flaky resolver
    #: into a false accusation.
    UNKNOWN = "unknown"


class TxtResolver(Protocol):
    """Returns the TXT strings at a name, or `None` when the lookup itself failed."""

    def txt(self, name: str) -> list[str] | None: ...


@dataclass(frozen=True)
class Check:
    name: str
    verdict: Verdict
    #: One sentence an operator can act on. **Never a key, an address or a token**: this reaches
    #: a browser, a screenshot and a log.
    detail: str


@dataclass(frozen=True)
class DomainReport:
    domain: str
    spf: Check
    dkim: Check
    dmarc: Check
    one_click: Check
    #: True when the domain belongs to a mail provider rather than to this sender, so the DNS
    #: checklist does not apply. Carried on the report so a caller can say so in words instead
    #: of inferring it from three identical checks.
    provider_owned: bool = False

    @property
    def items(self) -> tuple[Check, ...]:
        if self.provider_owned:
            # One authentication line rather than the same sentence three times.
            return (self.spf, self.one_click)
        return (self.spf, self.dkim, self.dmarc, self.one_click)

    @property
    def ready(self) -> bool:
        """Whether this domain is fit to send bulk mail from.

        `UNKNOWN` counts as not ready. For a go-live gate "we could not tell" is not a yes, and
        saying otherwise would let a flaky lookup wave a domain through.
        """
        return all(
            check.verdict is Verdict.PASS or check.verdict is Verdict.WARN for check in self.items
        )


def _spf(records: list[str] | None) -> Check:
    if records is None:
        return Check("SPF", Verdict.UNKNOWN, "The DNS lookup failed, so this could not be checked.")
    spf = [record for record in records if record.lower().startswith("v=spf1")]
    if not spf:
        return Check(
            "SPF",
            Verdict.FAIL,
            "No SPF record. Receivers cannot tell which servers may send as this domain.",
        )
    if len(spf) > 1:
        return Check(
            "SPF",
            Verdict.FAIL,
            f"{len(spf)} SPF records, and two is a permanent error rather than a merge — "
            "receivers give up. Combine them into one record.",
        )
    return Check("SPF", Verdict.PASS, "Exactly one SPF record, which is what receivers require.")


def _dkim(found: tuple[str, list[str] | None] | None) -> Check:
    if found is None:
        return Check(
            "DKIM",
            Verdict.UNKNOWN,
            "No key at the selectors tried ("
            + ", ".join(DKIM_SELECTORS)
            + "). DNS cannot list selectors, so this is not proof there is none.",
        )
    selector, records = found
    if records is None:
        return Check(
            "DKIM", Verdict.UNKNOWN, "The DNS lookup failed, so this could not be checked."
        )
    return Check(
        "DKIM",
        Verdict.PASS,
        f"A key is published at the {selector} selector. Whether mail is actually signed with "
        "it only a received message can show — check Show original in Gmail.",
    )


def _dmarc(records: list[str] | None) -> Check:
    if records is None:
        return Check(
            "DMARC", Verdict.UNKNOWN, "The DNS lookup failed, so this could not be checked."
        )
    dmarc = [record for record in records if record.lower().startswith("v=dmarc1")]
    if not dmarc:
        return Check("DMARC", Verdict.FAIL, "No DMARC record at _dmarc for this domain.")

    record = dmarc[0]
    policy = "unknown"
    for part in record.split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "p":
            policy = f"p={value.strip()}"
            break

    if "rua=" not in record.lower():
        return Check(
            "DMARC",
            Verdict.WARN,
            f"Published ({policy}) but with no rua= address, so no aggregate reports arrive. "
            "They are the one deliverability signal that works at low volume.",
        )
    # The address itself is deliberately not echoed: it is somebody's mailbox.
    return Check("DMARC", Verdict.PASS, f"Published ({policy}) with aggregate reports enabled.")


def _one_click(configured: bool) -> Check:
    if configured:
        return Check(
            "One-click unsubscribe",
            Verdict.PASS,
            "A one-click URL is configured, so List-Unsubscribe-Post is sent (RFC 8058).",
        )
    return Check(
        "One-click unsubscribe",
        Verdict.FAIL,
        "No one-click URL: set PUBLIC_API_URL so List-Unsubscribe can carry an https target. "
        "Until then only the mailto is sent, which bulk senders are no longer judged on alone.",
    )


def _shared(name: str) -> Check:
    """The verdict for a domain somebody else owns.

    A pass, because there is genuinely nothing wrong and nothing to do: the provider publishes
    SPF, signs with DKIM and sets DMARC for its own domain, and does it properly. The detail
    carries the two things that *are* worth knowing, because they are easy to assume away.
    """
    return Check(
        "Authentication",
        Verdict.PASS,
        f"{name} is your provider's domain, so SPF, DKIM and DMARC are theirs and are already "
        "correct — there is nothing for you to publish. Two consequences worth knowing: you "
        "share the domain's reputation with everyone else sending from it, and you cannot add "
        "it to Postmaster Tools because it is not your domain. Sending from a domain you own "
        "gives you both of those.",
    )


def check_domain(
    domain: str, *, resolver: TxtResolver, unsubscribe_configured: bool
) -> DomainReport:
    """Everything checkable about one sending domain, from outside.

    A provider-owned domain (`gmail.com` and friends) is reported as the provider's business
    rather than run through the DNS checklist — see `PROVIDER_DOMAINS`.
    """
    name = domain.strip().lower().rstrip(".")

    if name in PROVIDER_DOMAINS:
        shared = _shared(name)
        # The same verdict in all three slots rather than a different shape of report, so every
        # caller -- the admin screen, the go-live script -- renders one thing and cannot forget
        # to handle a special case. One-click unsubscribe is still ours to get right, because it
        # is our header and not the provider's.
        return DomainReport(
            domain=name,
            spf=shared,
            dkim=shared,
            dmarc=shared,
            one_click=_one_click(unsubscribe_configured),
            provider_owned=True,
        )

    dkim_found: tuple[str, list[str] | None] | None = None
    for selector in DKIM_SELECTORS:
        answer = resolver.txt(f"{selector}._domainkey.{name}")
        if answer is None:
            dkim_found = (selector, None)
            break
        if any(record.lower().startswith("v=dkim1") for record in answer):
            dkim_found = (selector, answer)
            break

    return DomainReport(
        domain=name,
        spf=_spf(resolver.txt(name)),
        dkim=_dkim(dkim_found),
        dmarc=_dmarc(resolver.txt(f"_dmarc.{name}")),
        one_click=_one_click(unsubscribe_configured),
    )


class DnsTxtResolver:
    """TXT lookups through `dnspython`, cached for the life of the instance.

    Cached because a report asks about several names per domain and an operator will press
    refresh. The import is lazy and the timeouts are explicit, matching the importer's MX check,
    which is the other place in this codebase that talks to DNS.
    """

    def __init__(self, timeout: float = DNS_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout
        self._cache: dict[str, list[str] | None] = {}

    def txt(self, name: str) -> list[str] | None:
        if name not in self._cache:
            self._cache[name] = self._lookup(name)
        return self._cache[name]

    def _lookup(self, name: str) -> list[str] | None:
        import dns.exception
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.timeout = self._timeout
        resolver.lifetime = self._timeout
        try:
            answer = resolver.resolve(name, "TXT")
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            # A definitive "nothing here", which is a real answer rather than a failure.
            return []
        except dns.exception.DNSException:
            # A timeout or SERVFAIL. Not evidence of absence — the caller reports UNKNOWN.
            return None
        # A TXT record arrives as one or more byte strings that the owner intended to be joined.
        return [
            "".join(part.decode("utf-8", "replace") for part in record.strings) for record in answer
        ]
