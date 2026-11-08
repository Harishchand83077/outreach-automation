"""An OAuth 2.1 authorization server: clients, codes, refresh tokens.

Revision ID: a92d41f7b6c3
Revises: f3a67b1c8e05
Create Date: 2026-11-09

Three new tables and two nullable columns on `api_tokens`. **No existing row changes**, and
nothing starts working differently: a deployment with no `oauth_clients` row has an
authorization server that nobody can ask anything of, and every hand-minted token keeps working
exactly as before because its `resource` is NULL.

**`api_tokens` gains `client_id` and `resource` rather than OAuth getting its own token table**,
which is the decision ADR-058 rests on. An OAuth access token is a row in the existing table, so
the tool catalogue, the scope gate and -- most importantly -- the re-resolution of the
membership on every call are untouched. A second token table would be a second set of rules to
keep in step with those, and the first time they drifted the difference would be a credential
that outlived the membership behind it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a92d41f7b6c3"
down_revision = "f3a67b1c8e05"
branch_labels = None
depends_on = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "redirect_uris",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("created_by", sa.String(length=320), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        # **Per workspace**, so two workspaces may each register the same application and
        # neither can see the other's. A global client would mean any owner could put a
        # plausible name and their own redirect URI on a consent screen another workspace's
        # user is shown -- a consent screen is a phishing surface.
        sa.UniqueConstraint("org_id", "client_id", name="uq_oauth_clients_org_client"),
        # A client with no redirect URI could never be used and would sit there looking
        # configured, which is the shape of defect this project keeps finding.
        sa.CheckConstraint("cardinality(redirect_uris) > 0", name="ck_oauth_clients_redirects"),
        schema=SCHEMA,
    )

    op.create_table(
        "oauth_codes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("redirect_uri", sa.String(length=2000), nullable=False),
        sa.Column("resource", sa.String(length=2000), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column(
            "code_challenge_method",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'S256'"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        # **S256 in the database as well as in the validator.** `plain` is a challenge equal to
        # its verifier and protects against nothing; a row written by hand must not be able to
        # ask for it.
        sa.CheckConstraint("code_challenge_method = 'S256'", name="ck_oauth_codes_method"),
        schema=SCHEMA,
    )
    op.create_index("ix_oauth_codes_expiry", "oauth_codes", ["expires_at"], schema=SCHEMA)

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("resource", sa.String(length=2000), nullable=False),
        sa.Column("access_token_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["access_token_id"], [f"{SCHEMA}.api_tokens.id"], ondelete="SET NULL"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_oauth_refresh_user", "oauth_refresh_tokens", ["org_id", "user_id"], schema=SCHEMA
    )

    op.add_column(
        "api_tokens", sa.Column("client_id", sa.String(length=64), nullable=True), schema=SCHEMA
    )
    op.add_column(
        "api_tokens", sa.Column("resource", sa.String(length=2000), nullable=True), schema=SCHEMA
    )


def downgrade() -> None:
    """Drops the server and every credential it issued.

    **The access tokens go too**, by `client_id IS NOT NULL`, rather than being left behind with
    their `resource` column removed. A token whose audience can no longer be checked is a token
    that would then be accepted here on the strength of being a row -- which is the thing the
    audience check exists to prevent, so leaving them is worse than revoking them. The
    hand-minted ones are untouched.
    """
    op.execute(
        sa.text(
            f"UPDATE {SCHEMA}.api_tokens SET revoked_at = now() "
            "WHERE client_id IS NOT NULL AND revoked_at IS NULL"
        )
    )
    op.drop_column("api_tokens", "resource", schema=SCHEMA)
    op.drop_column("api_tokens", "client_id", schema=SCHEMA)
    op.drop_index("ix_oauth_refresh_user", table_name="oauth_refresh_tokens", schema=SCHEMA)
    op.drop_table("oauth_refresh_tokens", schema=SCHEMA)
    op.drop_index("ix_oauth_codes_expiry", table_name="oauth_codes", schema=SCHEMA)
    op.drop_table("oauth_codes", schema=SCHEMA)
    op.drop_table("oauth_clients", schema=SCHEMA)
