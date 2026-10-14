"""Researching a lead end to end, against a real database.

Needs a database for the domain cache — several founders at one company share one company page,
and proving the second lead reuses the first's work is the point.

No network: the fetcher is driven by respx and the LLM by a fake router.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
import respx

from app.db.models import DomainResearch, Research
from app.services.research.fetcher import Fetcher
from app.services.research.findings import ResearchResult
from app.services.research.search import SearchBudget, SearchHit
from app.services.research.service import research_lead

pytestmark = pytest.mark.asyncio(loop_scope="session")

HOME = """
<html><head><title>Acme Analytics</title></head><body>
<nav><a href="/about">About</a><a href="/careers">Careers</a></nav>
<main><p>Acme Analytics builds forecasting tools for mid-sized logistics firms in India.</p></main>
</body></html>
"""
CAREERS = """
<html><body><main>
<p>We are hiring two backend engineers to work on our data ingestion
pipeline this quarter.</p>
</main></body></html>
"""

OFFER = "Fixed-scope data pipeline and dashboard projects delivered to a deadline"


class FakeRouter:
    """Returns a queued ResearchResult instead of calling a model."""

    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.prompts: list[str] = []

    async def structured(self, schema, *, task, system, user, lead_id=None, **kw):
        self.prompts.append(user)
        result = self.results.pop(0) if self.results else ResearchResult()
        if isinstance(result, Exception):
            raise result
        return result


def grounded_result() -> ResearchResult:
    """What a well-behaved model returns: hooks quoting the fetched text verbatim."""
    return ResearchResult.model_validate(
        {
            "company": {"what_they_do": "Forecasting tools for logistics firms"},
            "hooks": [
                {
                    "text": "They are hiring backend engineers for a data pipeline",
                    "kind": "hiring",
                    "source_url": "https://acme.example/careers",
                    "snippet": (
                        "hiring two backend engineers to work on our data ingestion pipeline"
                    ),
                    "strength": 0.8,
                }
            ],
            "confidence": 0.8,
        }
    )


@pytest.fixture
def no_dns(monkeypatch):
    monkeypatch.setattr("app.services.research.net.resolve", lambda host: ["93.184.216.34"])


@pytest_asyncio.fixture(loop_scope="session")
async def fetcher(no_dns):
    async with httpx.AsyncClient() as client:
        yield Fetcher(client=client, host_delay=0.0, respect_robots=False)


@pytest_asyncio.fixture(loop_scope="session")
async def site():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://acme.example/").mock(return_value=httpx.Response(200, html=HOME))
        mock.get("https://acme.example/about").mock(return_value=httpx.Response(404))
        mock.get("https://acme.example/careers").mock(
            return_value=httpx.Response(200, html=CAREERS)
        )
        mock.get(url__regex=r"https://acme\.example/.*").mock(return_value=httpx.Response(404))
        yield mock


@pytest_asyncio.fixture(loop_scope="session")
async def lead(lead_factory):
    return await lead_factory(
        state="NEW",
        company_name="Acme Analytics",
        company_url="https://acme.example",
        domain="acme.example",
        job_title="Founder",
    )


class TestHappyPath:
    async def test_a_grounded_hook_is_kept_and_stored(self, db, org, lead, fetcher, site):
        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(grounded_result()),
            fetcher=fetcher,
        )
        await db.commit()

        assert outcome.succeeded
        assert outcome.verified.has_evidence
        assert not outcome.verified.thin

        stored = await db.scalar(
            __import__("sqlalchemy").select(Research).where(Research.lead_id == lead.id)
        )
        assert stored is not None
        assert len(stored.hooks) == 1
        assert stored.confidence > 0.5

    async def test_person_context_is_recorded_without_fetching_linkedin(
        self, db, org, lead_factory, fetcher, site
    ):
        """A2 asked for person research. LinkedIn stays stored-and-shown only (hard rule 4),
        so the person context is what we were *told*, not what we scraped."""
        lead = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="acme.example",
            job_title="Founder",
            notes="Met at a conference",
            linkedin_url="https://linkedin.com/in/asha",
        )
        await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(grounded_result()),
            fetcher=fetcher,
        )
        await db.commit()

        stored = await db.scalar(
            __import__("sqlalchemy").select(Research).where(Research.lead_id == lead.id)
        )
        assert stored.person_notes["job_title"] == "Founder"
        assert stored.person_notes["notes"] == "Met at a conference"
        assert stored.person_notes["linkedin_url"] == "https://linkedin.com/in/asha"

        # linkedin.com is on the blocked-host list, so no request to it is even attempted.
        assert not any("linkedin" in str(call.request.url) for call in site.calls)

    async def test_the_prompt_carries_no_lead_pii(self, db, org, lead, fetcher, site):
        """This task is routed to providers that may train on prompts."""
        router = FakeRouter(grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        prompt = router.prompts[0]
        assert "Acme Analytics" in prompt
        assert lead.email_norm not in prompt
        assert (lead.full_name or "zzz") not in prompt


class TestGroundingIsEnforced:
    async def test_an_invented_hook_is_dropped_and_the_lead_is_thin(
        self, db, org, lead, fetcher, site
    ):
        """A1's fix, end to end: a confident model that invents is reduced to nothing."""
        invented = ResearchResult.model_validate(
            {
                "hooks": [
                    {
                        "text": "They just raised a Series A",
                        "source_url": "https://acme.example/",
                        "snippet": "Acme Analytics closed a twelve million dollar Series A round",
                        "strength": 0.9,
                    }
                ],
                "confidence": 0.95,
            }
        )
        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(invented),
            fetcher=fetcher,
        )
        await db.commit()

        assert outcome.verified.hooks == []
        assert outcome.verified.thin is True
        assert outcome.verified.confidence <= 0.2
        assert len(outcome.verified.rejected) == 1

    async def test_the_rejection_is_stored_so_a_weak_model_is_visible(
        self, db, org, lead, fetcher, site
    ):
        invented = ResearchResult.model_validate(
            {
                "hooks": [
                    {
                        "text": "Made up",
                        "source_url": "https://acme.example/",
                        "snippet": "a sentence that appears nowhere on the fetched pages at all",
                    }
                ],
                "confidence": 0.9,
            }
        )
        await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(invented),
            fetcher=fetcher,
        )
        await db.commit()

        stored = await db.scalar(
            __import__("sqlalchemy").select(Research).where(Research.lead_id == lead.id)
        )
        assert stored.thin is True
        assert len(stored.rejected) == 1


class TestDomainCache:
    async def test_a_second_lead_at_the_same_company_reuses_the_research(
        self, db, org, lead, lead_factory, fetcher, site
    ):
        """The reason the cache is per domain: a sheet routinely has several founders at one
        company, and their company page is the same page."""
        router = FakeRouter(grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()
        calls_after_first = len(site.calls)

        colleague = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="acme.example",
        )
        outcome = await research_lead(
            db, org_id=org.id, lead=colleague, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        assert outcome.from_cache is True
        assert outcome.verified.has_evidence
        assert len(site.calls) == calls_after_first, "no page was fetched again"
        assert len(router.prompts) == 1, "the model was not called again"

    async def test_two_companies_sharing_an_email_domain_do_not_share_research(
        self, db, org, lead_factory, fetcher
    ):
        """The cache key must be the company *website*, not the address the founder writes from.

        A founder on Gmail is imported with a warning, not rejected (hard rule: a founder may
        genuinely use one), so `leads.domain` stays `gmail.com`. Keying the cache on that made
        every Gmail-addressed lead in the org share **one** row — so the second one was served
        the first company's findings, and the drafter cites those findings as fact. An email to
        one company describing another is not a quality problem, it is the worst output this
        system can produce.
        """
        other_home = HOME.replace("Acme Analytics", "Zenith Logistics")
        with respx.mock(assert_all_called=False) as mock:
            mock.get("https://acme.example/").mock(return_value=httpx.Response(200, html=HOME))
            mock.get("https://zenith.example/").mock(
                return_value=httpx.Response(200, html=other_home)
            )
            mock.get(url__regex=r"https://(acme|zenith)\.example/.+").mock(
                return_value=httpx.Response(404)
            )

            first = await lead_factory(
                company_name="Acme Analytics",
                company_url="https://acme.example",
                domain="gmail.com",
            )
            second = await lead_factory(
                company_name="Zenith Logistics",
                company_url="https://zenith.example",
                domain="gmail.com",
            )
            router = FakeRouter(grounded_result(), grounded_result())

            await research_lead(
                db, org_id=org.id, lead=first, offer=OFFER, router=router, fetcher=fetcher
            )
            await db.commit()
            outcome = await research_lead(
                db, org_id=org.id, lead=second, offer=OFFER, router=router, fetcher=fetcher
            )
            await db.commit()

        assert outcome.from_cache is False, "the second company was served the first's research"
        assert len(router.prompts) == 2, "the second company was never actually researched"

    async def test_the_same_company_is_cached_whatever_the_founders_write_from(
        self, db, org, lead_factory, fetcher, site
    ):
        """The other half of the same key: two founders at one company, one on the company
        domain and one on Gmail, are one company and must be researched once."""
        router = FakeRouter(grounded_result(), grounded_result())
        on_domain = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="acme.example",
        )
        on_gmail = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="gmail.com",
        )

        await research_lead(
            db, org_id=org.id, lead=on_domain, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()
        outcome = await research_lead(
            db, org_id=org.id, lead=on_gmail, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        assert outcome.from_cache is True
        assert len(router.prompts) == 1, "the same company was researched twice"

    async def test_an_expired_cache_entry_is_refetched(self, db, org, lead, fetcher, site):
        from datetime import UTC, datetime, timedelta

        router = FakeRouter(grounded_result(), grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        row = await db.scalar(
            __import__("sqlalchemy")
            .select(DomainResearch)
            .where(DomainResearch.domain == "acme.example")
        )
        row.expires_at = datetime.now(tz=UTC) - timedelta(days=1)
        await db.commit()

        outcome = await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        assert outcome.from_cache is False
        assert len(router.prompts) == 2

    async def test_a_stale_hit_keeps_what_lasts_and_drops_what_does_not(
        self, db, org, lead, lead_factory, fetcher, site
    ):
        """Past 30 days a cache hit is still used, minus the hooks that describe a moment.

        "I saw you are hiring two backend engineers" is a good opening line for a month and an
        embarrassing one after three — the recipient knows whether it is still true. What the
        company *does* has not changed, so throwing that away with it would be the expensive
        half of the same mistake.
        """
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        router = FakeRouter(grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        row = await db.scalar(select(DomainResearch).where(DomainResearch.domain == "acme.example"))
        # One durable hook beside the perishable one the fixture produces.
        findings = dict(row.findings)
        hooks = list(findings["hooks"])
        durable = {**hooks[0], "kind": "product"}
        perishable = {**hooks[0], "kind": "hiring"}
        findings["hooks"] = [durable, perishable]
        row.findings = findings
        row.fetched_at = datetime.now(tz=UTC) - timedelta(days=45)
        await db.commit()

        colleague = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="acme.example",
        )
        outcome = await research_lead(
            db, org_id=org.id, lead=colleague, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        assert outcome.from_cache is True, "a stale company is not an unknown company"
        kinds = {hook.kind for hook in outcome.verified.hooks}
        assert "product" in kinds
        assert "hiring" not in kinds, "a three-month-old hiring hook was offered as current"
        assert any("time-sensitive" in gap for gap in outcome.verified.gaps), (
            "the reviewer cannot see that something was dropped"
        )
        assert len(router.prompts) == 1, "the company was researched again for no reason"

    async def test_a_stale_hit_with_nothing_durable_left_is_a_miss(
        self, db, org, lead, lead_factory, fetcher, site
    ):
        """If every hook was perishable, dropping them leaves findings not worth an email, so
        the company is researched again rather than mailed on the strength of nothing."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        router = FakeRouter(grounded_result(), grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        row = await db.scalar(select(DomainResearch).where(DomainResearch.domain == "acme.example"))
        findings = dict(row.findings)
        findings["hooks"] = [{**hook, "kind": "news"} for hook in findings["hooks"]]
        row.findings = findings
        row.fetched_at = datetime.now(tz=UTC) - timedelta(days=45)
        await db.commit()

        colleague = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="acme.example",
        )
        outcome = await research_lead(
            db, org_id=org.id, lead=colleague, offer=OFFER, router=router, fetcher=fetcher
        )

        assert outcome.from_cache is False
        assert len(router.prompts) == 2, "the stale row was used instead of being replaced"

    async def test_a_hit_inside_the_month_keeps_everything(
        self, db, org, lead, lead_factory, fetcher, site
    ):
        """The common case must not pay for the stale one: a fresh hit is untouched."""
        router = FakeRouter(grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        colleague = await lead_factory(
            company_name="Acme Analytics",
            company_url="https://acme.example",
            domain="acme.example",
        )
        outcome = await research_lead(
            db, org_id=org.id, lead=colleague, offer=OFFER, router=router, fetcher=fetcher
        )

        assert outcome.from_cache is True
        assert len(outcome.verified.hooks) == 1
        assert not any("time-sensitive" in gap for gap in outcome.verified.gaps)

    async def test_another_orgs_cache_is_not_reused(self, db, org, lead, fetcher, site):
        """Cached research is per org: one tenant must not read another's work."""
        from app.db.models import Org

        router = FakeRouter(grounded_result(), grounded_result())
        await research_lead(
            db, org_id=org.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        await db.commit()

        other = Org(name="Other")
        db.add(other)
        await db.flush()

        outcome = await research_lead(
            db, org_id=other.id, lead=lead, offer=OFFER, router=router, fetcher=fetcher
        )
        assert outcome.from_cache is False


class TestFailuresDegradeGracefully:
    async def test_a_lead_with_no_company_url_is_skipped_not_guessed(
        self, db, org, lead_factory, fetcher
    ):
        """Guessing from the email domain would send us off to fetch gmail.com."""
        lead = await lead_factory(company_name="Acme", company_url=None, domain="gmail.com")
        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(grounded_result()),
            fetcher=fetcher,
        )
        assert not outcome.succeeded
        assert "no company_url" in (outcome.skipped_reason or "")

    async def test_an_unreachable_site_does_not_fail_the_lead(self, db, org, lead, fetcher, no_dns):
        with respx.mock:
            respx.get(url__regex=r"https://acme\.example.*").mock(
                side_effect=httpx.ConnectError("refused")
            )
            outcome = await research_lead(
                db,
                org_id=org.id,
                lead=lead,
                offer=OFFER,
                router=FakeRouter(grounded_result()),
                fetcher=fetcher,
            )
        assert not outcome.succeeded
        assert "no page could be fetched" in (outcome.skipped_reason or "")

    async def test_an_unsafe_company_url_is_refused_before_any_request(
        self, db, org, lead_factory, fetcher
    ):
        lead = await lead_factory(
            company_name="Evil", company_url="http://169.254.169.254/", domain="x.example"
        )
        with respx.mock:
            outcome = await research_lead(
                db,
                org_id=org.id,
                lead=lead,
                offer=OFFER,
                router=FakeRouter(grounded_result()),
                fetcher=fetcher,
            )
            assert respx.calls.call_count == 0
        assert not outcome.succeeded

    async def test_exhausted_llm_capacity_propagates_rather_than_guessing(
        self, db, org, lead, fetcher, site
    ):
        """A lead with no research is recoverable; a lead with invented research is an email
        we cannot take back."""
        from app.services.llm.errors import CapacityExhausted

        with pytest.raises(CapacityExhausted):
            await research_lead(
                db,
                org_id=org.id,
                lead=lead,
                offer=OFFER,
                router=FakeRouter(CapacityExhausted("all providers down")),
                fetcher=fetcher,
            )


class TestSearchFallback:
    class FakeSearch:
        name = "tavily"

        def __init__(self, *hits: SearchHit) -> None:
            self.hits = list(hits)
            self.queries: list[str] = []

        async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
            self.queries.append(query)
            return self.hits

    def _thin(self) -> ResearchResult:
        return ResearchResult(hooks=[], confidence=0.1)

    async def test_search_runs_only_when_the_site_yielded_nothing(
        self, db, org, lead, fetcher, site
    ):
        search = self.FakeSearch(SearchHit("News", "https://news.example/acme", "Acme grew"))
        budget = SearchBudget(db, org.id, monthly=10)

        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(self._thin()),
            fetcher=fetcher,
            search=search,
            budget=budget,
        )
        await db.commit()

        assert outcome.searched is True
        assert len(search.queries) == 1
        assert await budget.used_this_month() == 1

    async def test_search_is_skipped_when_the_site_gave_a_hook(self, db, org, lead, fetcher, site):
        """The company's own site is better evidence and costs nothing."""
        search = self.FakeSearch()
        budget = SearchBudget(db, org.id, monthly=10)

        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(grounded_result()),
            fetcher=fetcher,
            search=search,
            budget=budget,
        )
        assert outcome.searched is False
        assert search.queries == []

    async def test_an_exhausted_budget_degrades_instead_of_failing(
        self, db, org, lead, fetcher, site
    ):
        """Tavily's free plan is 1,000 credits a month; one large import could spend the lot."""
        search = self.FakeSearch()
        budget = SearchBudget(db, org.id, monthly=1)
        await budget.record(provider="tavily", credits=1)
        await db.commit()

        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(self._thin()),
            fetcher=fetcher,
            search=search,
            budget=budget,
        )
        assert outcome.searched is False
        assert search.queries == []
        assert any("budget exhausted" in gap for gap in outcome.verified.gaps)

    async def test_search_results_are_sources_not_hooks(self, db, org, lead, fetcher, site):
        """A search snippet has not been through the same verification as page text, so it
        informs a reviewer rather than grounding a claim."""
        search = self.FakeSearch(SearchHit("News", "https://news.example/acme", "Acme grew"))
        budget = SearchBudget(db, org.id, monthly=10)

        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(self._thin()),
            fetcher=fetcher,
            search=search,
            budget=budget,
        )
        assert outcome.verified.hooks == []
        assert any("news.example" in s.url for s in outcome.verified.sources)
        assert any("not verified" in gap for gap in outcome.verified.gaps)

    async def test_the_policy_can_switch_search_off(self, db, org, lead, fetcher, site):
        search = self.FakeSearch()
        budget = SearchBudget(db, org.id, monthly=10)
        outcome = await research_lead(
            db,
            org_id=org.id,
            lead=lead,
            offer=OFFER,
            router=FakeRouter(self._thin()),
            fetcher=fetcher,
            search=search,
            budget=budget,
            search_policy="never",
        )
        assert outcome.searched is False
