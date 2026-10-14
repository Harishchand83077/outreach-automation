"""import batches, so a wrong sheet can be undone

Revision ID: d3b8c04e9a17
Revises: c7a2e5b81f40
Created: 2026-10-15 10:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d3b8c04e9a17"
down_revision: str | None = "c7a2e5b81f40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, and left NULL for every lead that already exists. A lead imported before this
    # column existed belongs to no batch, which is the truth -- there is no record of which
    # upload it came from, and inventing one would make an undo delete the wrong rows.
    op.add_column(
        "leads",
        sa.Column("import_batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="app",
    )
    # Partial: only rows that belong to a batch are ever looked up this way, and an undo is a
    # rare operation against a table that will hold tens of thousands of rows.
    op.create_index(
        "ix_leads_import_batch",
        "leads",
        ["import_batch_id"],
        unique=False,
        schema="app",
        postgresql_where=sa.text("import_batch_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_leads_import_batch", table_name="leads", schema="app")
    op.drop_column("leads", "import_batch_id", schema="app")
