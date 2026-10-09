"""Refuse to migrate a database that is not ours.

This exists because of a near miss. `.env` held a `DATABASE_URL` pointing at `localhost:5432` —
the default Postgres port, already occupied by an unrelated project's container on the same
machine — and `alembic upgrade head` was one correct password away from creating an `app` schema
inside that other project's database. A wrong password is not a safety mechanism.

So the check is explicit, runs before any DDL, and names what it found:

1. The connected database must be named what we expect (`outreach` by default).
2. The database must not contain tables that are not ours. A database with its own tables in
   `public` belongs to something else, and a migration there would be a write into somebody
   else's data.

There is deliberately no override flag. If this fires, the fix is to point `DATABASE_URL` at the
right database, not to force past the check.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db.models import SCHEMA


class WrongDatabase(Exception):
    """The connection does not point at this application's database."""


def _describe(conn: Connection) -> tuple[str, str, int | None]:
    row = conn.execute(
        text("select current_database(), host(inet_server_addr()), inet_server_port()")
    ).one()
    return row[0], row[1] or "local", row[2]


def permitted_names(expected_name: str) -> frozenset[str]:
    """The database names this application may migrate.

    `<expected>_test` is included so the integration suite can migrate its own database with the
    guard still active — the guard is part of what those tests cover. The suffix is spelled out
    rather than matched loosely, so a database called anything else is still refused.
    """
    return frozenset({expected_name, f"{expected_name}_test"})


def assert_is_our_database(conn: Connection, *, expected_name: str) -> None:
    """Raise `WrongDatabase` unless this connection is safe to migrate."""
    actual_name, host, port = _describe(conn)
    where = f"{host}:{port}/{actual_name}"
    allowed = permitted_names(expected_name)

    if actual_name not in allowed:
        raise WrongDatabase(
            f"Refusing to migrate {where}: this database is named {actual_name!r}, and this "
            f"application only migrates {sorted(allowed)}.\n"
            "Check DATABASE_URL — on a machine running more than one project, the default "
            "Postgres port usually belongs to something else."
        )

    # Tables outside our schema mean somebody else's data lives here. `public` is left empty by
    # our migrations, so anything in it is a foreign tenant.
    foreign: list[str] = list(
        conn.execute(
            text(
                """
            select schemaname || '.' || tablename
            from pg_tables
            where schemaname not in ('pg_catalog', 'information_schema', :ours)
            order by 1
            limit 10
            """
            ),
            {"ours": SCHEMA},
        )
        .scalars()
        .all()
    )

    if foreign:
        listed = ", ".join(foreign)
        raise WrongDatabase(
            f"Refusing to migrate {where}: it already contains tables that are not ours "
            f"({listed}). This looks like another application's database.\n"
            "Point DATABASE_URL at a database dedicated to this project."
        )
