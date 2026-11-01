"""Boards a workspace wants polled daily (F5).

Revision ID: b6c2e74d09af
Revises: d4f8a1c93e62
Create Date: 2026-11-01

One empty table. The poll only runs for rows somebody added, so this changes no behaviour until
a board is watched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6c2e74d09af"
down_revision = "d4f8a1c93e62"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "watched_boards",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("label", sa.String(200)),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "source", "slug", name="uq_watched_boards_org_source_slug"),
        sa.CheckConstraint(
            "source IN ('greenhouse','lever','ashby')", name="ck_watched_boards_source"
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("watched_boards", schema=SCHEMA)
