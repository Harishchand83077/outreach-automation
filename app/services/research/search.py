"""Web search, behind an interface and a budget.

`ARCHITECTURE_TARGET.md` §12. Search is a *fallback*, not the primary source: the company's own
site is better evidence and costs nothing. Tavily's free plan is 1,000 credits a month, so an
unbudgeted search per lead would exhaust it in a single large import.

Hence the default policy `when_thin` — search only when the company's own pages yielded no
usable hook. One search per lead, cached per domain, counted against the monthly budget. With
no key, or with the budget spent, research degrades to site text only rather than failing.

One key per org (hard rule 8). Never multiple accounts to stretch a free tier.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: Credits one workspace may spend per provider per calendar month. Tavily's published free plan
#: is 1,000 a month, which is where the number comes from; applied to the others it is a ceiling
#: of ours rather than a claim about their plans, which we cannot read. Overridable via
#: TAVILY_MONTHLY_CREDIT_BUDGET.
DEFAULT_MONTHLY_BUDGET = 1000
#: Warn in the UI at this fraction of the budget.
WARN_AT = 0.9


class SearchUnavailable(Exception):
    """This provider could not answer. Try the next member of the chain; keep the key."""


class SearchKeyRefused(SearchUnavailable):
    """This provider refused the key itself. Stop using it until somebody fixes it.

    A subclass, so a caller that only wants "this one did not answer" needs one `except` while
    the chain, which has to record the refusal against the credential, can tell them apart. The
    two were one thing before F17, and collapsing them is how a key is recorded as dead because
    a provider had a bad afternoon.
    """


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    """Swappable, so adding a provider later does not touch the research pipeline."""

    name: str

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]: ...


class NullSearchProvider:
    """No key, no search. Research falls back to the company's own pages."""

    name = "none"

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        return []


class TavilySearchProvider:
    """Tavily's basic search: one credit per call."""

    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, client: httpx.AsyncClient, *, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        """Run one basic search.

        **A failure raises rather than returning nothing**, which is a change F17 forced and is
        the more important half of it. With one provider the two were the same answer: search is
        a best-effort supplement, so an outage degraded the research result instead of failing
        the lead, and returning an empty list said that in the fewest lines.

        With a chain they are opposite answers. "Nobody writes about this company" is worth
        caching and worth stopping on; "this provider is down" must move to the next member and
        must never be stored. A provider reporting an outage as an empty list makes those
        indistinguishable -- the job-post intake's expensive lesson, where an unknown source with
        a plausible payload returned zero postings and read as an empty board. `SearchChain`
        catches both and degrades exactly as this did before.
        """
        try:
            response = await self._client.post(
                self.endpoint,
                json={
                    "api_key": self._api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": False,
                    "include_raw_content": False,
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SearchUnavailable(
                f"tavily could not be reached: {exc.__class__.__name__}"
            ) from exc

        return _read_tavily(_json_or_raise(response, "tavily"))


class BraveSearchProvider:
    """Brave Search's web endpoint.

    **The key travels in a header**, as it does for every provider here and for F9's contact
    adapters: a credential in a query string is a credential in an access log, a proxy log and
    a browser history.
    """

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, client: httpx.AsyncClient, *, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        try:
            response = await self._client.get(
                self.endpoint,
                params={"q": query, "count": max_results},
                headers={"X-Subscription-Token": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SearchUnavailable(
                f"brave could not be reached: {exc.__class__.__name__}"
            ) from exc

        return _read_brave(_json_or_raise(response, "brave"))


class SerperSearchProvider:
    """Serper's Google-backed search endpoint."""

    name = "serper"
    endpoint = "https://google.serper.dev/search"

    def __init__(self, api_key: str, client: httpx.AsyncClient, *, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        try:
            response = await self._client.post(
                self.endpoint,
                json={"q": query, "num": max_results},
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise SearchUnavailable(
                f"serper could not be reached: {exc.__class__.__name__}"
            ) from exc

        return _read_serper(_json_or_raise(response, "serper"))


def _json_or_raise(response: httpx.Response, provider: str) -> dict[str, Any]:
    """One place where an HTTP answer becomes a payload, for every provider.

    **A refused key is told apart from an outage**, because the two deserve opposite handling: a
    401 means stop using this key until somebody fixes it, while a 503 means try the next member
    of the chain and this one again tomorrow. Collapsing them is how a key gets recorded as dead
    because a provider had a bad afternoon.
    """
    if response.status_code in (401, 403):
        raise SearchKeyRefused(f"{provider} refused the key (HTTP {response.status_code})")
    if response.status_code == 429:
        raise SearchUnavailable(f"{provider} is rate limiting or out of credit (HTTP 429)")
    if response.status_code != 200:
        raise SearchUnavailable(f"{provider} answered HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SearchUnavailable(f"{provider} answered something that is not JSON") from exc
    if not isinstance(payload, dict):
        raise SearchUnavailable(f"{provider} answered a {type(payload).__name__}, not an object")
    return payload


def _read_tavily(payload: dict[str, Any]) -> list[SearchHit]:
    """Read Tavily's shape.

    **Each provider's response is read in exactly one function**, so a wrong assumption about a
    shape is one function and one test -- the bargain F9's contact adapters already made
    (ADR-041), and the reason the Lever feed's epoch milliseconds cost one fixture rather than a
    parser rewrite.
    """
    return _hits(payload.get("results"), title="title", url="url", snippet="content")


def _read_brave(payload: dict[str, Any]) -> list[SearchHit]:
    """Brave nests its web results under `web.results` and calls the snippet `description`."""
    web = payload.get("web")
    results = web.get("results") if isinstance(web, dict) else None
    return _hits(results, title="title", url="url", snippet="description")


def _read_serper(payload: dict[str, Any]) -> list[SearchHit]:
    """Serper returns `organic`, with the link under `link`."""
    return _hits(payload.get("organic"), title="title", url="link", snippet="snippet")


def _hits(rows: object, *, title: str, url: str, snippet: str) -> list[SearchHit]:
    """Shared field clipping.

    A row with no URL is dropped: a hit a reviewer cannot open is not evidence of anything.
    """
    if not isinstance(rows, list):
        return []
    out: list[SearchHit] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get(url):
            continue
        out.append(
            SearchHit(
                title=str(row.get(title) or "")[:300],
                url=str(row.get(url) or ""),
                snippet=str(row.get(snippet) or "")[:1000],
            )
        )
    return out


#: Which class serves which name. One mapping, read by the chain builder, so a name in
#: `SEARCH_PROVIDERS` with no adapter here fails at build time rather than being silently
#: skipped -- the job-post intake's lesson about an unknown source returning zero results.
ADAPTERS: dict[str, type[Any]] = {
    TavilySearchProvider.name: TavilySearchProvider,
    BraveSearchProvider.name: BraveSearchProvider,
    SerperSearchProvider.name: SerperSearchProvider,
}


class SearchBudget:
    """Counts credits for the current calendar month, **per provider**.

    Per provider rather than one pooled total, because each member of the chain is a separate
    account with its own allowance -- a shared total would have a workspace's second key refused
    for credits its first key spent. The number is **ours, not theirs**: we cannot read anybody's
    plan, so this is a ceiling that stops a loop spending somebody's month in an afternoon,
    exactly as `DAILY_LOOKUP_BUDGET` is for contact lookups.
    """

    def __init__(
        self, session: AsyncSession, org_id: uuid.UUID, *, monthly: int = DEFAULT_MONTHLY_BUDGET
    ) -> None:
        self._session = session
        self._org_id = org_id
        self.monthly = monthly

    async def used_this_month(
        self, *, provider: str | None = None, now: datetime | None = None
    ) -> int:
        """Credits spent this calendar month, for one provider or for all of them.

        `provider=None` keeps the pooled total, which is what the credits-left readout shows: a
        workspace wants to know what its month has cost, not to add three numbers up itself.
        """
        from app.db.models import SearchUsage

        now = now or datetime.now(tz=UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        query = select(func.coalesce(func.sum(SearchUsage.credits), 0)).where(
            SearchUsage.org_id == self._org_id, SearchUsage.at >= start
        )
        if provider is not None:
            query = query.where(SearchUsage.provider == provider)
        total = await self._session.scalar(query)
        return int(total or 0)

    async def remaining(self, *, provider: str | None = None, now: datetime | None = None) -> int:
        return max(self.monthly - await self.used_this_month(provider=provider, now=now), 0)

    async def can_spend(
        self, credits: int = 1, *, provider: str | None = None, now: datetime | None = None
    ) -> bool:
        return await self.remaining(provider=provider, now=now) >= credits

    async def record(
        self,
        *,
        provider: str,
        credits: int = 1,
        domain: str | None = None,
        lead_id: uuid.UUID | None = None,
    ) -> None:
        from app.db.models import SearchUsage

        self._session.add(
            SearchUsage(
                org_id=self._org_id,
                provider=provider,
                credits=credits,
                query_domain=domain,
                lead_id=lead_id,
            )
        )
        await self._session.flush()

    async def status(self, *, now: datetime | None = None) -> dict[str, object]:
        """For the UI's credits-left readout."""
        used = await self.used_this_month(now=now)
        return {
            "used": used,
            "remaining": max(self.monthly - used, 0),
            "monthly": self.monthly,
            "warn": used >= self.monthly * WARN_AT,
        }


def build_provider(settings: object, client: httpx.AsyncClient) -> SearchProvider:
    """The deployment's own Tavily entry, or nothing. Absence is a supported configuration.

    This is the **last** member of F17's chain and the only one that is not somebody's own key,
    so it is the one with a caveat: its free tier is the deployment's and every workspace using
    it draws on the same thousand credits. `build_chain` puts a workspace's own keys ahead of
    it, which is the whole reason to add one.
    """
    key = str(getattr(settings, "TAVILY_API_KEY", "") or "").strip()
    if not key:
        return NullSearchProvider()
    return TavilySearchProvider(key, client)
