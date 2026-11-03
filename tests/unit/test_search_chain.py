"""The ordered search chain, and what it does and does not call (F17, ADR-046).

These are the slice's two acceptance criteria and the two properties that cost money if they are
wrong: **the second provider is not called after a hit**, and a failure is not mistaken for an
answer.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx

from app.services.research import search
from app.services.research.chain import (
    CEILING_SPENT,
    ChainMember,
    SearchChain,
)
from app.services.research.search import (
    SearchHit,
    SearchKeyRefused,
    SearchUnavailable,
)

pytestmark = pytest.mark.asyncio

ORG = uuid.UUID("00000000-0000-4000-8000-00000000000a")


class Recorder:
    """A provider that records every call and answers however the test says."""

    def __init__(self, name: str, *, hits: list[SearchHit] | None = None, raises=None) -> None:
        self.name = name
        self._hits = hits or []
        self._raises = raises
        self.calls: list[str] = []

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        self.calls.append(query)
        if self._raises is not None:
            raise self._raises
        return list(self._hits)


def hit(url: str = "https://news.example/acme") -> SearchHit:
    return SearchHit(title="Acme raised", url=url, snippet="Acme raised a round")


def chain_of(*providers: Recorder) -> SearchChain:
    """A chain with no session and no budget, which is every test that is about ordering."""
    return SearchChain(
        [ChainMember(provider=provider) for provider in providers],
        session=None,  # type: ignore[arg-type]
        org_id=ORG,
        budget=None,
    )


class TestTheShortCircuit:
    async def test_the_second_provider_is_not_called_after_a_hit(self) -> None:
        """The acceptance criterion, and the reason the chain exists: a member that is not
        called is a member that is not billed."""
        first = Recorder("tavily", hits=[hit()])
        second = Recorder("brave", hits=[hit("https://other.example")])

        answer = await chain_of(first, second).run("acme company news")

        assert first.calls == ["acme company news"]
        assert second.calls == []
        assert answer.provider == "tavily"
        assert answer.spent == 1

    async def test_the_order_decides_who_is_called(self) -> None:
        """Reversed, the same two providers produce the other answer. The order is the feature,
        so a test that only ever asserts the first member proves nothing about ordering."""
        first = Recorder("brave", hits=[hit("https://brave.example")])
        second = Recorder("tavily", hits=[hit()])

        answer = await chain_of(first, second).run("acme company news")

        assert answer.provider == "brave"
        assert second.calls == []

    async def test_a_second_member_answers_when_the_first_finds_nothing(self) -> None:
        """An empty answer is not a hit, so the chain keeps going. A provider that does not
        index a small Indian company is the ordinary case this exists for."""
        first = Recorder("tavily", hits=[])
        second = Recorder("brave", hits=[hit()])

        answer = await chain_of(first, second).run("acme company news")

        assert first.calls and second.calls
        assert answer.provider == "brave"
        assert answer.hits
        # Both answered, so both were billed. The ledger counts answers, not hits.
        assert answer.spent == 2

    async def test_a_failing_member_is_skipped_and_the_next_one_answers(self) -> None:
        first = Recorder("tavily", raises=SearchUnavailable("tavily answered HTTP 503"))
        second = Recorder("brave", hits=[hit()])

        answer = await chain_of(first, second).run("acme company news")

        assert answer.provider == "brave"
        assert answer.spent == 1, "an outage is not a credit"
        assert [attempt.answered for attempt in answer.attempts] == [False, True]


class TestWhenNobodyAnswers:
    async def test_an_empty_chain_answers_nothing_and_says_nobody_was_asked(self) -> None:
        """A workspace with no key is a supported configuration, not an error: research
        degrades to the company's own pages, which is where it was before F17."""
        answer = await chain_of().run("acme company news")

        assert not answer.answered
        assert answer.attempts == []
        assert answer.spent == 0

    async def test_every_member_failing_is_not_an_answer(self) -> None:
        """**The distinction the single-provider version could not express.** "Nobody writes
        about this company" is an answer worth caching; "both providers were down" is not, and
        caching it would stop this workspace searching for a week."""
        first = Recorder("tavily", raises=SearchUnavailable("down"))
        second = Recorder("brave", raises=SearchUnavailable("down"))

        answer = await chain_of(first, second).run("acme company news")

        assert not answer.answered
        assert answer.provider is None
        assert len(answer.attempts) == 2

    async def test_a_provider_that_answered_nothing_is_an_answer(self) -> None:
        answer = await chain_of(Recorder("tavily", hits=[])).run("acme company news")

        assert answer.answered
        assert answer.provider == "tavily"
        assert answer.hits == []


class TestTheCeiling:
    class Budget:
        """Only the two methods the chain uses, so the test is about the chain."""

        def __init__(self, *, spent: set[str]) -> None:
            self.spent = spent
            self.recorded: list[str] = []

        async def can_spend(self, credits: int = 1, *, provider=None, now=None) -> bool:
            return provider not in self.spent

        async def record(self, *, provider, credits=1, domain=None, lead_id=None) -> None:
            self.recorded.append(provider)

    async def test_a_member_over_its_ceiling_is_not_called_and_the_next_one_is(self) -> None:
        """Per provider, not pooled: each member is a separate account with its own allowance,
        so a spent first key must not refuse a second key's credits."""
        first = Recorder("tavily", hits=[hit()])
        second = Recorder("brave", hits=[hit("https://brave.example")])
        budget = self.Budget(spent={"tavily"})

        chain = SearchChain(
            [ChainMember(provider=first), ChainMember(provider=second)],
            session=None,  # type: ignore[arg-type]
            org_id=ORG,
            budget=budget,  # type: ignore[arg-type]
        )
        answer = await chain.run("acme company news")

        assert first.calls == [], "a member over its ceiling is skipped before it is called"
        assert answer.provider == "brave"
        assert budget.recorded == ["brave"]

    async def test_the_reason_a_member_was_skipped_is_the_shared_constant(self) -> None:
        """`CEILING_SPENT` is compared against by the research service to decide what a reviewer
        is told. A literal here matched by a literal there is this project's most repeated
        defect, so the producer and the consumer share the name."""
        budget = self.Budget(spent={"tavily"})
        chain = SearchChain(
            [ChainMember(provider=Recorder("tavily"))],
            session=None,  # type: ignore[arg-type]
            org_id=ORG,
            budget=budget,  # type: ignore[arg-type]
        )

        answer = await chain.run("acme company news")

        assert [attempt.error for attempt in answer.attempts] == [CEILING_SPENT]


class TestReadingEachProvidersShape:
    """Every provider's response is read in exactly one function, so a wrong assumption about a
    shape is one function and one test (ADR-041's bargain, reused).

    **None of these three endpoints has been called**: each needs a paid or registered account,
    and hard rule 5 forbids buying one. So these fixtures are each provider's documented shape,
    and `docs/SEARCH_PROVIDERS.md` says which is verified and which is assumed rather than
    implying all three were tested.
    """

    async def test_tavily(self) -> None:
        payload = {
            "results": [
                {"title": "Acme raised", "url": "https://news.example/a", "content": "A round"},
                {"title": "No url", "content": "dropped"},
            ]
        }
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.post(search.TavilySearchProvider.endpoint).mock(
                return_value=httpx.Response(200, json=payload)
            )
            hits = await search.TavilySearchProvider("k", client).search("acme")

        assert [h.url for h in hits] == ["https://news.example/a"]
        assert hits[0].snippet == "A round"

    async def test_brave_nests_its_results_and_renames_the_snippet(self) -> None:
        payload = {
            "web": {
                "results": [
                    {
                        "title": "Acme raised",
                        "url": "https://news.example/b",
                        "description": "A round",
                    }
                ]
            }
        }
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.get(search.BraveSearchProvider.endpoint).mock(
                return_value=httpx.Response(200, json=payload)
            )
            hits = await search.BraveSearchProvider("k", client).search("acme")

        assert [h.url for h in hits] == ["https://news.example/b"]
        assert hits[0].snippet == "A round", "brave calls it `description`"

    async def test_serper_calls_the_url_a_link(self) -> None:
        payload = {"organic": [{"title": "Acme", "link": "https://news.example/c", "snippet": "x"}]}
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.post(search.SerperSearchProvider.endpoint).mock(
                return_value=httpx.Response(200, json=payload)
            )
            hits = await search.SerperSearchProvider("k", client).search("acme")

        assert [h.url for h in hits] == ["https://news.example/c"]

    async def test_a_shape_we_cannot_read_is_empty_rather_than_a_crash(self) -> None:
        """A provider that changed its field names answers nothing, which the chain then treats
        as a provider that found nothing. Honest: we did reach them and they did reply."""
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.get(search.BraveSearchProvider.endpoint).mock(
                return_value=httpx.Response(200, json={"web": "not an object"})
            )
            assert await search.BraveSearchProvider("k", client).search("acme") == []


class TestARefusedKeyIsNotAnOutage:
    """Two statuses that deserve opposite handling. Collapsing them is how a perfectly good key
    is recorded as dead because a provider had a bad afternoon."""

    @pytest.mark.parametrize("code", [401, 403])
    async def test_a_refused_key_raises_the_refusal(self, code: int) -> None:
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.post(search.SerperSearchProvider.endpoint).mock(
                return_value=httpx.Response(code, json={})
            )
            with pytest.raises(SearchKeyRefused):
                await search.SerperSearchProvider("k", client).search("acme")

    @pytest.mark.parametrize("code", [429, 500, 503])
    async def test_an_outage_raises_unavailable_and_not_a_refusal(self, code: int) -> None:
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.post(search.SerperSearchProvider.endpoint).mock(
                return_value=httpx.Response(code, json={})
            )
            with pytest.raises(SearchUnavailable) as caught:
                await search.SerperSearchProvider("k", client).search("acme")

        assert not isinstance(caught.value, SearchKeyRefused)

    async def test_a_network_failure_is_an_outage(self) -> None:
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.get(search.BraveSearchProvider.endpoint).mock(
                side_effect=httpx.ConnectError("no route")
            )
            with pytest.raises(SearchUnavailable):
                await search.BraveSearchProvider("k", client).search("acme")

    async def test_an_answer_that_is_not_json_is_an_outage_not_an_empty_result(self) -> None:
        """The job-post intake's lesson: an unreadable answer reported as zero results is
        indistinguishable from a company nobody writes about."""
        async with httpx.AsyncClient() as client, respx.mock as mock:
            mock.post(search.TavilySearchProvider.endpoint).mock(
                return_value=httpx.Response(200, text="<html>rate limited</html>")
            )
            with pytest.raises(SearchUnavailable):
                await search.TavilySearchProvider("k", client).search("acme")


class TestTheKeyNeverTravelsInAUrl:
    """A credential in a query string is a credential in an access log, a proxy log and a
    browser history. The same assertion F9's contact adapters carry."""

    async def test_brave_sends_the_key_in_a_header(self) -> None:
        async with httpx.AsyncClient() as client, respx.mock as mock:
            route = mock.get(search.BraveSearchProvider.endpoint).mock(
                return_value=httpx.Response(200, json={"web": {"results": []}})
            )
            await search.BraveSearchProvider("secret-key", client).search("acme")

        request = route.calls[0].request
        assert "secret-key" not in str(request.url)
        assert request.headers["X-Subscription-Token"] == "secret-key"

    async def test_serper_sends_the_key_in_a_header(self) -> None:
        async with httpx.AsyncClient() as client, respx.mock as mock:
            route = mock.post(search.SerperSearchProvider.endpoint).mock(
                return_value=httpx.Response(200, json={"organic": []})
            )
            await search.SerperSearchProvider("secret-key", client).search("acme")

        request = route.calls[0].request
        assert "secret-key" not in str(request.url)
        assert request.headers["X-API-KEY"] == "secret-key"


class TestEveryProviderHasAnAdapter:
    def test_each_name_in_the_tuple_can_be_built(self) -> None:
        """`SEARCH_PROVIDERS` and `ADAPTERS` are two lists of the same thing, which is exactly
        the pair that drifts. A name in the tuple with no adapter would be silently skipped by
        `build_chain` -- a provider a workspace saved a key for and that never runs."""
        from app.db.models import SEARCH_PROVIDERS

        assert set(SEARCH_PROVIDERS) <= set(search.ADAPTERS)

    def test_no_adapter_is_a_contact_provider(self) -> None:
        """The two families share one table, so the only thing stopping a search key being
        handed to a contact adapter is that the name sets are disjoint."""
        from app.db.models import CONTACT_PROVIDERS, SEARCH_PROVIDERS

        assert not set(CONTACT_PROVIDERS) & set(SEARCH_PROVIDERS)
