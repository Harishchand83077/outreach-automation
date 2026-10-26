"""Record when a Gmail grant was given, so it can be warned about before it lapses.

Revision ID: d4e1f8a90c27
Revises: c7a2e94b5d31
Create Date: 2026-10-26

A refresh token issued by an OAuth client in Testing status expires **seven days** after the
person consented (ADR-018). Nothing on the row said when that was: `created_at` stops moving once
the row exists, so a mailbox reconnected this morning looked exactly as stale as it did yesterday,
and `last_refreshed_at` moves every hour as access tokens are minted, so it says nothing about the
grant at all.

The backfill is `created_at`, which is correct for every row that has been connected once and
conservative for one that has been reconnected: it can only make a live connection look older than
it is, which errs towards warning somebody early. Warning early is a reminder; warning late is a
campaign that stopped on Friday.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e1f8a90c27"
down_revision = "c7a2e94b5d31"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "mail_credentials",
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    # Nullable rather than NOT NULL with a default: `now()` as a server default would say every
    # existing grant was given at deploy time, which is the one answer that is certainly wrong
    # and which would silence the warning for a week on a connection expiring tomorrow.
    op.execute(
        f"UPDATE {SCHEMA}.mail_credentials SET granted_at = created_at WHERE granted_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("mail_credentials", "granted_at", schema=SCHEMA)
