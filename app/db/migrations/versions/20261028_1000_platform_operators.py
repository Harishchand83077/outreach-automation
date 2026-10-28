"""A scope above orgs, for the person who runs the deployment.

Revision ID: c3e7a195f68d
Revises: b6f0a2c54e18
Create Date: 2026-10-28

`SIGNUP_MODE=open` gives every signup its own workspace (ADR-017), so the person who runs this
deployment has no way to see how many workspaces exist, whether their mailboxes still
authenticate, or whether the worker is wedged -- the ordinary admin routes answer all of those for
exactly one org, which is correct and is not the question.

Purely additive: one new table with no foreign key, read by one new read-only route. **Nothing
writes it**, here or anywhere in the application -- a grant is an `INSERT` run by somebody who
already has the database, because there is no role that could legitimately hand out a scope that
sits above every org. `docs/USERS.md` carries the statement to run.

The table deliberately has no `org_id`, which is the third documented deviation from ADR-004
after `worker_heartbeats` and `alerts.org_id`. A row here is not one tenant's data.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c3e7a195f68d"
down_revision = "b6f0a2c54e18"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "platform_operators",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Supabase `sub`, as in `members`. Unique across the deployment: unlike a membership
        # there is nothing for a second row to mean, and two rows would make "is this person an
        # operator?" depend on which one came back first -- the exact silent failure ADR-022 was
        # written about.
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("note", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("user_id", name="uq_platform_operators_user"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    # Dropping this removes every grant, so the platform screen stops answering for everybody.
    # Nothing else reads the table, so no other behaviour changes -- and an org's own admin
    # routes are untouched, because the scope was never a role inside one.
    op.drop_table("platform_operators", schema=SCHEMA)
