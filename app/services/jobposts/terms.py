"""Whether we may poll a source at all, recorded per source (F22, ADR-054). No I/O.

**A recorded human judgement, not a guess.** Each entry below says what was read, when, and what
it permits, in the spirit of `CONTACT_PROVIDERS.md`'s verified-versus-assumed table. Nothing here
infers permission from a URL answering 200: an endpoint being reachable is not an endpoint being
offered, and hard rule 8 ("respect provider terms") is the rule this module exists to make
checkable rather than remembered.

**`UNKNOWN` is a refusal here**, which is the opposite of how the deliverability report treats
it and deliberately so. There, "we could not tell" is reported honestly to a person who then
decides. Here the next step is an automated fetch repeated daily for ever, and "we never checked"
is not a basis for that.

The unit this is decided in is the **source**, because terms are published per service and two
companies on one ATS are covered by the same document. The page fallback is decided per host
instead, and by `robots.txt`, because a company's own careers page has no document we could have
read in advance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Terms:
    """What one source permits, and the evidence for it."""

    source: str
    #: True only when a *public, documented, unauthenticated* endpoint is offered for this.
    pollable: bool
    #: What was read, in words, so the next person can re-read the same thing.
    basis: str
    checked_on: date | None = None

    @property
    def note(self) -> str:
        when = f" (checked {self.checked_on.isoformat()})" if self.checked_on else ""
        return f"{self.basis}{when}"


CHECKED = date(2026, 11, 7)

#: One entry per source. A source absent from here is refused.
TERMS: dict[str, Terms] = {
    "greenhouse": Terms(
        source="greenhouse",
        pollable=True,
        basis=(
            "Greenhouse publishes a job board API for the boards companies choose to make "
            "public, documented for external consumption and requiring no key. The responses "
            "in tests/fixtures/feeds were taken from it."
        ),
        checked_on=CHECKED,
    ),
    "lever": Terms(
        source="lever",
        pollable=True,
        basis=(
            "Lever publishes a postings API per company at api.lever.co/v0/postings, "
            "documented, unauthenticated, and intended for a company's own site to render its "
            "jobs."
        ),
        checked_on=CHECKED,
    ),
    "ashby": Terms(
        source="ashby",
        pollable=True,
        basis=(
            "Ashby publishes a public job-board endpoint per organisation for the same purpose."
        ),
        checked_on=CHECKED,
    ),
    "pasted": Terms(
        source="pasted",
        pollable=False,
        basis=(
            "A posting the user pasted. There is nothing to poll: the user supplied the text, "
            "and nothing in this system fetches a pasted URL (hard rule 4 and ADR-036)."
        ),
        checked_on=CHECKED,
    ),
}


def terms_for(source: str) -> Terms:
    """What this source permits. An unknown source is refused, with a sentence."""
    known = TERMS.get((source or "").strip().lower())
    if known is not None:
        return known
    return Terms(
        source=source,
        pollable=False,
        basis=(
            "We have not read this source's terms. A source is added to app/services/jobposts/"
            "terms.py by a person who has read them, because an endpoint being reachable is not "
            "an endpoint being offered."
        ),
    )


def may_poll(source: str) -> bool:
    return terms_for(source).pollable


def pollable_sources() -> tuple[str, ...]:
    """The sources a board may be watched on. Read by the route and by the screen, so a picker
    cannot offer something the route refuses."""
    return tuple(sorted(name for name, terms in TERMS.items() if terms.pollable))
