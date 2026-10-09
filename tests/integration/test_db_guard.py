"""The migration guard, against a real server.

This covers a near miss rather than a hypothetical. `.env` held a `DATABASE_URL` on
`localhost:5432` — the default port, occupied on this machine by an unrelated project's
container — and `alembic upgrade head` was one correct password away from creating an `app`
schema inside that other project's database. The only thing that stopped it was the password
being wrong, which is not a safety mechanism. See ADR-005.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.guard import WrongDatabase, assert_is_our_database, permitted_names
from tests.integration.conftest import TEST_DATABASE_URL

#: Stands in for the neighbouring project's database.
FOREIGN_DB = "guard_probe_not_ours"


def _url_for(database: str) -> str:
    parts = urlsplit(TEST_DATABASE_URL)
    return urlunsplit(parts._replace(path=f"/{database}"))


class TestPermittedNames:
    def test_the_app_database_and_its_test_twin_are_allowed(self):
        assert permitted_names("outreach") == {"outreach", "outreach_test"}

    @pytest.mark.parametrize(
        "name",
        ["postgres", "ml_insight", "outreach_prod", "outreachtest", "not_ours", ""],
    )
    def test_anything_else_is_refused(self, name):
        assert name not in permitted_names("outreach")


@pytest.mark.asyncio(loop_scope="session")
class TestTheGuardAgainstARealServer:
    async def test_our_own_test_database_passes(self, _migrated_database):
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as conn:
                # Must not raise: this is our database, holding only our schema.
                await conn.run_sync(assert_is_our_database, expected_name="outreach")
        finally:
            await engine.dispose()

    async def test_a_database_with_the_wrong_name_is_refused(self, _migrated_database):
        """The first line of defence: the name must be one of ours."""
        admin = create_async_engine(_url_for("postgres"), isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{FOREIGN_DB}"'))
                await conn.execute(text(f'CREATE DATABASE "{FOREIGN_DB}"'))

            probe = create_async_engine(_url_for(FOREIGN_DB))
            try:
                async with probe.connect() as conn:
                    with pytest.raises(WrongDatabase, match="is named"):
                        await conn.run_sync(assert_is_our_database, expected_name="outreach")
            finally:
                await probe.dispose()
        finally:
            async with admin.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{FOREIGN_DB}"'))
            await admin.dispose()

    async def test_a_correctly_named_database_holding_foreign_tables_is_refused(
        self, _migrated_database
    ):
        """The second line of defence, and the one that would have caught the real near miss.

        A database can carry the right name and still belong to something else. Tables outside
        our schema mean another application's data is in here, so we must not write to it.
        """
        admin = create_async_engine(_url_for("postgres"), isolation_level="AUTOCOMMIT")
        rightly_named = "outreach_test"
        try:
            probe = create_async_engine(_url_for(rightly_named))
            try:
                async with probe.begin() as conn:
                    await conn.execute(text("CREATE TABLE public.someone_elses_data (id int)"))
                async with probe.connect() as conn:
                    with pytest.raises(WrongDatabase, match="not ours"):
                        await conn.run_sync(assert_is_our_database, expected_name="outreach")
            finally:
                async with probe.begin() as conn:
                    await conn.execute(text("DROP TABLE IF EXISTS public.someone_elses_data"))
                await probe.dispose()
        finally:
            await admin.dispose()

    async def test_the_error_names_what_it_found(self, _migrated_database):
        """A guard that fires without saying why just gets forced past."""
        probe = create_async_engine(_url_for("postgres"))
        try:
            async with probe.connect() as conn:
                with pytest.raises(WrongDatabase) as exc:
                    await conn.run_sync(assert_is_our_database, expected_name="outreach")
            message = str(exc.value)
            assert "postgres" in message
            assert "DATABASE_URL" in message, "the message must say what to fix"
        finally:
            await probe.dispose()
