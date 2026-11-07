"""Two narrower token scopes: the person's own facts, and a note with a source.

Revision ID: f3a67b1c8e05
Revises: e81c5f4a9d26
Create Date: 2026-11-07

Widens `ck_api_tokens_scopes` from three values to five. **No existing row changes**: every token
in the table carries `mcp:read`, or `mcp:read` and `mcp:propose`, or `ext:suggest`, and all three
still satisfy the wider constraint.

**And it corrects a drift.** `models.py` has declared this constraint as
`scopes <@ ARRAY['mcp:read']` since the table was created, while two migrations widened the live
one -- so the model has been two slices stale. A model that disagrees with the database is what
makes `alembic revision --autogenerate` write a migration dropping something it cannot see, which
has already happened once in this repository with an index. The constraint is now generated from
`models.API_TOKEN_SCOPES`, which is the single list.

**The permitted set is still an allowlist of five.** `mcp:send` stays unexpressible -- there is no
tool it could authorise, and the database refusing it is what keeps that true independently of the
code.

The downgrade **revokes** any token carrying one of the two new scopes rather than silently
stripping it, which is the choice ADR-030's downgrade made for the same reason: a credential whose
permissions change under it while it keeps working is worse than one that stops.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a67b1c8e05"
down_revision = "e81c5f4a9d26"
branch_labels = None
depends_on = None

SCHEMA = "app"
NAME = "ck_api_tokens_scopes"

#: Mirrors `models.API_TOKEN_SCOPES`. Written out here because a migration must keep working
#: against the code of its own day; the model holds the authority for new rows.
BEFORE = ("mcp:read", "mcp:propose", "ext:suggest")
AFTER = ("mcp:read", "mcp:profile", "mcp:notes", "mcp:propose", "ext:suggest")
NEW_SCOPES = ("mcp:profile", "mcp:notes")


def _array(values: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{value}'" for value in values) + "]::text[]"


def upgrade() -> None:
    op.drop_constraint(NAME, "api_tokens", type_="check", schema=SCHEMA)
    op.create_check_constraint(NAME, "api_tokens", f"scopes <@ {_array(AFTER)}", schema=SCHEMA)


def downgrade() -> None:
    # Revoked, not stripped: a token that keeps working with fewer permissions than its holder
    # was told it has is the worse failure.
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.api_tokens
               SET revoked_at = now()
             WHERE revoked_at IS NULL
               AND scopes && {_array(NEW_SCOPES)}
            """
        )
    )
    # Anything still live now satisfies the narrower constraint, because the only rows that did
    # not were just revoked -- and a revoked row is still a row, so the scopes have to go too.
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.api_tokens
               SET scopes = ARRAY(
                     SELECT unnest(scopes) EXCEPT SELECT unnest({_array(NEW_SCOPES)})
                   )
             WHERE scopes && {_array(NEW_SCOPES)}
            """
        )
    )
    # `cardinality(scopes) > 0` is a separate constraint, so a token whose *only* scope was one
    # of the two new ones would now be empty. It is given back the read scope: it is revoked, so
    # this grants nothing, and leaving it empty would fail the other constraint.
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.api_tokens
               SET scopes = ARRAY['mcp:read']::text[]
             WHERE cardinality(scopes) = 0
            """
        )
    )
    op.drop_constraint(NAME, "api_tokens", type_="check", schema=SCHEMA)
    op.create_check_constraint(NAME, "api_tokens", f"scopes <@ {_array(BEFORE)}", schema=SCHEMA)
