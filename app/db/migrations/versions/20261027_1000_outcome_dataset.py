"""Capture outcomes, so we can later learn what worked.

Revision ID: a9d4e73b1f25
Revises: f1c6a2b84e03
Create Date: 2026-10-27

ADR-026 and `docs/DATASET_DESIGN.md`. Two new tables and four additive columns.

**Entirely additive, and nothing backfills.** Every existing row keeps its meaning; `lead_outcomes`
starts empty and fills as leads are sent to and reply. There is deliberately no attempt to
reconstruct outcomes for past sends: the classifications that would be used are already recorded,
and inventing a `lead_outcomes` row from them would manufacture `observed_until` values that were
never actually observed. A dataset whose first rows are guesses is worse than one that starts
empty.

The one constraint worth reading is `ck_lead_outcomes_censoring`: an outcome is
`no_reply_after_window` **if and only if** the row is censored. The whole dataset rests on that
distinction, so it is enforced by the database rather than trusted to whoever writes next.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9d4e73b1f25"
down_revision = "f1c6a2b84e03"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "lead_outcomes",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        # Nullable so an erasure detaches the person and keeps the anonymised features.
        sa.Column("lead_id", sa.UUID(as_uuid=True)),
        sa.Column("campaign_id", sa.UUID(as_uuid=True)),
        sa.Column(
            "campaign_kind", sa.String(20), nullable=False, server_default="outreach"
        ),
        # Nullable: NULL is "still watching", which is not a member of the enum.
        sa.Column("outcome", sa.String(30)),
        sa.Column("censored", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("stage", sa.String(20), nullable=False, server_default="sent"),
        sa.Column("objection_kind", sa.String(20)),
        sa.Column("not_now_until", sa.DateTime(timezone=True)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("confirmed_by", sa.String(320)),
        sa.Column("first_sent_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("observed_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], [f"{SCHEMA}.leads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["campaign_id"], [f"{SCHEMA}.campaigns.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("lead_id", name="uq_lead_outcomes_lead"),
        sa.CheckConstraint(
            "(outcome = 'no_reply_after_window') = censored",
            name="ck_lead_outcomes_censoring",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_lead_outcomes_org_outcome", "lead_outcomes", ["org_id", "outcome"], schema=SCHEMA
    )

    op.create_table(
        "experiments",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "arms",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "exploration_share", sa.Float(), nullable=False, server_default="0.1"
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["campaign_id"], [f"{SCHEMA}.campaigns.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "exploration_share >= 0 AND exploration_share <= 1",
            name="ck_experiments_exploration_share",
        ),
        schema=SCHEMA,
    )

    # Which arm a send used, and which warm-up day it went out on. `warmup_day` is stored rather
    # than derived because `warmup_started_on` can be reset, so the same `sent_at` would compute
    # a different answer later. Weekday and hour are deliberately *not* stored: those are
    # derivable from `sent_at` plus the campaign's timezone, and a denormalised copy of a derived
    # value is a copy that can disagree with its source.
    op.add_column("messages", sa.Column("variant", sa.String(40)), schema=SCHEMA)
    op.add_column("messages", sa.Column("warmup_day", sa.Integer()), schema=SCHEMA)

    # The link from a reviewer's edit back to the draft the model produced. Version numbers
    # cannot express it: a regeneration after a rejection also makes a new version, and that is a
    # different event from a human rewriting the model's words.
    op.add_column(
        "drafts", sa.Column("derived_from_id", sa.UUID(as_uuid=True)), schema=SCHEMA
    )
    op.create_foreign_key(
        "fk_drafts_derived_from",
        "drafts",
        "drafts",
        ["derived_from_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )

    # Shipped and read by nothing. The flag has to predate the data, or pooling could be switched
    # on retroactively over results gathered while nobody had been asked.
    op.add_column(
        "orgs",
        sa.Column(
            "dataset_pooling_opt_in",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("orgs", "dataset_pooling_opt_in", schema=SCHEMA)
    op.drop_constraint("fk_drafts_derived_from", "drafts", schema=SCHEMA, type_="foreignkey")
    op.drop_column("drafts", "derived_from_id", schema=SCHEMA)
    op.drop_column("messages", "warmup_day", schema=SCHEMA)
    op.drop_column("messages", "variant", schema=SCHEMA)
    op.drop_table("experiments", schema=SCHEMA)
    op.drop_index("ix_lead_outcomes_org_outcome", table_name="lead_outcomes", schema=SCHEMA)
    op.drop_table("lead_outcomes", schema=SCHEMA)
