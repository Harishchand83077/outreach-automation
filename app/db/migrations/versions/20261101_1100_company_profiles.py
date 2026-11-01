"""Public company facts, shared across workspaces (F19, ADR-038).

Revision ID: f8d2a64b17c3
Revises: e3b7f05c21da
Create Date: 2026-11-01

**The only table in this schema with no `org_id`.** A deliberate exception to ADR-004, holding
public facts only -- name, careers URL, ATS type and slug, size band, a summary, and the source
behind each field. No lead, no contact, no message, no workspace note; a structural test asserts
that by column name.

One empty table, no backfill. `domain_research` stays exactly as it is: that one is per-workspace
and holds the *findings a draft cites*, which are not public facts and must not be pooled.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f8d2a64b17c3"
down_revision = "e3b7f05c21da"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "company_profiles",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("name", sa.String(300)),
        sa.Column("careers_url", sa.String(2000)),
        sa.Column("ats_type", sa.String(20)),
        sa.Column("ats_slug", sa.String(200)),
        sa.Column("size_band", sa.String(20)),
        sa.Column("summary", sa.Text()),
        sa.Column(
            "sources", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("content_hash", sa.String(64)),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.UniqueConstraint("domain", name="uq_company_profiles_domain"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Nothing is lost that cannot be fetched again: every field here is public and re-derivable,
    # which is the same property that makes sharing it acceptable in the first place.
    op.drop_table("company_profiles", schema=SCHEMA)
