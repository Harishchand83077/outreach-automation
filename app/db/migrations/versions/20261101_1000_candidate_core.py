"""A candidate's own facts, and the answers that rest on them (F6).

Revision ID: e3b7f05c21da
Revises: b6c2e74d09af
Create Date: 2026-11-01

Two empty tables, no backfill. `CandidateProfile.facts` inside `campaigns.persona` keeps working
unchanged, so every existing job-hunt campaign behaves exactly as before; these tables are what
a *person* owns across campaigns, and a campaign compiles from them when it has none of its own.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e3b7f05c21da"
down_revision = "b6c2e74d09af"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "profile_facts",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("fact_id", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("evidence_url", sa.String(2000)),
        sa.Column("source", sa.String(20), nullable=False, server_default=sa.text("'user'")),
        # NULL means proposed: mined facts are not usable until the person agrees to them.
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "fact_id", name="uq_profile_facts_org_fact"),
        sa.CheckConstraint(
            "kind IN ('skill','project','role','education')", name="ck_profile_facts_kind"
        ),
        sa.CheckConstraint("source IN ('user','github','resume')", name="ck_profile_facts_source"),
        schema=SCHEMA,
    )
    op.create_table(
        "answer_bank",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("question_pattern", sa.String(300), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column(
            "fact_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "question_pattern", name="uq_answer_bank_org_pattern"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("answer_bank", schema=SCHEMA)
    op.drop_table("profile_facts", schema=SCHEMA)
