"""Opportunity workspace tables: opportunities, opportunity_events, tasks, notes (S19.2a, ADR-069).

Revision ID: f71c9d84b2e5
Revises: e45b8c62a1f3
Create Date: 2026-11-21

An opportunity wraps a job post (or a manual entry). The status machine lives in
``app/domain/opportunity.py``; the transition function in ``app/db/opportunity.py``
is the only writer of ``opportunities.status``.
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlalchemy.dialects.postgresql
from alembic import op

revision = "f71c9d84b2e5"
down_revision = "e45b8c62a1f3"
branch_labels = None
depends_on = None

SCHEMA = "app"

STATUS_VALUES = (
    "saved", "preparing", "ready", "applied",
    "in_conversation", "interviewing", "offer", "closed",
)
CLOSED_REASONS = (
    "rejected", "withdrawn", "job_closed",
    "skipped", "no_response", "offer_accepted", "offer_declined",
)
TASK_STATUS_VALUES = ("pending", "done", "dismissed")
ACTOR_KINDS = ("user", "extension", "system")


def upgrade() -> None:
    op.create_table(
        "opportunities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("job_post_id", sa.UUID(), nullable=True),
        sa.Column("company_domain", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="saved"),
        sa.Column("strategy_note", sa.Text(), nullable=True),
        sa.Column("resume_version_id", sa.UUID(), nullable=True),
        sa.Column("closed_reason", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["job_post_id"], [f"{SCHEMA}.job_posts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["resume_version_id"],
            [f"{SCHEMA}.resume_versions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "job_post_id", name="uq_opportunities_org_post"
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUS_VALUES) + ")",
            name="ck_opportunities_status",
        ),
        sa.CheckConstraint(
            "(status = 'closed' AND closed_reason IS NOT NULL) "
            "OR (status != 'closed' AND closed_reason IS NULL)",
            name="ck_opportunities_closed_reason",
        ),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN ("
            + ", ".join(f"'{r}'" for r in CLOSED_REASONS)
            + ")",
            name="ck_opportunities_closed_reason_value",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_opportunities_org_status", "opportunities", ["org_id", "status"], schema=SCHEMA
    )
    op.create_index("ix_opportunities_job_post", "opportunities", ["job_post_id"], schema=SCHEMA)

    op.create_table(
        "opportunity_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_kind", sa.String(length=20), nullable=False),
        sa.Column("event_kind", sa.String(length=60), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            [f"{SCHEMA}.opportunities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "actor_kind IN (" + ", ".join(f"'{a}'" for a in ACTOR_KINDS) + ")",
            name="ck_opp_events_actor_kind",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_opp_events_opportunity",
        "opportunity_events",
        ["opportunity_id", "occurred_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], [f"{SCHEMA}.opportunities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in TASK_STATUS_VALUES) + ")",
            name="ck_tasks_status",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_tasks_opportunity", "tasks", ["opportunity_id"], schema=SCHEMA)
    op.create_index("ix_tasks_org_due", "tasks", ["org_id", "due_at"], schema=SCHEMA)

    op.create_table(
        "notes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["opportunity_id"], [f"{SCHEMA}.opportunities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index("ix_notes_opportunity", "notes", ["opportunity_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ix_notes_opportunity", table_name="notes", schema=SCHEMA)
    op.drop_table("notes", schema=SCHEMA)
    op.drop_index("ix_tasks_org_due", table_name="tasks", schema=SCHEMA)
    op.drop_index("ix_tasks_opportunity", table_name="tasks", schema=SCHEMA)
    op.drop_table("tasks", schema=SCHEMA)
    op.drop_index("ix_opp_events_opportunity", table_name="opportunity_events", schema=SCHEMA)
    op.drop_table("opportunity_events", schema=SCHEMA)
    op.drop_index("ix_opportunities_job_post", table_name="opportunities", schema=SCHEMA)
    op.drop_index("ix_opportunities_org_status", table_name="opportunities", schema=SCHEMA)
    op.drop_table("opportunities", schema=SCHEMA)
