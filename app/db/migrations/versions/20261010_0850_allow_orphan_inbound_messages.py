"""allow orphan inbound messages

Inbound mail that matches no lead must still be stored. §10.2 is explicit that unmatched mail is
never deleted and never marked read, so `messages.lead_id` cannot be NOT NULL — the alternative is
silently losing somebody's reply. Outbound mail always has a lead, which the check constraint keeps
true now that the column itself no longer can.

Revision ID: e6ad2939ee88
Revises: 3381e3444e39
Created: 2026-10-10 08:50:39.060323+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6ad2939ee88"
down_revision: str | None = "3381e3444e39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "lead_id",
        existing_type=sa.UUID(),
        nullable=True,
        schema="app",
    )
    op.create_check_constraint(
        "ck_messages_out_has_lead",
        "messages",
        "direction = 'in' OR lead_id IS NOT NULL",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("ck_messages_out_has_lead", "messages", type_="check", schema="app")
    # Orphans cannot survive a NOT NULL column, and discarding somebody's reply to satisfy a
    # downgrade would be worse than refusing it. Delete them deliberately or stay upgraded.
    op.execute("DELETE FROM app.messages WHERE lead_id IS NULL")
    op.alter_column(
        "messages",
        "lead_id",
        existing_type=sa.UUID(),
        nullable=False,
        schema="app",
    )
