"""The migrations must agree with the models.

This test exists because of a specific, nasty failure. The partial unique index that makes job
enqueueing idempotent — `uq_jobs_kind_dedupe_live`, now `uq_jobs_org_kind_dedupe_live` after
ADR-032 — was created by raw SQL in migration 1 but never declared in the model metadata.
Autogenerate therefore read it as drift and wrote a migration that **dropped** it, silently
removing the guarantee. The queue tests caught it, but only because they happened to cover that
behaviour.

It has since earned its keep a second time: ADR-032 renamed the index, and this file is what
failed in CI to say so.

Comparing metadata against a freshly migrated database catches the whole class: an index, a
constraint or a column that exists in one place and not the other.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import SCHEMA, Base
from tests.integration.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Differences that are noise rather than drift. Alembic reports the SERIAL sequence behind
#: `events.id` as an object it does not manage, on every comparison.
_IGNORED_PREFIXES = ("add_sequence", "remove_sequence")


def _is_real_drift(diff: object) -> bool:
    kind = diff[0] if isinstance(diff, tuple) and diff else ""
    if isinstance(kind, str) and kind.startswith(_IGNORED_PREFIXES):
        return False
    return True


class TestModelsMatchMigrations:
    async def test_there_is_no_drift(self, _migrated_database):
        """A fresh `alembic upgrade head` must produce exactly what the models describe.

        If this fails, do not just regenerate: check *which* direction is wrong. A missing index
        in the metadata makes autogenerate delete a real one.
        """
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as conn:
                diffs = await conn.run_sync(self._compare)
        finally:
            await engine.dispose()

        real = [d for d in diffs if _is_real_drift(d)]
        assert real == [], (
            "models and migrations disagree. Each entry is (operation, object):\n"
            + "\n".join(f"  {d}" for d in real)
        )

    @staticmethod
    def _compare(connection: object) -> list[object]:
        context = MigrationContext.configure(
            connection,  # type: ignore[arg-type]
            opts={
                "include_schemas": True,
                "version_table_schema": SCHEMA,
                "compare_type": True,
            },
        )
        return list(compare_metadata(context, Base.metadata))


class TestTheDedupeIndexSurvives:
    """The specific regression, asserted directly rather than only via behaviour."""

    async def test_the_partial_unique_index_exists(self, db):
        """Renamed by ADR-032, and this guard is how the rename was noticed.

        It was `uq_jobs_kind_dedupe_live`. Scoping the key to the workspace changed the columns,
        so the name changed with them — and this test failed in CI, which is exactly what a guard
        written against a silent index drop is for. The assertion is now *stronger* rather than
        merely renamed: it checks `org_id` is in the definition, because the whole point of the
        change is that the key is per workspace.
        """
        found = await db.scalar(
            text(
                "select indexdef from pg_indexes "
                "where schemaname = 'app' and indexname = 'uq_jobs_org_kind_dedupe_live'"
            )
        )
        assert found is not None, (
            "uq_jobs_org_kind_dedupe_live is missing: job enqueueing is no longer idempotent"
        )
        assert "UNIQUE" in found.upper()
        # ADR-032: without `org_id` a live job in one workspace refuses the same key in every
        # other workspace, and `enqueue` reports that as "already queued, nothing to do".
        assert "org_id" in found.lower()
        # The predicate is what makes it *partial* — without it, a key could never be reused
        # after a job finished.
        assert "status" in found.lower()

    async def test_the_old_unscoped_index_is_gone(self, db):
        """Both must not exist at once.

        The migration creates the new index before dropping the old one, deliberately, so the
        guarantee is never weaker mid-migration. If the drop were ever lost, the stricter old
        index would still be refusing cross-workspace keys and the fix would look applied while
        doing nothing.
        """
        stale = await db.scalar(
            text(
                "select indexname from pg_indexes "
                "where schemaname = 'app' and indexname = 'uq_jobs_kind_dedupe_live'"
            )
        )
        assert stale is None, (
            "the pre-ADR-032 index is still present, so cross-workspace keys are still refused"
        )

    async def test_the_events_append_only_trigger_exists(self, db):
        found = await db.scalar(
            text("select tgname from pg_trigger where tgname = 'trg_events_append_only'")
        )
        assert found is not None, "the audit log is no longer append-only"
