"""A posting's fit score, stored so reading one is free.

Revision ID: e81c5f4a9d26
Revises: c47f91a2e6b8
Create Date: 2026-11-07

One new empty table. `fit.score_fit` has existed since F6 with no caller at all -- the extractor,
the scorer and the dataclasses were built and tested and nothing ever ran them against a posting
-- so this migration adds the place the result goes, and the slice adds the press that produces
one.

`score` is check-constrained to 0..1 because it is a fraction of requirements matched, and a
value outside that range could only mean the comparison changed meaning under rows already
stored.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e81c5f4a9d26"
down_revision = "c47f91a2e6b8"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "fit_scores",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_post_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "matched",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "missing",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "requirements",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_post_id"], [f"{SCHEMA}.job_posts.id"], ondelete="CASCADE"),
        # One row per posting: a second score is the same question asked again, and two rows
        # would let the screen and the MCP tool show different answers.
        sa.UniqueConstraint("org_id", "job_post_id", name="uq_fit_scores_org_post"),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_fit_scores_range"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("fit_scores", schema=SCHEMA)
