"""The deployment no longer has a mailbox of its own.

Revision ID: f1c6a2b84e03
Revises: e5b3c7d21a94
Create Date: 2026-10-26

ADR-023. `orgs.uses_environment_mailbox` said which org was entitled to the `SMTP_EMAIL` and
`SMTP_PASSWORD` in the environment. There is no such account any more: every mailbox authenticates
with a credential somebody connected through OAuth, so the column answers a question nobody asks.

**This is a real, visible change for the org that had it.** Its mailbox stops sending and stops
being read the moment this lands, and stays that way until somebody presses Connect Gmail on it.
That is one click, and it is the point of the change rather than a cost of it: after this there is
no identity in the system that is not somebody's own.

Dropping rather than leaving it in place. A column nothing reads is a trap — the next person to
find it will reasonably assume it still means something, and the one after that will write code
against it.

The downgrade recreates the column as `false` for everyone. It cannot restore which org held it,
because that fact exists nowhere else; `app/services/mail/connection.py` at `c7a2e94b5d31` is the
record of what it meant.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "f1c6a2b84e03"
down_revision = "e5b3c7d21a94"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.drop_column("orgs", "uses_environment_mailbox", schema=SCHEMA)

    op.create_table(
        "send_allowlist",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("email_norm", sa.String(320), nullable=False),
        sa.Column("added_by", sa.String(320)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("org_id", "email_norm", name="uq_send_allowlist_org_email"),
        schema=SCHEMA,
    )

    # **The backfill matters more than it looks.** `SEND_ALLOWLIST` was a deployment-wide
    # environment variable and is now an org's own rows. Without this step, a deployment
    # rehearsing in `allowlist` mode would come back from the migration with an empty list and
    # send nothing to anybody -- correct by the new rules, and indistinguishable from a break.
    #
    # Read from the process environment, which the container already holds. Nothing is logged
    # beyond a count: these are real addresses, which is exactly why the API reports the list
    # only to the org that owns it.
    configured = [
        part.strip().lower()
        for part in (os.environ.get("SEND_ALLOWLIST") or "").split(",")
        if part.strip()
    ]
    if configured:
        connection = op.get_bind()
        oldest = connection.execute(
            sa.text("SELECT id FROM app.orgs ORDER BY created_at, id LIMIT 1")
        ).scalar()
        if oldest is not None:
            for address in dict.fromkeys(configured):
                connection.execute(
                    sa.text(
                        "INSERT INTO app.send_allowlist (org_id, email_norm, added_by)"
                        " VALUES (:org, :email, :who) ON CONFLICT DO NOTHING"
                    ),
                    {"org": oldest, "email": address, "who": "SEND_ALLOWLIST (migrated)"},
                )
            print(f"migrated {len(configured)} allowlisted address(es) to the oldest org")


def downgrade() -> None:
    op.drop_table("send_allowlist", schema=SCHEMA)
    op.add_column(
        "orgs",
        sa.Column(
            "uses_environment_mailbox",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=SCHEMA,
    )
