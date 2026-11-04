"""Entities and edges, where an edge without a source is refused (F12, ADR-051).

Revision ID: 6a2d47c9b8e1
Revises: 1f6b03a9e5c4
Create Date: 2026-11-05

Two empty tables. No existing row is read or changed, and no workspace gains a graph by being
migrated: edges arrive from the signal producer, which only runs for a company somebody watches.

**`ck_edges_provenance` is the slice.** An edge must carry a `source_url` or a `message_id`, so
"we know Acme is hiring" cannot be stored without the thing that says so. It is a check
constraint rather than a rule in a service because a service can be bypassed by the next writer
and a constraint cannot -- and because the sentences these rows feed are sentences somebody will
repeat to a stranger. A claim whose source nobody can produce is the thing this system exists
not to say.

**`uq_edges_claim` is declared `NULLS NOT DISTINCT`**, which is the part worth reading twice.
Three of its seven columns are null in any given row -- an edge names an object *or* a text, and
carries a URL *or* a message id -- and under Postgres's default rule two NULLs never collide, so
the index would have deduplicated **nothing at all**. The daily poll sees yesterday's postings
again every morning, so that index silently not working means the graph grows a duplicate claim
a day until a brief cites the same sentence six times. Requires Postgres 15; this project runs
17 in development and in production, checked rather than assumed.

`entities.lead_id` and `edges.message_id` differ on purpose. Erasing a lead must not delete what
we learned about their *employer*, so that one is `SET NULL` -- but an edge whose only provenance
is a message must go with the message, because a claim that outlived its source is exactly the
claim this table refuses to store.

The downgrade drops both tables. Nothing else refers to them, and no lead, message or draft is
touched.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import EDGE_PREDICATES, ENTITY_KINDS

revision = "6a2d47c9b8e1"
down_revision = "1f6b03a9e5c4"
branch_labels = None
depends_on = None

SCHEMA = "app"


def _allowlist(column: str, names: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{name}'" for name in names) + ")"


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("norm_key", sa.String(length=300), nullable=False),
        sa.Column("domain", sa.String(length=253)),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        # SET NULL: erasing a lead must not delete what we learned about their employer.
        sa.ForeignKeyConstraint(["lead_id"], [f"{SCHEMA}.leads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("org_id", "kind", "norm_key", name="uq_entities_org_kind_key"),
        sa.CheckConstraint(_allowlist("kind", ENTITY_KINDS), name="ck_entities_kind"),
        schema=SCHEMA,
    )
    op.create_index("ix_entities_org_domain", "entities", ["org_id", "domain"], schema=SCHEMA)

    op.create_table(
        "edges",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("predicate", sa.String(length=30), nullable=False),
        sa.Column("object_id", postgresql.UUID(as_uuid=True)),
        sa.Column("object_text", sa.String(length=500)),
        sa.Column("source_url", sa.String(length=2000)),
        sa.Column("message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["subject_id"], [f"{SCHEMA}.entities.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["object_id"], [f"{SCHEMA}.entities.id"], ondelete="CASCADE"),
        # CASCADE, unlike `entities.lead_id`: a claim whose only source is a message must go
        # with that message, because a claim that outlived its source is what this table refuses.
        sa.ForeignKeyConstraint(["message_id"], [f"{SCHEMA}.messages.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "source_url IS NOT NULL OR message_id IS NOT NULL", name="ck_edges_provenance"
        ),
        sa.CheckConstraint(
            "(object_id IS NOT NULL AND object_text IS NULL) "
            "OR (object_id IS NULL AND object_text IS NOT NULL)",
            name="ck_edges_object",
        ),
        sa.CheckConstraint(
            _allowlist("predicate", EDGE_PREDICATES), name="ck_edges_predicate"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_edges_org_subject", "edges", ["org_id", "subject_id"], schema=SCHEMA)
    # See the module docstring: without NULLS NOT DISTINCT this index deduplicates nothing.
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX uq_edges_claim ON {SCHEMA}.edges "
            "(org_id, subject_id, predicate, object_id, object_text, source_url, message_id) "
            "NULLS NOT DISTINCT"
        )
    )


def downgrade() -> None:
    op.drop_index("uq_edges_claim", table_name="edges", schema=SCHEMA)
    op.drop_index("ix_edges_org_subject", table_name="edges", schema=SCHEMA)
    op.drop_table("edges", schema=SCHEMA)
    op.drop_index("ix_entities_org_domain", table_name="entities", schema=SCHEMA)
    op.drop_table("entities", schema=SCHEMA)
