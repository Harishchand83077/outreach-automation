"""A campaign can be set up today and start on Monday.

Revision ID: a81f3c6d2e07
Revises: f2c7d1a60b94

Two nullable timestamps and one new status. Nullable because every campaign that exists today
has no schedule and must keep behaving exactly as it does -- a `starts_at` with a default would
have silently become the authority the moment it was added, which is the mistake ADR-010
deliberately avoided when `orgs.send_mode` went in.

`scheduled` joins the status check constraint. It could have been left as `draft` until the
start time, but `draft` means "not finished being set up" to the person reading it, and a
campaign waiting for Monday is finished. A status that lies about why nothing is happening is
the thing this whole feature exists to stop.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a81f3c6d2e07"
down_revision: str | None = "f2c7d1a60b94"
branch_labels: None = None
depends_on: None = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "campaigns",
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        schema=SCHEMA,
    )

    op.drop_constraint("ck_campaigns_status", "campaigns", schema=SCHEMA)
    op.create_check_constraint(
        "ck_campaigns_status",
        "campaigns",
        "status IN ('draft','scheduled','running','paused','completed','archived')",
        schema=SCHEMA,
    )
    # A window that ends before it starts is a typo, and one that reaches the database is a
    # campaign that can never run while looking perfectly fine on screen.
    op.create_check_constraint(
        "ck_campaigns_schedule_order",
        "campaigns",
        "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint("ck_campaigns_schedule_order", "campaigns", schema=SCHEMA)
    # Anything parked in `scheduled` goes back to `draft`, or the old constraint cannot be
    # recreated. It is the state those campaigns were in before this migration.
    op.execute(f"UPDATE {SCHEMA}.campaigns SET status = 'draft' WHERE status = 'scheduled'")
    op.drop_constraint("ck_campaigns_status", "campaigns", schema=SCHEMA)
    op.create_check_constraint(
        "ck_campaigns_status",
        "campaigns",
        "status IN ('draft','running','paused','completed','archived')",
        schema=SCHEMA,
    )
    op.drop_column("campaigns", "ends_at", schema=SCHEMA)
    op.drop_column("campaigns", "starts_at", schema=SCHEMA)
