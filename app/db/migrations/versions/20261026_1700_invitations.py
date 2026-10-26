"""Hold a place in an org for somebody who has not signed in yet.

Revision ID: e5b3c7d21a94
Revises: d4e1f8a90c27
Create Date: 2026-10-26

Adding a teammate meant inserting a `members` row by hand, and it had to be done **before** their
first authenticated request. Afterwards, `provision()` has already given them a workspace of their
own, and inserting the row then leaves them with two memberships that `deps.current_user` resolves
by taking whichever comes back first -- `uq_members_org_user` is per org, so nothing refuses it.

Purely additive: one new table. No existing row is read or changed, and a deployment that never
creates an invitation behaves exactly as it does today.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5b3c7d21a94"
down_revision = "d4e1f8a90c27"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column(
            "id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="member"),
        sa.Column("invited_by", sa.String(320)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_invitations_role"),
        schema=SCHEMA,
    )
    op.create_index("ix_invitations_email", "invitations", ["email"], schema=SCHEMA)
    op.create_index("ix_invitations_org", "invitations", ["org_id"], schema=SCHEMA)

    # **One pending invitation per address, across the whole deployment.** Partial, so a used or
    # revoked row never blocks a fresh invitation -- re-inviting somebody who left has to work.
    #
    # Deployment-wide rather than per org on purpose: a person belongs to exactly one workspace
    # here, so two orgs holding a pending invitation for the same address is a race whose winner
    # depends on which of them signs in first. Refusing the second invitation is a sentence
    # somebody can act on; losing a race is not.
    op.create_index(
        "uq_invitations_pending_email",
        "invitations",
        ["email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("uq_invitations_pending_email", table_name="invitations", schema=SCHEMA)
    op.drop_index("ix_invitations_org", table_name="invitations", schema=SCHEMA)
    op.drop_index("ix_invitations_email", table_name="invitations", schema=SCHEMA)
    op.drop_table("invitations", schema=SCHEMA)
