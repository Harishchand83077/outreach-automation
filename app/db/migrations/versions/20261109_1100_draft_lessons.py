"""What reviewers keep changing, waiting for somebody to decide about it.

Revision ID: b57e3c91d248
Revises: a92d41f7b6c3
Create Date: 2026-11-09

One new empty table. Nothing changes for any existing row, and nothing changes about what gets
written: a lesson only reaches a drafting prompt in the `approved` state, and nothing is in that
state until a person puts it there.

`(org_id, kind)` is unique, which is what makes **a dismissed lesson stay dismissed**. The kind
is a stable key computed from the edit itself, so recomputing finds the existing row rather than
proposing a duplicate of something somebody already said no to — and a feature that asked again
every week would train them to stop reading it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b57e3c91d248"
down_revision = "a92d41f7b6c3"
branch_labels = None
depends_on = None

SCHEMA = "app"
STATES = ("proposed", "approved", "dismissed")


def upgrade() -> None:
    op.create_table(
        "draft_lessons",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("state", sa.String(length=20), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("decided_by", sa.String(length=320), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "kind", name="uq_draft_lessons_org_kind"),
        sa.CheckConstraint(
            "state IN (" + ", ".join(f"'{name}'" for name in STATES) + ")",
            name="ck_draft_lessons_state",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Drops the table, and with it every approved lesson.

    Safe in the direction that matters: a draft written without them is a draft written the way
    this system wrote them before F16, which is the behaviour every existing email already has.
    """
    op.drop_table("draft_lessons", schema=SCHEMA)
