"""Alembic environment.

The database URL comes from settings, never from `alembic.ini`, so no connection string is ever
committed. Migrations are forward-only and backwards-compatible within a release
(expand, migrate, contract) per ARCHITECTURE_TARGET §19.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.settings import config as settings
from app.db.guard import assert_is_our_database
from app.db.models import SCHEMA, Base

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

#: A programmatic caller (the integration test harness) may set the URL itself; only fall back to
#: settings when it has not. This keeps connection strings out of alembic.ini either way.
if not alembic_config.get_main_option("sqlalchemy.url", None):
    alembic_config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, *args: object) -> bool:
    """Autogenerate only touches our own schema, never Postgres internals."""
    if type_ == "table":
        return getattr(obj, "schema", None) == SCHEMA
    return True


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_object=_include_object,
        # Catch column type drift in autogenerate rather than discovering it in production.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=SCHEMA,
        include_schemas=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    # Before any DDL: confirm this is our database and not a neighbour's. See app/db/guard.py
    # and ADR-005 — a wrong password once came within a correct password of migrating an
    # unrelated project's database.
    assert_is_our_database(connection, expected_name=settings.DB_EXPECTED_NAME)

    # The schema must exist before Alembic writes its own version table into it.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # `begin()` rather than `connect()`: creating the schema before `context.configure` opens a
    # transaction on the connection, and without an outer commit the whole migration — DDL and
    # version row alike — silently rolls back on exit while still logging success.
    async with connectable.begin() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
