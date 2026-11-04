"""A watchlist of companies, and the signals they produce (F11, ADR-049).

Revision ID: 1f6b03a9e5c4
Revises: 8d1a4f60c3b7
Create Date: 2026-11-05

Two empty tables. No existing row is read or changed, and **no workspace gains a watch or a
signal by being migrated**: a person adds a company, and until they do the daily poll behaves
exactly as it did -- it stores postings for the job-hunt side and produces nothing.

`signal_events` carries its own `domain` as well as a nullable `watchlist_id`, which looks like
duplication and is the point: `ON DELETE SET NULL` means forgetting a company keeps the record
that it was hiring in March, and a signal that could not say which company it was about would be
a row nobody could read. The same reasoning as `booking_events` surviving a lead's erasure.

The unique key `(org_id, kind, source, external_id)` is what makes the daily poll harmless. It
sees yesterday's postings again every time it runs, so without it every morning would add a
fresh copy of every open role and the list would be unusable inside a week -- the same device,
for the same reason, as the booking webhook's key against provider retries.

The downgrade drops both tables. Nothing else refers to them, and no lead, message or draft is
touched: a signal is a reason to write that somebody either used or did not, and nothing was
ever sent because of one without a person approving it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import SIGNAL_KINDS, SIGNAL_STATES

revision = "1f6b03a9e5c4"
down_revision = "8d1a4f60c3b7"
branch_labels = None
depends_on = None

SCHEMA = "app"


def _allowlist(column: str, names: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{name}'" for name in names) + ")"


def upgrade() -> None:
    op.create_table(
        "watchlist",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("label", sa.String(length=300)),
        sa.Column("ats_source", sa.String(length=20)),
        sa.Column("ats_slug", sa.String(length=200)),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True)),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_signal_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        # SET NULL, so deleting a campaign does not delete the watch: a company is worth
        # watching before any campaign exists, which is the ordinary case.
        sa.ForeignKeyConstraint(
            ["campaign_id"], [f"{SCHEMA}.campaigns.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("org_id", "domain", name="uq_watchlist_org_domain"),
        sa.CheckConstraint(
            "ats_source IS NULL OR ats_source IN ('greenhouse','lever','ashby')",
            name="ck_watchlist_ats_source",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_watchlist_org", "watchlist", ["org_id"], schema=SCHEMA)
    op.create_index(
        "ix_watchlist_board", "watchlist", ["org_id", "ats_source", "ats_slug"], schema=SCHEMA
    )

    op.create_table(
        "signal_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("watchlist_id", postgresql.UUID(as_uuid=True)),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=2000)),
        sa.Column("job_post_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="new"),
        sa.Column("observed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        # All three are SET NULL rather than CASCADE: forgetting a company, archiving a posting
        # or erasing a lead must not delete the record that the thing happened.
        sa.ForeignKeyConstraint(
            ["watchlist_id"], [f"{SCHEMA}.watchlist.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["job_post_id"], [f"{SCHEMA}.job_posts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["lead_id"], [f"{SCHEMA}.leads.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "org_id", "kind", "source", "external_id", name="uq_signal_events_org_kind_ext"
        ),
        sa.CheckConstraint(_allowlist("kind", SIGNAL_KINDS), name="ck_signal_events_kind"),
        sa.CheckConstraint(_allowlist("state", SIGNAL_STATES), name="ck_signal_events_state"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_signal_events_org_created", "signal_events", ["org_id", "created_at"], schema=SCHEMA
    )
    op.create_index(
        "ix_signal_events_org_state", "signal_events", ["org_id", "state"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_signal_events_org_state", table_name="signal_events", schema=SCHEMA)
    op.drop_index("ix_signal_events_org_created", table_name="signal_events", schema=SCHEMA)
    op.drop_table("signal_events", schema=SCHEMA)
    op.drop_index("ix_watchlist_board", table_name="watchlist", schema=SCHEMA)
    op.drop_index("ix_watchlist_org", table_name="watchlist", schema=SCHEMA)
    op.drop_table("watchlist", schema=SCHEMA)
