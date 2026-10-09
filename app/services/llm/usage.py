"""Token accounting, so capacity is measured rather than guessed.

The prototype had none: when Groq's daily quota ran out, work simply failed with no record of
why or of how close it had been (issue E5). `ARCHITECTURE_TARGET.md` §20 puts the per-lead token
estimate at 9-12k and calls it explicitly a planning number "to be replaced by measurements".
This is where the measurements come from.

The store is a protocol with two implementations: an in-memory one for unit tests, and a
database-backed one for real use. Daily caps must survive a restart, so production needs the
database — but the router's selection logic is pure and testable without one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.llm.errors import Outcome


@dataclass(frozen=True)
class UsageRecord:
    """One call, successful or not. Failures are recorded too — a provider that fails often is
    exactly what the admin view needs to show."""

    entry_name: str
    task: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    outcome: Outcome
    lead_id: uuid.UUID | None = None
    at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class UsageStore(Protocol):
    """Where usage goes, and where headroom is read from."""

    async def record(self, usage: UsageRecord) -> None: ...

    async def tokens_today(self, entry_name: str, *, now: datetime | None = None) -> int: ...

    async def tokens_in_last_minute(
        self, entry_name: str, *, now: datetime | None = None
    ) -> int: ...

    async def requests_in_last_minute(
        self, entry_name: str, *, now: datetime | None = None
    ) -> int: ...


class InMemoryUsageStore:
    """For unit tests, and as a fallback when no session is available.

    Deliberately not used in production: a per-process counter resets on restart, which would
    silently hand back a day's worth of quota the provider has not actually given us.
    """

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)

    def _since(self, cutoff: datetime, entry_name: str) -> list[UsageRecord]:
        return [r for r in self.records if r.entry_name == entry_name and r.at >= cutoff]

    async def tokens_today(self, entry_name: str, *, now: datetime | None = None) -> int:
        now = now or datetime.now(tz=UTC)
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        return sum(r.total_tokens for r in self._since(start, entry_name))

    async def tokens_in_last_minute(self, entry_name: str, *, now: datetime | None = None) -> int:
        now = now or datetime.now(tz=UTC)
        return sum(r.total_tokens for r in self._since(now - timedelta(minutes=1), entry_name))

    async def requests_in_last_minute(self, entry_name: str, *, now: datetime | None = None) -> int:
        now = now or datetime.now(tz=UTC)
        return len(self._since(now - timedelta(minutes=1), entry_name))


class DbUsageStore:
    """Database-backed, so daily caps survive restarts and redeploys."""

    def __init__(self, session: AsyncSession, org_id: uuid.UUID) -> None:
        self._session = session
        self._org_id = org_id

    async def record(self, usage: UsageRecord) -> None:
        from app.db.models import LlmUsage

        self._session.add(
            LlmUsage(
                org_id=self._org_id,
                entry_name=usage.entry_name,
                task=usage.task,
                lead_id=usage.lead_id,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                latency_ms=usage.latency_ms,
                outcome=usage.outcome.value,
                at=usage.at,
            )
        )
        await self._session.flush()

    async def _sum_tokens_since(self, entry_name: str, cutoff: datetime) -> int:
        from app.db.models import LlmUsage

        total = await self._session.scalar(
            select(func.coalesce(func.sum(LlmUsage.tokens_in + LlmUsage.tokens_out), 0)).where(
                LlmUsage.org_id == self._org_id,
                LlmUsage.entry_name == entry_name,
                LlmUsage.at >= cutoff,
            )
        )
        return int(total or 0)

    async def tokens_today(self, entry_name: str, *, now: datetime | None = None) -> int:
        now = now or datetime.now(tz=UTC)
        start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
        return await self._sum_tokens_since(entry_name, start)

    async def tokens_in_last_minute(self, entry_name: str, *, now: datetime | None = None) -> int:
        now = now or datetime.now(tz=UTC)
        return await self._sum_tokens_since(entry_name, now - timedelta(minutes=1))

    async def requests_in_last_minute(self, entry_name: str, *, now: datetime | None = None) -> int:
        from app.db.models import LlmUsage

        now = now or datetime.now(tz=UTC)
        count = await self._session.scalar(
            select(func.count()).where(
                LlmUsage.org_id == self._org_id,
                LlmUsage.entry_name == entry_name,
                LlmUsage.at >= now - timedelta(minutes=1),
            )
        )
        return int(count or 0)

    async def tokens_per_lead(self, since: date | None = None) -> dict[str, float]:
        """Measured cost per lead, for `/admin/usage`.

        This is the number that decides when to start paying: §20's 9-12k estimate was always
        meant to be replaced by what we actually observe.
        """
        from app.db.models import LlmUsage

        stmt = select(
            func.coalesce(func.sum(LlmUsage.tokens_in + LlmUsage.tokens_out), 0),
            func.count(func.distinct(LlmUsage.lead_id)),
        ).where(LlmUsage.org_id == self._org_id, LlmUsage.lead_id.is_not(None))
        if since is not None:
            stmt = stmt.where(
                LlmUsage.at >= datetime.combine(since, datetime.min.time(), tzinfo=UTC)
            )

        total_tokens, leads = (await self._session.execute(stmt)).one()
        return {
            "total_tokens": float(total_tokens or 0),
            "leads": float(leads or 0),
            "tokens_per_lead": float(total_tokens or 0) / float(leads) if leads else 0.0,
        }
