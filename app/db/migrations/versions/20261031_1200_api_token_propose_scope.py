"""Allow the `mcp:propose` scope on an API token (ADR-030).

Revision ID: f2a4c81b9e07
Revises: d8b1c6e40a37
Create Date: 2026-10-31

`ck_api_tokens_scopes` has said `scopes <@ ARRAY['mcp:read']` since the table was created, which
is what has stopped a write scope being invented by a hand-written INSERT. Phase 3 of
`docs/MCP_PLAN.md` adds one deliberately, so the constraint widens by exactly one value.

**It changes no existing row and nobody's behaviour.** Every token in the table carries
`['mcp:read']`, which still satisfies the new constraint; the minting route defaults to
read-only and asks for the new scope only when somebody ticks the box. What is permitted is
still an allowlist of two, so `mcp:send` remains unexpressible -- there is no tool it could
authorise, and the database refusing it is what keeps that true independently of the code.
"""

from __future__ import annotations

from alembic import op

revision = "f2a4c81b9e07"
down_revision = "d8b1c6e40a37"
branch_labels = None
depends_on = None

SCHEMA = "app"
TABLE = "api_tokens"
NAME = "ck_api_tokens_scopes"


def upgrade() -> None:
    op.drop_constraint(NAME, TABLE, schema=SCHEMA, type_="check")
    op.create_check_constraint(
        NAME,
        TABLE,
        "scopes <@ ARRAY['mcp:read','mcp:propose']::text[]",
        schema=SCHEMA,
    )


def downgrade() -> None:
    # A token holding the propose scope would violate the narrower constraint, so the revert has
    # to resolve it rather than fail halfway. It **revokes** such tokens rather than silently
    # stripping the scope: a credential whose permissions changed under it while it keeps working
    # is worse than one that stops and has to be minted again, and the endpoint it was for no
    # longer accepts that scope either way.
    op.execute(
        f"""
        UPDATE {SCHEMA}.{TABLE}
           SET revoked_at = now()
         WHERE revoked_at IS NULL
           AND 'mcp:propose' = ANY(scopes)
        """
    )
    op.execute(
        f"""
        UPDATE {SCHEMA}.{TABLE}
           SET scopes = ARRAY['mcp:read']::text[]
         WHERE 'mcp:propose' = ANY(scopes)
        """
    )
    op.drop_constraint(NAME, TABLE, schema=SCHEMA, type_="check")
    op.create_check_constraint(
        NAME,
        TABLE,
        "scopes <@ ARRAY['mcp:read']::text[]",
        schema=SCHEMA,
    )
