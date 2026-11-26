"""ADR-084 -- saved_people table.

Revision ID: f94b2c67d8a1
Revises: e83f1a56b9c2
Create Date: 2026-11-26

People the user saved from a page they were viewing, to look up an address for later. Additive:
a new table, no existing row is touched. Downgrade drops it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f94b2c67d8a1"
down_revision = "e83f1a56b9c2"
branch_labels = None
depends_on = None

_SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "saved_people",
        sa.Column(
            "id", sa.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column(
            "org_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(f"{_SCHEMA}.orgs.id", ondelete="CASCADE", name="fk_saved_people_org_id"),
            nullable=False,
        ),
        sa.Column("full_name", sa.String(300), nullable=False),
        sa.Column("headline", sa.String(500)),
        sa.Column("company_name", sa.String(300)),
        sa.Column("profile_url", sa.String(2000), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("state", sa.String(20), nullable=False, server_default="to_look_up"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_saved_people"),
        sa.CheckConstraint(
            "state IN ('to_look_up', 'found', 'dismissed')", name="ck_saved_people_state"
        ),
        sa.UniqueConstraint("org_id", "profile_url", name="uq_saved_people_org_url"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("saved_people", schema=_SCHEMA)
