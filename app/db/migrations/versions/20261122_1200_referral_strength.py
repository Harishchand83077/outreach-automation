"""S19.6 — connections.strength, tags, relationship_note.

Revision ID: d75e1b39f4c1
Revises: c64d9e58f3a0
Create Date: 2026-11-22

Adds three nullable columns to connections so the user can annotate their own
judgement of each tie. Strength is the user's own value, never inferred —
ADR-072 records that inferred tie strength is both impossible to verify and
directly harmful if wrong.

No backfill: older rows have NULL for all three, displayed as "not rated".

Downgrade drops all three columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d75e1b39f4c1"
down_revision = "c64d9e58f3a0"
branch_labels = None
depends_on = None

_SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "strength",
            sa.SmallInteger(),
            sa.CheckConstraint(
                "strength IS NULL OR (strength >= 1 AND strength <= 5)",
                name="ck_connections_strength",
            ),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        "connections",
        sa.Column("tags", sa.ARRAY(sa.Text()), nullable=True),
        schema=_SCHEMA,
    )
    op.add_column(
        "connections",
        sa.Column("relationship_note", sa.Text(), nullable=True),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("connections", "relationship_note", schema=_SCHEMA)
    op.drop_column("connections", "tags", schema=_SCHEMA)
    op.drop_column("connections", "strength", schema=_SCHEMA)
