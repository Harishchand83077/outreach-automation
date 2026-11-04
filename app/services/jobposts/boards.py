"""Telling a board's host from a company's own domain (F5 defect, ADR-047).

`job_posts.company_domain` was derived with `urlsplit(apply_url).hostname`, and for every
posting that arrived through a feed that host is **the applicant tracking system's**, not the
company's: `job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`. Verified against the
three real trimmed feed responses in `tests/fixtures/feeds/`, which is where it should have been
caught, because the fixtures have carried those URLs since F5 landed.

Three consumers read that column and all three were wrong for a feed posting:

1. **A contact lookup searched the ATS vendor.** `lookup.run` took the posting's domain, so
   asking who is hiring at a company spent one of the user's own provider credits looking for
   people who work at Greenhouse.
2. **Importing a contact gave the lead `company_url = "https://job-boards.greenhouse.io"`**, which
   the research pipeline then fetched, synthesised and cited. That is ADR-011's defect through a
   different door, and with its worst consequence intact: `cache_key()` reduces that host to
   `greenhouse.io`, so **every** feed-sourced lead in a workspace shared one `domain_research`
   row, and the second was served the first's findings.
3. **The referral matcher looked for connections at "boards"**, taking the first label of the
   host.

What the feed genuinely identifies is the company's **slug on that board** -- `vercel`,
`leverdemo`, `ramp` -- which is a true fact about the company and is what `watched_boards`
already keys on. So the slug is stored, the vendor host is never stored as a domain, and the
company's own domain is resolved where `company_profiles` knows it (F19 records `ats_type` and
`ats_slug` per domain, which is exactly this lookup in reverse).

**Where it cannot be resolved, the domain is NULL and callers refuse rather than guess.** A
posting whose company we cannot name is a posting a person can name in one click; a posting we
silently attribute to Greenhouse is a credit spent, a page researched and an email drafted about
the wrong company.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: Host suffixes belonging to an applicant tracking system, with the source they serve. Matched
#: by suffix rather than equality because Greenhouse answers on both `boards.greenhouse.io` and
#: `job-boards.greenhouse.io` -- the fixtures carry the second, the older docs the first, and a
#: list of exact hosts would have been wrong for one of them.
BOARD_HOSTS: dict[str, str] = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "ashbyhq.io": "ashby",
    "workable.com": "workable",
    "recruitee.com": "recruitee",
    "teamtailor.com": "teamtailor",
    "bamboohr.com": "bamboohr",
    "smartrecruiters.com": "smartrecruiters",
    "myworkdayjobs.com": "workday",
    "icims.com": "icims",
    "jazz.co": "jazzhr",
    "applytojob.com": "jazzhr",
    "breezy.hr": "breezy",
    "personio.de": "personio",
    "join.com": "join",
    "pinpointhq.com": "pinpoint",
    "freshteam.com": "freshteam",
    "zohorecruit.com": "zoho",
    "keka.com": "keka",
    "darwinbox.in": "darwinbox",
    "naukri.com": "naukri",
    "instahyre.com": "instahyre",
    "linkedin.com": "linkedin",
    "indeed.com": "indeed",
    "glassdoor.com": "glassdoor",
    "wellfound.com": "wellfound",
    "angel.co": "wellfound",
    "ycombinator.com": "ycombinator",
}


#: First path segments that name the board's own routing rather than a company. **The
#: migration's backfill imports this**, rather than restating it in SQL: the backfill and the
#: parser are one rule applied to the same strings, and two copies of it would be this project's
#: signature defect with the two halves in different languages.
NOT_A_SLUG: frozenset[str] = frozenset(
    {"embed", "jobs", "job", "postings", "o", "p", "search", "api", "v1", "v0"}
)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def board_vendor(url: str) -> str | None:
    """Which ATS or job board serves this URL, or None if it looks like a company's own site.

    The list is deliberately longer than the three feeds we poll. The polling sources are
    Greenhouse, Lever and Ashby, but a **pasted** posting is whatever the user was reading, and
    the question this answers is not "can we poll it?" -- it is "would storing this host as the
    company's domain be a lie?" For Naukri, LinkedIn and Workday it would be, so they belong
    here even though nothing fetches them.
    """
    host = host_of(url)
    if not host:
        return None
    for suffix, vendor in BOARD_HOSTS.items():
        if host == suffix or host.endswith(f".{suffix}"):
            return vendor
    return None


def is_board_url(url: str) -> bool:
    return board_vendor(url) is not None


def slug_from_url(url: str) -> str | None:
    """The company's identifier on the board, read from the first path segment.

    All three feeds we poll put it there -- `/vercel/jobs/6231864004`,
    `/leverdemo/ad208490-...`, `/ramp/34413f8d-...` -- which is checked against the real trimmed
    responses rather than assumed from documentation.

    Returns None for a URL that is not a board's, and for a board URL with no path: a slug is
    only meaningful as a company's name on a specific board, so a bare host yields nothing. The
    aggregators in `BOARD_HOSTS` that do not organise by company -- a LinkedIn or Indeed posting
    URL names the posting, not the employer -- are therefore expected to produce a slug that is
    not a company, which is why a slug alone never becomes a domain and only ever resolves one
    through `company_profiles`.
    """
    if not is_board_url(url):
        return None
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return None
    first = segments[0].lower()
    # Greenhouse's embedded form is `/embed/job_board?for=<slug>`, and Lever's apply pages add a
    # trailing segment. Neither changes the first segment, so the only case worth excluding is a
    # first segment that is plainly not a company.
    if first in NOT_A_SLUG:
        return None
    return first[:200]


def company_domain_of(url: str) -> str | None:
    """The company's own domain from an apply URL, or None when the URL belongs to a board.

    **None rather than the board's host**, which is the whole fix. A column that holds the ATS
    vendor for every feed posting is worse than an empty one: empty makes a caller ask, and
    `job-boards.greenhouse.io` makes three callers confidently act on it.
    """
    if is_board_url(url):
        return None
    return host_of(url) or None
