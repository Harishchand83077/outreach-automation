"""A careers page watched by its hash, for a company with no ATS feed.

Revision ID: c47f91a2e6b8
Revises: 9b3e5c81d7a4
Create Date: 2026-11-07

Six nullable columns on `watchlist`, and no new table. A page watch belongs on the watch it is
part of: `signals.intake.record` takes a `WatchlistEntry`, which is what carries the pause, the
company's domain and the lead match, so a separate table would have needed its own copy of all
three and produced two answers to "which companies do we watch" -- the signature defect, in the
slice whose own ADR is about not holding one fact in two places.

**`page_hash` is a hash and the page text has nowhere to go.** Storing the page would make this
a crawler with a cache -- somebody else's copy of their own site, in our database, for every
company any workspace watches -- and `PRIVACY.md` could no longer say we keep no page text.
There is deliberately no column for it.

Additive and NULL for every existing row: a watch with no `careers_url` is simply not polled
for a page, which is every watch today.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "c47f91a2e6b8"
down_revision = "9b3e5c81d7a4"
branch_labels = None
depends_on = None

SCHEMA = "app"

#: `(name, type)` pairs rather than `Column` objects, so each statement builds its own. A
#: module-level `Column` can only be added once -- `Column.copy()` is deprecated and reusing the
#: object across calls mutates it -- and this module is imported by every test run.
COLUMNS: tuple[tuple[str, Any], ...] = (
    ("careers_url", sa.String(length=2000)),
    ("page_hash", sa.String(length=64)),
    ("page_length", sa.Integer()),
    ("page_polled_at", sa.DateTime(timezone=True)),
    ("page_changed_at", sa.DateTime(timezone=True)),
    ("page_refused_reason", sa.String(length=300)),
)


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column("watchlist", sa.Column(name, kind, nullable=True), schema=SCHEMA)

    # Only the watches that have a page are ever selected by the scheduler, and on a table where
    # most rows will not have one a partial index is the whole table's worth of rows avoided.
    op.create_index(
        "ix_watchlist_page_due",
        "watchlist",
        ["page_polled_at"],
        postgresql_where=sa.text("careers_url IS NOT NULL"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drops the page watch. The signals it produced stay.

    `signal_events` rows survive by design -- deleting the record that a company changed their
    careers page in March would quietly rewrite a funnel, which is the same reasoning ADR-049
    used for forgetting a company.
    """
    op.drop_index("ix_watchlist_page_due", table_name="watchlist", schema=SCHEMA)
    for name, _ in reversed(COLUMNS):
        op.drop_column("watchlist", name, schema=SCHEMA)
