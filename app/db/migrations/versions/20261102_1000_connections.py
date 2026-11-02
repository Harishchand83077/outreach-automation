"""The user's own LinkedIn connections, for the referral finder (F10, ADR-042).

Revision ID: 3c1f80a7b4e2
Revises: 08d9c5fbdc1b
Create Date: 2026-11-02

One empty table. No existing row is read or changed.

**There is no email column, and that is the design rather than an omission.** A LinkedIn export
carries `Email Address` for connections who allow it; storing it would turn a referral finder
into somebody's whole address book, belonging to people who agreed to connect with *them*. The
parser drops the column before anything reaches here, and a test asserts an address in the file
reaches no row. The schema not having anywhere to put it is the second line of that defence.

The downgrade drops the table, which deletes the uploaded connections. Stated plainly: they are
the user's own file, re-uploaded in one click, and there is nothing here worth a tombstone --
unlike a suppression, forgetting a connection cannot cause anybody to be contacted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3c1f80a7b4e2"
down_revision = "08d9c5fbdc1b"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("full_name", sa.String(300), nullable=False),
        sa.Column("profile_url", sa.String(2000)),
        sa.Column("company", sa.String(300)),
        sa.Column("company_norm", sa.String(300)),
        sa.Column("position", sa.String(300)),
        sa.Column("connected_on", sa.Date()),
        sa.Column("import_batch_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=SCHEMA,
    )
    # One row per person per workspace, so re-exporting a grown network updates rather than
    # doubling everybody -- a referral shortlist showing the same person twice looks wrong
    # rather than long. Partial, because a row with no URL cannot be deduplicated at all.
    op.create_index(
        "uq_connections_profile",
        "connections",
        ["org_id", "profile_url"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("profile_url IS NOT NULL"),
    )
    op.create_index(
        "ix_connections_org_company", "connections", ["org_id", "company_norm"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_connections_org_company", table_name="connections", schema=SCHEMA)
    op.drop_index("uq_connections_profile", table_name="connections", schema=SCHEMA)
    op.drop_table("connections", schema=SCHEMA)
