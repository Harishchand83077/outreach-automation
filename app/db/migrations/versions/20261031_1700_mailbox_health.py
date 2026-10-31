"""Per-mailbox daily health, for bounce auto-pause (ADR-034).

Revision ID: c7b3e9a2d481
Revises: a91c5d47f3b2
Create Date: 2026-10-31

One empty table with no backfill, so adding it changes nobody's behaviour: the rollup writes its
first rows on the next `Scheduler` pass and the auto-pause has nothing to act on until a mailbox
has actually sent something.

`complaint_rate` is nullable and is deliberately never written. A complaint rate needs a feed
from the receiving provider, and Postmaster Tools is a link we ask the owner to open rather than
an integration (ADR-029). A column holding zero would read as "no complaints" when it means "we
cannot see complaints".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7b3e9a2d481"
down_revision = "a91c5d47f3b2"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "mailbox_health",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("mailbox_id", sa.UUID(as_uuid=True), nullable=False),
        # A date rather than a timestamp: the send window and the daily cap are both IST days,
        # so health measured in UTC days would straddle them.
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("sent", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("bounced", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("complaint_rate", sa.Float()),
        sa.Column("source", sa.String(20), nullable=False, server_default=sa.text("'messages'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mailbox_id"], [f"{SCHEMA}.mailboxes.id"], ondelete="CASCADE"),
        # The rollup recomputes a day it has already written, so the upsert needs a key.
        sa.UniqueConstraint("mailbox_id", "day", name="uq_mailbox_health_mailbox_day"),
        sa.CheckConstraint("sent >= 0 AND bounced >= 0", name="ck_mailbox_health_counts"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_mailbox_health_org_day", "mailbox_health", ["org_id", "day"], schema=SCHEMA
    )


def downgrade() -> None:
    # Dropping this loses the history the auto-pause reads, which is the correct consequence:
    # the code that reads it goes away in the same revert. **A paused mailbox stays paused** --
    # `mailboxes.paused` is its own column and is not touched here, because un-pausing a mailbox
    # as a side effect of a schema revert would resume sending from an account somebody stopped
    # for a reason this table was the evidence for.
    op.drop_index("ix_mailbox_health_org_day", table_name="mailbox_health", schema=SCHEMA)
    op.drop_table("mailbox_health", schema=SCHEMA)
