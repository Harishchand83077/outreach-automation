"""Add priority column to provider_credentials for contact lookup waterfall.

Revision ID: d94a1b73f8e2
Revises: c83f2a19e7d4
Create Date: 2026-11-21

Nullable int; a NULL priority is treated as the lowest priority (sorted last).
No backfill: existing rows get NULL and are sorted last in the waterfall until
the workspace owner sets a priority through the settings UI.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d94a1b73f8e2"
down_revision = "c83f2a19e7d4"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "provider_credentials",
        sa.Column("priority", sa.Integer(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("provider_credentials", "priority", schema=SCHEMA)
