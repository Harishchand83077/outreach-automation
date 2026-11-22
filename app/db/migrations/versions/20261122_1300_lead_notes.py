"""S21.1 — lead_notes table.

Revision ID: e83f1a56b9c2
Revises: d75e1b39f4c1
Create Date: 2026-11-22

Reviewers can leave free-text notes on a lead. Notes are immutable after creation — a
reviewer's observation at a point in time should be preserved as written, not silently
corrected. Deleted when the lead is deleted (CASCADE). ADR-078.

No backfill; there are no existing notes to create.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e83f1a56b9c2"
down_revision = "d75e1b39f4c1"
branch_labels = None
depends_on = None

_SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "lead_notes",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "lead_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.leads.id", ondelete="CASCADE", name="fk_lead_notes_lead_id"),
            nullable=False,
        ),
        sa.Column("author_id", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lead_notes"),
        sa.CheckConstraint(
            "length(body) > 0 AND length(body) <= 2000",
            name="ck_lead_notes_body_len",
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_lead_notes_lead_id",
        "lead_notes",
        ["lead_id"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_lead_notes_lead_id", table_name="lead_notes", schema=_SCHEMA)
    op.drop_table("lead_notes", schema=_SCHEMA)
