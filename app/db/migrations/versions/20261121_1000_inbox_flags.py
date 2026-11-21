"""Inbox snooze and archive flags, one row per thread the reviewer has acted on.

Revision ID: c83f2a19e7d4
Revises: b57e3c91d248
Create Date: 2026-11-21

A thread is a lead.  Snooze and archive are UI decisions, not lead states: they
control visibility in the inbox view without touching the outreach pipeline.  The
one-writer rule for ``leads.state`` is preserved -- this table never touches it.

``snoozed_until`` and ``archived_at`` are both nullable; a row only exists when
at least one flag has been set.  The unique constraint means a reviewer can re-
snooze or un-archive without creating duplicate rows.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c83f2a19e7d4"
down_revision = "b57e3c91d248"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "inbox_flags",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(length=320), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lead_id"], [f"{SCHEMA}.leads.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "lead_id", name="uq_inbox_flags_org_lead"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_inbox_flags_org_snoozed",
        "inbox_flags",
        ["org_id", "snoozed_until"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_flags_org_snoozed", table_name="inbox_flags", schema=SCHEMA)
    op.drop_table("inbox_flags", schema=SCHEMA)
