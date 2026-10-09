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
from typing import Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: Tavily's free plan, as published. Overridable via TAVILY_MONTHLY_CREDIT_BUDGET.
DEFAULT_MONTHLY_BUDGET = 1000
#: Warn in the UI at this fraction of the budget.
WARN_AT = 0.9


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
        """Run one basic search. A failure returns nothing rather than raising.

        Search is a best-effort supplement: an outage must degrade the research result, not
        fail the lead.
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
        except httpx.HTTPError:
            return []

        if response.status_code != 200:
            return []

        try:
            payload = response.json()
        except ValueError:
            return []

        return [
            SearchHit(
                title=str(item.get("title") or "")[:300],
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or "")[:1000],
            )
            for item in (payload.get("results") or [])
            if item.get("url")
        ]


class SearchBudget:
    """Counts credits for the current calendar month."""

    def __init__(
        self, session: AsyncSession, org_id: uuid.UUID, *, monthly: int = DEFAULT_MONTHLY_BUDGET
    ) -> None:
        self._session = session
        self._org_id = org_id
        self.monthly = monthly

    async def used_this_month(self, *, now: datetime | None = None) -> int:
        from app.db.models import SearchUsage

        now = now or datetime.now(tz=UTC)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = await self._session.scalar(
            select(func.coalesce(func.sum(SearchUsage.credits), 0)).where(
                SearchUsage.org_id == self._org_id, SearchUsage.at >= start
            )
        )
        return int(total or 0)

    async def remaining(self, *, now: datetime | None = None) -> int:
        return max(self.monthly - await self.used_this_month(now=now), 0)

    async def can_spend(self, credits: int = 1, *, now: datetime | None = None) -> bool:
        return await self.remaining(now=now) >= credits

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
    """Tavily when a key is set, otherwise nothing. Absence is a supported configuration."""
    key = str(getattr(settings, "TAVILY_API_KEY", "") or "").strip()
    if not key:
        return NullSearchProvider()
    return TavilySearchProvider(key, client)
