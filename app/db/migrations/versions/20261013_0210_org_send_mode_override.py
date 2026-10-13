"""org send mode override

Revision ID: b4f1a7c93d52
Revises: cad167cbb0b0
Created: 2026-10-13 02:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b4f1a7c93d52'
down_revision: str | None = 'cad167cbb0b0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable on purpose: NULL means "no override, use the environment". That makes this
    # migration a no-op for behaviour -- every existing org keeps sending exactly as it did,
    # under whatever SEND_MODE the process was started with. A column with a default would have
    # silently become the authority the moment it was added (ADR-010).
    op.add_column(
        "orgs",
        sa.Column("send_mode", sa.String(length=20), nullable=True),
        schema="app",
    )
    op.add_column(
        "orgs",
        sa.Column("send_mode_set_by", sa.String(length=320), nullable=True),
        schema="app",
    )
    op.add_column(
        "orgs",
        sa.Column("send_mode_set_at", sa.DateTime(timezone=True), nullable=True),
        schema="app",
    )
    # The database refuses a value the code has never heard of, so a hand-written UPDATE cannot
    # invent a fourth mode. NULL passes, which is what "no override" is.
    op.create_check_constraint(
        "ck_orgs_send_mode",
        "orgs",
        "send_mode IS NULL OR send_mode IN ('disabled','allowlist','live')",
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("ck_orgs_send_mode", "orgs", schema="app", type_="check")
    op.drop_column("orgs", "send_mode_set_at", schema="app")
    op.drop_column("orgs", "send_mode_set_by", schema="app")
    op.drop_column("orgs", "send_mode", schema="app")
