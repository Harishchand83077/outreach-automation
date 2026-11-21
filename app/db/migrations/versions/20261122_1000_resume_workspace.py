"""S19.3 — resume_versions.opportunity_id and base_document_id.

Revision ID: b93e6a47c1d8
Revises: a82c5f71e0b4
Create Date: 2026-11-22

Adds two nullable columns to resume_versions so a version can be linked to the
opportunity it was tailored for and to the source document it was derived from.

Both are nullable and have no backfill — the screen shows "not recorded" for
older versions, which is the honest answer (ADR-070 principle applied here too).

Downgrade drops both columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b93e6a47c1d8"
down_revision = "a82c5f71e0b4"
branch_labels = None
depends_on = None

_SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "resume_versions",
        sa.Column(
            "opportunity_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{_SCHEMA}.opportunities.id",
                ondelete="SET NULL",
                name="fk_resume_versions_opportunity_id",
            ),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.add_column(
        "resume_versions",
        sa.Column(
            "base_document_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{_SCHEMA}.documents.id",
                ondelete="SET NULL",
                name="fk_resume_versions_base_document_id",
            ),
            nullable=True,
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_resume_versions_opportunity_id",
        "resume_versions",
        ["opportunity_id"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_resume_versions_opportunity_id", table_name="resume_versions", schema=_SCHEMA)
    op.drop_column("resume_versions", "base_document_id", schema=_SCHEMA)
    op.drop_column("resume_versions", "opportunity_id", schema=_SCHEMA)
