"""A campaign knows what it is for: outreach, or a job hunt.

Revision ID: b6f0a2c54e18
Revises: a9d4e73b1f25
Create Date: 2026-10-27

ADR-025 and `docs/CAMPAIGN_KINDS.md`. One column with a default and a check constraint.

**Nothing changes for an existing campaign**, and that is not merely the intent but the
mechanism: `kind` defaults to `outreach`, and `outreach`'s defaults in `app/domain/kinds.py` are
today's values copied exactly. A campaign that was running before this lands is a campaign whose
every input is unchanged afterwards. `tests/unit/test_campaign_kinds.py` asserts that directly
rather than leaving it to inspection.

The downgrade drops the column. A `job_hunt` campaign would then be read as outreach, whose
persona schema its `persona` jsonb does not satisfy -- so the downgrade is safe only while no
job-hunt campaign exists, which is the case the moment this is reverted in anger.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6f0a2c54e18"
down_revision = "a9d4e73b1f25"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("kind", sa.String(20), nullable=False, server_default="outreach"),
        schema=SCHEMA,
    )
    op.create_check_constraint(
        "ck_campaigns_kind", "campaigns", "kind IN ('outreach','job_hunt')", schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint("ck_campaigns_kind", "campaigns", schema=SCHEMA, type_="check")
    op.drop_column("campaigns", "kind", schema=SCHEMA)
