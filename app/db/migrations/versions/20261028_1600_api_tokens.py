"""A credential for the MCP endpoint, separate from the dashboard's.

Revision ID: d8b1c6e40a37
Revises: c3e7a195f68d
Create Date: 2026-10-28

An MCP client stores one credential and sends it on every call. The dashboard's Supabase JWT is
the obvious candidate and the wrong one: Supabase accepts the RFC 8707 `resource` parameter and
ignores it, issuing `aud: "authenticated"` whatever was asked for (supabase/auth#2610, open at
the time of writing). A token that cannot be shown to have been minted for this endpoint is the
confused-deputy problem resource indicators exist to prevent, so the endpoint gets its own key.

Purely additive: one table, empty on arrival. The `/mcp` endpoint refuses every caller until
somebody mints a token in Settings, so the deploy changes nobody's behaviour.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8b1c6e40a37"
down_revision = "c3e7a195f68d"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        # Kept in clear so the UI can identify a row without holding the secret.
        sa.Column("token_prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("scopes", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_hash"),
        # The database refuses a scope the code has never heard of, as `ck_orgs_send_mode` does
        # for send modes. There is no write scope today, and this is what stops one being
        # invented by a hand-written INSERT.
        sa.CheckConstraint("scopes <@ ARRAY['mcp:read']::text[]", name="ck_api_tokens_scopes"),
        sa.CheckConstraint("cardinality(scopes) > 0", name="ck_api_tokens_scopes_not_empty"),
        schema=SCHEMA,
    )
    op.create_index("ix_api_tokens_org", "api_tokens", ["org_id"], schema=SCHEMA)
    op.create_index("ix_api_tokens_hash", "api_tokens", ["token_hash"], schema=SCHEMA)


def downgrade() -> None:
    # Dropping this revokes every MCP credential, which is the correct consequence: the endpoint
    # that reads them goes away in the same revert. Nothing else reads the table.
    op.drop_index("ix_api_tokens_hash", table_name="api_tokens", schema=SCHEMA)
    op.drop_index("ix_api_tokens_org", table_name="api_tokens", schema=SCHEMA)
    op.drop_table("api_tokens", schema=SCHEMA)
