"""Search providers in `provider_credentials`, and a cache for their answers (F17, ADR-046).

Revision ID: 4c7e2a9b50d3
Revises: b9f3c6a21d47
Create Date: 2026-11-04

Two changes, both additive, and **no existing row is read or changed**.

**One check constraint widens by three values.** `ck_provider_credentials_provider` accepted the
four contact providers; it now accepts `tavily`, `brave` and `serper` as well. Every row in the
table today names a contact provider and still satisfies it, so nothing is rewritten and nothing
can fail to migrate. What is permitted is still an allowlist built from the tuples in `models.py`,
which is the cure this repository adopted after `reason="one-click unsubscribe"` made every
opt-out a 500: a literal that has to match a database constraint belongs to the thing that
defines the constraint.

**One empty table.** `search_cache` holds one provider answer per workspace per question, so a
repeat question costs nobody a credit. It carries `org_id` and is scoped per workspace, unlike
`company_profiles` (ADR-038), because a search answer is a provider's billable response to a
question one workspace asked out of its own credits -- the same side of that line as
`domain_research`, and not ours to pool.

**No workspace gains a search provider by being migrated.** The chain is built from keys an admin
pastes, and a workspace with none behaves exactly as it did before: research degrades to the
company's own pages. The deployment's `TAVILY_API_KEY`, where one is set, keeps working and is
the last member of the chain.

The downgrade narrows the constraint again, which means it **deletes** any search key a
workspace has saved. Revoking them would not do -- the constraint is on `provider` and does not
look at `revoked_at`, so a revoked `tavily` row violates it exactly as a live one does. That is
worth stating rather than hiding behind a tidier-sounding verb: reverting this migration destroys
credentials, and the only way to undo it is for each workspace to paste its key again. It then
drops `search_cache`, which costs the next research run one credit per company and nothing else,
because that table is a cache and holds no part of the record of what we said.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import ALL_PROVIDERS, CONTACT_PROVIDERS, SEARCH_PROVIDERS

revision = "4c7e2a9b50d3"
down_revision = "b9f3c6a21d47"
branch_labels = None
depends_on = None

SCHEMA = "app"
CONSTRAINT = "ck_provider_credentials_provider"


def _allowlist(names: tuple[str, ...]) -> str:
    return "provider IN (" + ", ".join(f"'{name}'" for name in names) + ")"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "provider_credentials", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        CONSTRAINT, "provider_credentials", _allowlist(ALL_PROVIDERS), schema=SCHEMA
    )

    op.create_table(
        "search_cache",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_norm", sa.String(length=300), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column(
            "hits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("org_id", "query_norm", name="uq_search_cache_org_query"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_search_cache_expires", "search_cache", ["expires_at"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_search_cache_expires", table_name="search_cache", schema=SCHEMA)
    op.drop_table("search_cache", schema=SCHEMA)

    # Deleted, because the constraint is on `provider` and ignores `revoked_at`: a revoked
    # search key violates the narrowed constraint exactly as a live one does. This destroys
    # credentials, which is why the module docstring says so in those words.
    names = ", ".join(f"'{name}'" for name in SEARCH_PROVIDERS)
    op.execute(
        sa.text(f"DELETE FROM {SCHEMA}.provider_credentials WHERE provider IN ({names})")
    )

    op.drop_constraint(CONSTRAINT, "provider_credentials", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        CONSTRAINT, "provider_credentials", _allowlist(CONTACT_PROVIDERS), schema=SCHEMA
    )
