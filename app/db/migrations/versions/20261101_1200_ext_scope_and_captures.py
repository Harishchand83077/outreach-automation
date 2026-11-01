"""The extension's scope, and what it may store (F8, ADR-039).

Revision ID: a2e94c710bd5
Revises: f8d2a64b17c3
Create Date: 2026-11-01

Widens `ck_api_tokens_scopes` by one value and adds two tables. No existing row changes: every
token today carries `mcp:read` or `mcp:propose`, both of which still satisfy the constraint.

`captures` holds page text a user sent deliberately, kept for days rather than months and listed
in `PRIVACY.md`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a2e94c710bd5"
down_revision = "f8d2a64b17c3"
branch_labels = None
depends_on = None

SCHEMA = "app"
SCOPE_CHECK = "ck_api_tokens_scopes"


def upgrade() -> None:
    op.drop_constraint(SCOPE_CHECK, "api_tokens", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        SCOPE_CHECK,
        "api_tokens",
        "scopes <@ ARRAY['mcp:read','mcp:propose','ext:suggest']::text[]",
        schema=SCHEMA,
    )

    op.create_table(
        "captures",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        # Which token sent it, so a device can be revoked and its captures identified.
        sa.Column("token_id", sa.UUID(as_uuid=True)),
        sa.Column("page_url", sa.String(2000), nullable=False),
        sa.Column("page_text", sa.Text()),
        # form | posting. A form capture carries labels; a posting capture carries text.
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["token_id"], [f"{SCHEMA}.api_tokens.id"], ondelete="SET NULL"),
        sa.CheckConstraint("kind IN ('form','posting')", name="ck_captures_kind"),
        schema=SCHEMA,
    )
    op.create_index("ix_captures_org_at", "captures", ["org_id", "captured_at"], schema=SCHEMA)

    op.create_table(
        "suggestions",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("capture_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("field_key", sa.String(300), nullable=False),
        sa.Column("proposed_answer", sa.Text()),
        sa.Column(
            "fact_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # proposed | accepted | edited | rejected | needs_user. The accept and edit signal is
        # what feeds the dataset later (ADR-026).
        sa.Column("state", sa.String(20), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["capture_id"], [f"{SCHEMA}.captures.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "state IN ('proposed','accepted','edited','rejected','needs_user')",
            name="ck_suggestions_state",
        ),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("suggestions", schema=SCHEMA)
    op.drop_index("ix_captures_org_at", table_name="captures", schema=SCHEMA)
    op.drop_table("captures", schema=SCHEMA)

    # An ext token would violate the narrower constraint, so it is revoked rather than silently
    # stripped: the gateway it was for goes away in the same revert.
    op.execute(
        f"""
        UPDATE {SCHEMA}.api_tokens
           SET revoked_at = now()
         WHERE revoked_at IS NULL AND 'ext:suggest' = ANY(scopes)
        """
    )
    op.execute(
        f"""
        UPDATE {SCHEMA}.api_tokens
           SET scopes = ARRAY['mcp:read']::text[]
         WHERE 'ext:suggest' = ANY(scopes)
        """
    )
    op.drop_constraint(SCOPE_CHECK, "api_tokens", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        SCOPE_CHECK,
        "api_tokens",
        "scopes <@ ARRAY['mcp:read','mcp:propose']::text[]",
        schema=SCHEMA,
    )
