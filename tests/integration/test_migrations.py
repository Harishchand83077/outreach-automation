"""The migrations must agree with the models.

This test exists because of a specific, nasty failure. `uq_jobs_kind_dedupe_live` — the partial
unique index that makes job enqueueing idempotent — was created by raw SQL in migration 1 but
never declared in the model metadata. Autogenerate therefore read it as drift and wrote a
migration that **dropped** it, silently removing the guarantee. The queue tests caught it, but
only because they happened to cover that behaviour.

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
        found = await db.scalar(
            text(
                "select indexdef from pg_indexes "
                "where schemaname = 'app' and indexname = 'uq_jobs_kind_dedupe_live'"
            )
        )
        assert found is not None, (
            "uq_jobs_kind_dedupe_live is missing: job enqueueing is no longer idempotent"
        )
        assert "UNIQUE" in found.upper()
        # The predicate is what makes it *partial* — without it, a key could never be reused
        # after a job finished.
        assert "status" in found.lower()

    async def test_the_events_append_only_trigger_exists(self, db):
        found = await db.scalar(
            text("select tgname from pg_trigger where tgname = 'trg_events_append_only'")
        )
        assert found is not None, "the audit log is no longer append-only"
