"""Researching one lead, end to end.

`ARCHITECTURE_TARGET.md` §12. The order of operations is the design:

1. **Check the domain cache.** Several founders at one company share one company page, so the
   expensive work is done per *domain*, not per lead.
2. **Fetch the company's own pages** — home plus a few same-site pages — through the hardened
   fetcher.
3. **Extract and budget** the text.
4. **Synthesise** structured findings with the LLM, routed to `research_company`, which carries
   no lead PII and so may use a provider that trains on prompts.
5. **Verify every hook against the fetched text**, by code. This is the step that makes it
   research rather than invention.
6. **Search only if that was thin**, at most once per lead, against a monthly credit budget.

What this replaces: a prompt that said "Base analysis on the company name" and produced
confident paragraphs about funding stage and traction for a company nobody had looked at
(issues A1, A2).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DomainResearch, Lead, Research
from app.services.llm.errors import CapacityExhausted, LLMError
from app.services.llm.router import LLMRouter
from app.services.research.extract import (
    ExtractedPage,
    build_evidence,
    candidate_urls,
    extract,
)
from app.services.research.fetcher import Fetcher
from app.services.research.findings import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Hook,
    ResearchResult,
    Source,
    VerifiedResearch,
    build_user_prompt,
    verify,
)
from app.services.research.net import UnsafeTarget, registrable_domain, validate_url
from app.services.research.search import SearchBudget, SearchProvider

#: How long a company's research may be reused. A quarter, not the original month, and the
#: reasoning is worth keeping because the obvious variable to economise on is the wrong one.
#:
#: **Storage is not the constraint.** `evidence_text` is capped at 20,000 characters and the
#: retention sweep nulls it after `RESEARCH_TEXT_RETENTION_DAYS`; what survives is the findings
#: and the source list, measured at ~2 KB. At 100 emails a day — about 1,100 new companies a
#: month in the worst case where no two leads share one — keeping every company's research
#: **forever** costs ~40 MB a year against 23 GB free, plus a constant ~22 MB for the rolling
#: month of un-purged text. Nothing in that is worth a decision.
#:
#: And nothing ever deleted these rows anyway, so the old 30-day expiry bought the worst of
#: both: we paid the storage permanently and stopped getting the reuse after a month.
#:
#: **Staleness is the constraint.** What a company does, and what it is built with, is the same
#: in April as it was in January. What it announced last week is not — and a cold email opening
#: on a stale "saw you're hiring" is worse than one that opens on nothing, because the recipient
#: knows it is wrong. So the window is long and the *perishable* hooks are dropped early, rather
#: than a short window throwing the durable findings away with them.
CACHE_DAYS = 90

#: Beyond this age a cache hit keeps only its durable hooks. If that leaves none, the row is
#: treated as a miss and the company is researched again — the hooks are the entire value.
PERISHABLE_AFTER_DAYS = 30

#: Hooks that describe a moment rather than a company. These are the most useful hooks we get
#: and the ones that embarrass us first.
PERISHABLE_KINDS: frozenset[str] = frozenset({"news", "hiring", "launch"})
#: At most this many pages per lead, home page included.
MAX_PAGES = 5
#: A page needs at least this much text to be worth including as evidence. Low on purpose: a
#: one-line hiring note is a usable hook, while a cookie banner is not.
MIN_PAGE_CHARS = 80


@dataclass
class ResearchOutcome:
    """What one research run produced, and how it got there."""

    verified: VerifiedResearch | None = None
    from_cache: bool = False
    pages_fetched: int = 0
    fetch_errors: list[str] = field(default_factory=list)
    searched: bool = False
    skipped_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.verified is not None


async def research_lead(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    offer: str,
    router: LLMRouter,
    fetcher: Fetcher,
    search: SearchProvider | None = None,
    budget: SearchBudget | None = None,
    search_policy: str = "when_thin",
    now: datetime | None = None,
) -> ResearchOutcome:
    """Research one lead and persist the result."""
    now = now or datetime.now(tz=UTC)

    if not lead.company_url:
        # Deliberately not guessed from the email domain: for a free-mail address that would
        # send us off to fetch gmail.com (see the importer's same refusal).
        return ResearchOutcome(skipped_reason="lead has no company_url")

    domain = cache_key(lead.company_url)
    cached = await _fresh_cache(session, org_id, domain, now)
    if cached is not None:
        # None means the row had nothing durable left to say, so it is a miss rather than a hit.
        from_cache = _verified_from_cache(cached, now)
        if from_cache is not None:
            await _store_lead_research(
                session, org_id=org_id, lead=lead, verified=from_cache, domain_row=cached
            )
            return ResearchOutcome(verified=from_cache, from_cache=True)

    pages, errors = await _fetch_pages(fetcher, lead.company_url)
    if not pages:
        return ResearchOutcome(fetch_errors=errors, skipped_reason="no page could be fetched")

    evidence = build_evidence(pages, offer_keywords=_keywords(offer))
    fetched_urls = {page.url for page in pages}

    try:
        result = await router.structured(
            ResearchResult,
            task="research_company",
            system=SYSTEM_PROMPT,
            user=build_user_prompt(
                company_name=lead.company_name or domain, offer=offer, evidence=evidence
            ),
            lead_id=lead.id,
        )
    except CapacityExhausted:
        # Pause rather than guess. A lead with no research is recoverable; a lead with invented
        # research is an email we cannot take back.
        raise
    except LLMError as exc:
        return ResearchOutcome(fetch_errors=errors, skipped_reason=f"synthesis failed: {exc}")

    result.sources = [
        Source(url=page.url, fetched_at=now.isoformat(), status=200) for page in pages
    ]
    verified = verify(result, corpus=evidence, fetched_urls=fetched_urls)

    searched = False
    if verified.thin and search is not None and budget is not None and search_policy != "never":
        searched = await _try_search(
            session,
            org_id=org_id,
            lead=lead,
            search=search,
            budget=budget,
            verified=verified,
            now=now,
        )

    domain_row = await _store_domain_research(
        session,
        org_id=org_id,
        domain=domain,
        verified=verified,
        evidence=evidence,
        now=now,
    )
    await _store_lead_research(
        session, org_id=org_id, lead=lead, verified=verified, domain_row=domain_row
    )

    return ResearchOutcome(
        verified=verified,
        pages_fetched=len(pages),
        fetch_errors=errors,
        searched=searched,
    )


async def _fetch_pages(fetcher: Fetcher, company_url: str) -> tuple[list[ExtractedPage], list[str]]:
    """Home page first, then a few same-site pages discovered from it."""
    errors: list[str] = []
    try:
        validate_url(company_url)
    except UnsafeTarget as exc:
        return [], [f"{company_url}: {exc}"]

    home = await fetcher.fetch(company_url)
    if not home.ok:
        return [], [f"{company_url}: {home.error}"]

    pages = [extract(home.html, home.final_url or company_url)]

    for url in candidate_urls(company_url, home.html, limit=MAX_PAGES - 1):
        result = await fetcher.fetch(url)
        if not result.ok:
            # A guessed path that 404s is expected, not a problem worth surfacing.
            errors.append(f"{url}: {result.error}")
            continue

        page = extract(result.html, result.final_url or url)
        # Any page with real text is kept, even a short one. A careers page saying "we are
        # hiring two backend engineers" is one sentence long and is exactly the kind of timely,
        # specific hook worth opening an email with. Dropping it for being "thin" would also
        # drop any hook citing it, because verification checks quotes against the corpus.
        # Thinness is judged on the combined evidence, not page by page.
        if len(page.text) >= MIN_PAGE_CHARS:
            pages.append(page)

    return pages, errors


def _keywords(offer: str) -> list[str]:
    """Words from our offer, used to prioritise paragraphs under the evidence budget."""
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "our",
        "your",
        "are",
        "was",
        "have",
        "has",
        "will",
        "can",
        "all",
        "who",
        "what",
        "them",
        "they",
        "work",
    }
    words = [w.strip(".,;:!?()").lower() for w in (offer or "").split()]
    return [w for w in words if len(w) > 3 and w not in stop][:12]


def cache_key(company_url: str | None) -> str:
    """The cache key for a company: the registrable domain of its **website**.

    Not `leads.domain`, which is the domain of the address the founder writes *from*. A founder
    on Gmail is imported with a warning rather than rejected, so that column is routinely
    `gmail.com` — and keying on it made every Gmail-addressed lead in an org share one cache
    row. The second such lead was then served the first company's findings, which the drafter
    goes on to cite as fact. An email to one company describing another is the worst output
    this system is capable of, and it was one shared key away.

    The website is also the *right* key on its own terms: it is where the findings came from.
    Two founders at one company are one fetch however either of them happens to write to us.
    """
    if not company_url:
        return ""
    host = urlparse(company_url if "//" in company_url else f"//{company_url}").hostname or ""
    return registrable_domain(host) if host else ""


async def _fresh_cache(
    session: AsyncSession, org_id: uuid.UUID, domain: str, now: datetime
) -> DomainResearch | None:
    if not domain:
        return None
    return await session.scalar(
        select(DomainResearch).where(
            DomainResearch.org_id == org_id,
            DomainResearch.domain == domain,
            DomainResearch.expires_at > now,
        )
    )


def _verified_from_cache(row: DomainResearch, now: datetime) -> VerifiedResearch | None:
    """Rebuild the verified result from a cached row, or None if it is too stale to use.

    No re-verification: these hooks were checked against the fetched text when they were
    written, and the text they were checked against is the text that produced them.

    Age is applied to the *hooks*, not to the row. Past `PERISHABLE_AFTER_DAYS` the hooks that
    describe a moment are dropped, because "I saw you are hiring two backend engineers" is a
    good opening line for a month and an embarrassing one after three. If that leaves no hooks
    at all, the caller gets None and researches the company again: the durable findings alone
    are not worth an email.
    """
    findings: dict[str, Any] = dict(row.findings or {})
    hooks: list[dict[str, Any]] = list(findings.get("hooks") or [])
    gaps: list[str] = list(findings.get("gaps") or [])

    kept = [Hook.model_validate(hook) for hook in hooks]
    if kept and now - row.fetched_at > timedelta(days=PERISHABLE_AFTER_DAYS):
        durable = [hook for hook in kept if hook.kind not in PERISHABLE_KINDS]
        if not durable:
            return None
        if len(durable) != len(kept):
            gaps.append(
                f"{len(kept) - len(durable)} time-sensitive hook(s) dropped: this company was "
                f"researched on {row.fetched_at.date().isoformat()}"
            )
        kept = durable

    return VerifiedResearch(
        company=findings.get("company", {}),
        hooks=kept,
        sources=[Source.model_validate(item) for item in (row.sources or [])],
        confidence=row.confidence,
        gaps=gaps,
        rejected=findings.get("rejected", []),
        thin=not kept,
    )


async def _store_domain_research(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    domain: str,
    verified: VerifiedResearch,
    evidence: str,
    now: datetime,
) -> DomainResearch | None:
    if not domain:
        return None

    existing = await session.scalar(
        select(DomainResearch).where(
            DomainResearch.org_id == org_id, DomainResearch.domain == domain
        )
    )
    payload = {
        "company": verified.company.model_dump(),
        "hooks": [hook.model_dump(mode="json") for hook in verified.hooks],
        "gaps": verified.gaps,
        "rejected": verified.rejected,
    }
    sources = [source.model_dump(mode="json") for source in verified.sources]
    # Truncated and purged after 30 days by the retention job: the free tier is 500 MB, and raw
    # text is almost all of the bytes.
    trimmed = evidence[:20_000]

    if existing is not None:
        existing.findings = payload
        existing.sources = sources
        existing.confidence = verified.confidence
        existing.evidence_text = trimmed
        existing.prompt_version = PROMPT_VERSION
        existing.fetched_at = now
        existing.expires_at = now + timedelta(days=CACHE_DAYS)
        await session.flush()
        return existing

    row = DomainResearch(
        org_id=org_id,
        domain=domain,
        findings=payload,
        sources=sources,
        confidence=verified.confidence,
        evidence_text=trimmed,
        prompt_version=PROMPT_VERSION,
        fetched_at=now,
        expires_at=now + timedelta(days=CACHE_DAYS),
    )
    session.add(row)
    await session.flush()
    return row


async def _store_lead_research(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    verified: VerifiedResearch,
    domain_row: DomainResearch | None,
) -> Research:
    """Persist per-lead research, replacing any previous run for this lead."""
    existing = await session.scalar(select(Research).where(Research.lead_id == lead.id))

    # Person context is what we were told, never what we scraped: LinkedIn is stored and
    # displayed only (hard rule 4), so it is recorded here as a reference, not as content.
    person_notes: dict[str, Any] = {
        "job_title": lead.job_title,
        "notes": lead.notes,
        "linkedin_url": lead.linkedin_url,
        "extra": dict(lead.extra or {}),
    }
    hooks = [hook.model_dump(mode="json") for hook in verified.hooks]

    if existing is not None:
        existing.domain_research_id = domain_row.id if domain_row else None
        existing.person_notes = person_notes
        existing.hooks = hooks
        existing.rejected = verified.rejected
        existing.confidence = verified.confidence
        existing.thin = verified.thin
        await session.flush()
        return existing

    row = Research(
        org_id=org_id,
        lead_id=lead.id,
        domain_research_id=domain_row.id if domain_row else None,
        person_notes=person_notes,
        hooks=hooks,
        rejected=verified.rejected,
        confidence=verified.confidence,
        thin=verified.thin,
    )
    session.add(row)
    await session.flush()
    return row


async def _try_search(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    lead: Lead,
    search: SearchProvider,
    budget: SearchBudget,
    verified: VerifiedResearch,
    now: datetime,
) -> bool:
    """One search, only when the site yielded nothing and only within budget.

    Hits are added as sources and gaps, not as hooks: a search snippet has not been through the
    same verification as page text, so it informs a human rather than grounding a claim.
    """
    if not await budget.can_spend(1, now=now):
        verified.gaps.append("search budget exhausted for this month")
        return False

    # The company's own domain, never `lead.domain`: for a founder on Gmail that column says
    # `gmail.com`, and "gmail.com company news" is a wasted credit from a budget of 1,000 a
    # month. With neither a name nor a site there is nothing to search for, so we do not.
    company = lead.company_name or cache_key(lead.company_url)
    if not company:
        verified.gaps.append("no company name or website to search for")
        return False

    query = f"{company} company news"
    hits = await search.search(query, max_results=3)
    await budget.record(
        provider=search.name, credits=1, domain=cache_key(lead.company_url), lead_id=lead.id
    )

    if not hits:
        return True

    verified.sources.extend(Source(url=hit.url, fetched_at=now.isoformat()) for hit in hits)
    verified.gaps.append(
        "search returned results that were not verified against fetched text; "
        "a reviewer should read them before relying on them"
    )
    return True
