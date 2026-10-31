"""Job postings from public ATS feeds and pasted URLs (F5).

Revision ID: d4f8a1c93e62
Revises: c7b3e9a2d481
Create Date: 2026-10-31

One empty table, no backfill. `body_text` is nullable because Greenhouse's list response carries
no description at all, and the retention sweep clears it like researched page text.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f8a1c93e62"
down_revision = "c7b3e9a2d481"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "job_posts",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("company_domain", sa.String(253)),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("location", sa.String(500)),
        sa.Column("apply_url", sa.String(2000), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("body_text", sa.Text()),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'open'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "source", "external_id", name="uq_job_posts_org_source_ext"),
        sa.CheckConstraint(
            "source IN ('greenhouse','lever','ashby','pasted')", name="ck_job_posts_source"
        ),
        sa.CheckConstraint("status IN ('open','archived')", name="ck_job_posts_status"),
        schema=SCHEMA,
    )
    op.create_index("ix_job_posts_org_fetched", "job_posts", ["org_id", "fetched_at"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_job_posts_org_fetched", table_name="job_posts", schema=SCHEMA)
    op.drop_table("job_posts", schema=SCHEMA)
