"""A mailbox pool per campaign, and the sticky sender on a lead (F7, ADR-044).

Revision ID: 7e1d4bc8a902
Revises: 5a2c9e7b1f40
Create Date: 2026-11-03

One empty table and one nullable column. **No existing row changes behaviour.**

That is the point of the design rather than a happy accident. `campaigns.mailbox_id` stays the
campaign's primary mailbox and `pool.pool_for` treats it as a member of its own pool with or
without a row here -- so there is nothing to backfill, and a campaign created tomorrow by a
`POST /campaigns` that knows nothing about pools still sends. The alternative, making this table
the sole authority, would have needed a backfill *and* a change to every path that attaches a
mailbox, and the path that forgot would have stopped sending with nothing on screen saying why.

`leads.mailbox_id` is NULL for every existing lead, which is the truth: those sends happened when
a campaign had exactly one mailbox, and inventing an assignment for them would claim to know
which address a message went out from when `messages.mailbox_id` already records that per
message. A lead with no assignment takes the pool's choice on its next send, and with a pool of
one that is the same mailbox it has always used.

`ON DELETE SET NULL` on `leads.mailbox_id`, not CASCADE: deleting a mailbox must not delete the
leads it wrote to. They become unassigned and the pool picks again, which is the right answer --
the address they knew is gone either way.

The downgrade drops both. The pool collapses to each campaign's primary mailbox, which is the
pre-F7 behaviour, and the sticky assignments are lost -- so a lead mid-conversation could have
its next follow-up sent from a different member of what *was* the pool. Stated here because that
is a visible consequence for a recipient, not just a schema change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import MAX_MAILBOX_WEIGHT, MIN_MAILBOX_WEIGHT

revision = "7e1d4bc8a902"
down_revision = "5a2c9e7b1f40"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "campaign_mailboxes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mailbox_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("campaign_id", "mailbox_id", name="uq_campaign_mailboxes_pair"),
        # Built from the constants in `models.py`, so the constraint and the route that validates
        # a weight cannot disagree about what a weight is.
        sa.CheckConstraint(
            f"weight BETWEEN {MIN_MAILBOX_WEIGHT} AND {MAX_MAILBOX_WEIGHT}",
            name="ck_campaign_mailboxes_weight",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_campaign_mailboxes_mailbox",
        "campaign_mailboxes",
        ["org_id", "mailbox_id"],
        schema=SCHEMA,
    )

    op.add_column(
        "leads",
        sa.Column("mailbox_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_leads_mailbox",
        "leads",
        "mailboxes",
        ["mailbox_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_leads_mailbox", "leads", schema=SCHEMA, type_="foreignkey")
    op.drop_column("leads", "mailbox_id", schema=SCHEMA)
    op.drop_index("ix_campaign_mailboxes_mailbox", table_name="campaign_mailboxes", schema=SCHEMA)
    op.drop_table("campaign_mailboxes", schema=SCHEMA)
