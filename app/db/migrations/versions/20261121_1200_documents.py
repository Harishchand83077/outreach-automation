"""Uploaded documents (resumes), one row per file per workspace (S19.1, ADR-068).

Revision ID: e45b8c62a1f3
Revises: d94a1b73f8e2
Create Date: 2026-11-21

Bytes in Postgres, capped at 2 MB per file (ADR-016 precedent; ADR-068 records
the departure from V4's S3 plan).  Unique on (org_id, sha256) so uploading the
same file twice is idempotent.  The ``text`` column is populated by the
``parse_resume`` job after upload.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e45b8c62a1f3"
down_revision = "d94a1b73f8e2"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("filename", sa.String(length=260), nullable=False),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("bytes", sa.LargeBinary(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"],
            [f"{SCHEMA}.orgs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "sha256", name="uq_documents_org_sha256"),
        sa.CheckConstraint("kind IN ('resume')", name="ck_documents_kind"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_documents_org_id",
        "documents",
        ["org_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_documents_org_id", table_name="documents", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
