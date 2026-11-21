"""S19.4 — job_posts.first_seen_at and last_verified_at.

Revision ID: c64d9e58f3a0
Revises: b93e6a47c1d8
Create Date: 2026-11-22

Adds two nullable observation columns to job_posts. No backfill — the screen
shows "not recorded" for older rows, which is the honest answer (ADR-070).

The existing daily ATS poll writes `last_verified_at` when it confirms a posting
is still open; the extension's capture route writes `first_seen_at` when a posting
is first stored. Both are nullable so older rows do not need defaults.

Downgrade drops both columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c64d9e58f3a0"
down_revision = "b93e6a47c1d8"
branch_labels = None
depends_on = None


_SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "job_posts",
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "job_posts",
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("job_posts", "last_verified_at", schema=_SCHEMA)
    op.drop_column("job_posts", "first_seen_at", schema=_SCHEMA)
