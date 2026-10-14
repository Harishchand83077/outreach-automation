"""campaign mode and fixed templates

Revision ID: c7a2e5b81f40
Revises: b4f1a7c93d52
Created: 2026-10-14 19:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a2e5b81f40"
down_revision: str | None = "b4f1a7c93d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `research` is the existing behaviour and therefore the default, so every campaign that
    # exists keeps drafting exactly as it did. A new mode is opt-in per campaign, chosen when
    # it is created; nothing about this migration changes what an existing campaign sends.
    op.add_column(
        "campaigns",
        sa.Column(
            "mode",
            sa.String(length=20),
            nullable=False,
            server_default="research",
        ),
        schema="app",
    )
    op.create_check_constraint(
        "ck_campaigns_mode",
        "campaigns",
        "mode IN ('research','template')",
        schema="app",
    )
    # Empty rather than NULL, matching `persona` and `settings`: the code reads these through a
    # Pydantic model and `{}` validates to "nothing configured", while NULL needs a guard at
    # every read. A `template`-mode campaign with `{}` here cannot draft, and says so.
    op.add_column(
        "campaigns",
        sa.Column(
            "templates",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema="app",
    )


def downgrade() -> None:
    op.drop_constraint("ck_campaigns_mode", "campaigns", schema="app")
    op.drop_column("campaigns", "templates", schema="app")
    op.drop_column("campaigns", "mode", schema="app")
