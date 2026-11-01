"""The shared public-company cache (F19, ADR-038, slice S12.6).

The first test is the one that matters. `company_profiles` is the only table in this schema with
no `org_id`, and the exception is only safe while the table holds nothing workspace-specific.
That is asserted by column name rather than trusted, because the first person to add `notes`
here would be leaking one workspace's thinking into another's, and nothing else would notice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select

from app.db.models import CompanyProfile, Org, SearchUsage
from app.services.research import company_cache

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

#: Words that would mean this table had started holding somebody's private work. Not an
#: exhaustive list of bad ideas, but the shapes a reasonable person would reach for.
FORBIDDEN_SUBSTRINGS = (
    "org",
    "lead",
    "contact",
    "message",
    "email",
    "note",
    "draft",
    "campaign",
    "owner",
    "member",
    "token",
    "credential",
)


class TestTheExceptionIsEnforced:
    def test_the_table_has_no_org_column(self):
        """**The ADR-004 exception, asserted.** Every other table in the schema carries `org_id`."""
        columns = {column.name for column in inspect(CompanyProfile).columns}

        assert "org_id" not in columns

    def test_no_column_could_hold_workspace_data(self):
        """The opposite of a tenancy test: this one fails if the table gains a tenant column."""
        columns = {column.name for column in inspect(CompanyProfile).columns}

        for name in columns:
            for forbidden in FORBIDDEN_SUBSTRINGS:
                assert forbidden not in name, (
                    f"company_profiles.{name} looks like workspace data. This table is shared "
                    "across every workspace and holds public facts only (ADR-038)."
                )

    def test_only_the_public_fields_can_be_written(self):
        """A caller passing something else has it dropped rather than stored.

        A caller that could add a field could add a workspace's private note to a shared row.
        """
        assert company_cache.PUBLIC_FIELDS == (
            "name",
            "careers_url",
            "ats_type",
            "ats_slug",
            "size_band",
            "summary",
        )

    async def test_an_unexpected_field_is_ignored(self, db):
        row, _ = await company_cache.apply(
            db,
            domain="acme.test",
            fields={"name": "Acme", "notes": "our best prospect"},
            sources={"name": "https://acme.test"},
            now=NOW,
        )
        await db.commit()

        assert row.name == "Acme"
        assert not hasattr(row, "notes")


class TestSharing:
    async def test_a_second_workspace_reads_the_cache_without_a_fetch(self, db, org):
        """The whole point: a cache hit costs no search credit.

        One workspace researching a company pays for it; the next gets it free, because a
        careers page is a careers page whoever is looking.
        """
        other = Org(name="Another agency")
        db.add(other)
        await db.commit()
        await company_cache.apply(
            db,
            domain="shared.test",
            fields={"name": "Shared", "careers_url": "https://shared.test/jobs"},
            sources={"name": "https://shared.test"},
            page_text="a page",
            now=NOW,
        )
        await db.commit()

        hit = await company_cache.get(db, domain="shared.test")

        assert hit is not None
        assert hit.fresh is True
        assert hit.profile.name == "Shared"
        # No search was recorded for either workspace, because nothing was fetched.
        spent = await db.scalar(select(SearchUsage).where(SearchUsage.org_id == other.id))
        assert spent is None

    async def test_www_and_case_resolve_to_one_row(self, db):
        await company_cache.apply(
            db, domain="Acme.Test", fields={"name": "Acme"}, sources={}, now=NOW
        )
        await db.commit()

        assert await company_cache.get(db, domain="www.acme.test") is not None
        rows = list((await db.scalars(select(CompanyProfile))).all())
        assert len([row for row in rows if row.domain == "acme.test"]) == 1


class TestFreshnessAndWork:
    async def test_an_unchanged_page_does_not_bump_the_version(self, db):
        """An unchanged page costs the fetch and nothing else.

        So a consumer holding version N can trust that N still means what it meant.
        """
        row, created = await company_cache.apply(
            db,
            domain="stable.test",
            fields={"name": "Stable"},
            sources={},
            page_text="same",
            now=NOW,
        )
        await db.commit()
        first_version = row.version

        _row, changed = await company_cache.apply(
            db,
            domain="stable.test",
            fields={"name": "Stable"},
            sources={},
            page_text="same",
            now=NOW + timedelta(days=40),
        )
        await db.commit()

        assert created is True
        assert changed is False
        assert row.version == first_version

    async def test_a_changed_page_bumps_the_version(self, db):
        row, _ = await company_cache.apply(
            db, domain="moving.test", fields={"name": "Old"}, sources={}, page_text="v1", now=NOW
        )
        await db.commit()

        _row, changed = await company_cache.apply(
            db,
            domain="moving.test",
            fields={"name": "New"},
            sources={},
            page_text="v2",
            now=NOW + timedelta(days=40),
        )
        await db.commit()

        assert changed is True
        assert row.version == 2
        assert row.name == "New"

    async def test_a_stale_profile_is_still_returned(self, db):
        """A careers page from five weeks ago beats nothing while a refresh is queued."""
        await company_cache.apply(
            db, domain="old.test", fields={"name": "Old"}, sources={}, now=NOW
        )
        await db.commit()
        row = await db.scalar(select(CompanyProfile).where(CompanyProfile.domain == "old.test"))
        row.fetched_at = NOW - company_cache.MAX_AGE - timedelta(days=1)
        await db.commit()

        hit = await company_cache.get(db, domain="old.test")

        assert hit is not None
        assert hit.fresh is False

    async def test_an_unknown_domain_is_a_miss_rather_than_an_error(self, db):
        assert await company_cache.get(db, domain="never-seen.test") is None

    async def test_an_empty_domain_is_refused(self, db):
        with pytest.raises(ValueError):
            await company_cache.apply(db, domain="   ", fields={}, sources={}, now=NOW)


class TestTheFairShare:
    async def test_a_workspace_within_budget_may_fetch(self, db, org):
        assert await company_cache.may_fetch(db, org_id=org.id, now=NOW) is True

    async def test_a_workspace_over_budget_may_not(self, db, org):
        """One workspace importing a thousand companies must not spend everybody's credits."""
        for _ in range(company_cache.DAILY_FETCH_BUDGET):
            db.add(SearchUsage(org_id=org.id, provider="tavily", credits=1, at=NOW))
        await db.commit()

        assert await company_cache.may_fetch(db, org_id=org.id, now=NOW) is False

    async def test_credits_are_summed_rather_than_searches_counted(self, db, org):
        """One search can cost several credits, and credits are what the provider bills."""
        db.add(
            SearchUsage(
                org_id=org.id,
                provider="tavily",
                credits=company_cache.DAILY_FETCH_BUDGET,
                at=NOW,
            )
        )
        await db.commit()

        assert await company_cache.may_fetch(db, org_id=org.id, now=NOW) is False

    async def test_yesterdays_spend_does_not_count(self, db, org):
        db.add(
            SearchUsage(
                org_id=org.id,
                provider="tavily",
                credits=company_cache.DAILY_FETCH_BUDGET,
                at=NOW - timedelta(days=1),
            )
        )
        await db.commit()

        assert await company_cache.may_fetch(db, org_id=org.id, now=NOW) is True

    async def test_one_workspace_at_its_limit_does_not_block_another(self, db, org):
        """The same rule as the job queue: a limit is per workspace or it is a shared outage."""
        other = Org(name="Unaffected")
        db.add(other)
        await db.commit()
        for _ in range(company_cache.DAILY_FETCH_BUDGET):
            db.add(SearchUsage(org_id=org.id, provider="tavily", credits=1, at=NOW))
        await db.commit()

        assert await company_cache.may_fetch(db, org_id=other.id, now=NOW) is True
