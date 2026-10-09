"""Usage accounting against a real database.

Daily caps must survive a restart. An in-process counter would reset and silently hand back a
day's worth of quota the provider has not actually given us, so the production store is
database-backed and this is what proves it.

This is also where §20's "9-12k tokens per lead" estimate stops being a guess: the admin view
reads measured totals from these rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.llm.errors import Outcome
from app.services.llm.usage import DbUsageStore, UsageRecord

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def store(db, org) -> DbUsageStore:
    return DbUsageStore(db, org.id)


def record(entry: str = "groq-llama-70b", **kw) -> UsageRecord:
    defaults = {
        "entry_name": entry,
        "task": "draft_email",
        "tokens_in": 1000,
        "tokens_out": 200,
        "latency_ms": 450,
        "outcome": Outcome.SUCCESS,
    }
    return UsageRecord(**{**defaults, **kw})


class TestRecording:
    async def test_a_call_is_persisted(self, store, db):
        await store.record(record())
        await db.commit()
        assert await store.tokens_today("groq-llama-70b") == 1200

    async def test_a_failure_is_recorded_with_zero_tokens(self, store, db):
        """A provider that fails often is exactly what the admin view needs to show."""
        await store.record(record(tokens_in=0, tokens_out=0, outcome=Outcome.RATE_LIMITED))
        await db.commit()
        assert await store.tokens_today("groq-llama-70b") == 0

    async def test_usage_is_attributed_to_a_lead(self, store, db, lead_factory):
        lead = await lead_factory()
        await store.record(record(lead_id=lead.id))
        await db.commit()

        measured = await store.tokens_per_lead()
        assert measured["leads"] == 1
        assert measured["tokens_per_lead"] == 1200

    async def test_tokens_per_lead_is_the_number_that_decides_when_to_pay(
        self, store, db, lead_factory
    ):
        """Research plus drafting plus validation, summed per lead."""
        for _ in range(3):
            lead = await lead_factory()
            for task in ("research_company", "draft_email", "validate_claims"):
                await store.record(
                    record(task=task, tokens_in=3000, tokens_out=500, lead_id=lead.id)
                )
        await db.commit()

        measured = await store.tokens_per_lead()
        assert measured["leads"] == 3
        assert measured["tokens_per_lead"] == 10_500


class TestHeadroomQueries:
    async def test_each_entry_is_counted_separately(self, store, db):
        """Two models from one provider are two pools of headroom, and caps apply per model."""
        await store.record(record("groq-llama-70b", tokens_in=5000, tokens_out=0))
        await store.record(record("groq-llama-8b", tokens_in=100, tokens_out=0))
        await db.commit()

        assert await store.tokens_today("groq-llama-70b") == 5000
        assert await store.tokens_today("groq-llama-8b") == 100

    async def test_yesterdays_usage_does_not_count_against_today(self, store, db):
        yesterday = datetime.now(tz=UTC) - timedelta(days=1)
        await store.record(record(at=yesterday, tokens_in=99_000, tokens_out=0))
        await store.record(record(tokens_in=1000, tokens_out=0))
        await db.commit()

        assert await store.tokens_today("groq-llama-70b") == 1000

    async def test_the_per_minute_window_only_counts_recent_calls(self, store, db):
        now = datetime.now(tz=UTC)
        await store.record(record(at=now - timedelta(minutes=5), tokens_in=9000, tokens_out=0))
        await store.record(record(at=now - timedelta(seconds=10), tokens_in=500, tokens_out=0))
        await db.commit()

        assert await store.tokens_in_last_minute("groq-llama-70b", now=now) == 500

    async def test_requests_in_the_last_minute_are_counted(self, store, db):
        now = datetime.now(tz=UTC)
        for _ in range(3):
            await store.record(record(at=now - timedelta(seconds=5)))
        await store.record(record(at=now - timedelta(minutes=3)))
        await db.commit()

        assert await store.requests_in_last_minute("groq-llama-70b", now=now) == 3

    async def test_an_unused_entry_reports_zero(self, store):
        assert await store.tokens_today("never-called") == 0


class TestTenancy:
    async def test_another_orgs_usage_is_not_counted(self, db, org):
        """Caps are per org: one tenant's spend must not exhaust another's headroom."""
        from app.db.models import Org

        other = Org(name="Other Tenant")
        db.add(other)
        await db.flush()

        ours = DbUsageStore(db, org.id)
        theirs = DbUsageStore(db, other.id)

        await theirs.record(record(tokens_in=50_000, tokens_out=0))
        await db.commit()

        assert await ours.tokens_today("groq-llama-70b") == 0
        assert await theirs.tokens_today("groq-llama-70b") == 50_000


class TestSurvivesRestart:
    async def test_a_fresh_store_sees_earlier_usage(self, db, org):
        """The reason this is database-backed: an in-process counter would reset and hand back
        quota the provider has not given us."""
        first = DbUsageStore(db, org.id)
        await first.record(record(tokens_in=7000, tokens_out=1000))
        await db.commit()

        # A new store object stands in for a new process.
        second = DbUsageStore(db, org.id)
        assert await second.tokens_today("groq-llama-70b") == 8000
