"""The signed booking webhook, and the bookings it records (F15, ADR-045).

Revision ID: b9f3c6a21d47
Revises: 7e1d4bc8a902
Create Date: 2026-11-03

Two empty tables. No existing row is read or changed, and **no deployment gains a webhook by
being migrated**: a workspace receives bookings only after an admin generates a secret, so the
public route answers the same 204 after this lands as it would have before it existed.

That is the ADR-010 and ADR-017 rule applied again. This change adds the second unauthenticated
write in the system; a migration that switched it on for every workspace at once would be the
worst default available.

`booking_events.lead_id` is `ON DELETE SET NULL`: erasing a lead must not erase the record that a
meeting happened, and `DELETE /leads/{id}` is a data-subject erasure rather than a correction. The
booking keeps the invitee's address, which is the one field that would then still identify them --
so lead erasure clears it, and `PRIVACY.md` says so.

The unique key `(org_id, provider, external_id)` is what makes a retried delivery harmless. Every
booking tool retries on a non-2xx and some retry on a 2xx as well, so without it a provider's
ordinary behaviour would put a second meeting in every funnel this feeds.

The downgrade drops both tables, which deletes the record of which meetings were booked through a
webhook. The leads that reached `MEETING_BOOKED` stay there -- that state is terminal and its
audit events are in `app.events`, which no downgrade touches.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import BOOKING_EVENT_KINDS, BOOKING_MATCHES

revision = "b9f3c6a21d47"
down_revision = "7e1d4bc8a902"
branch_labels = None
depends_on = None

SCHEMA = "app"


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "booking_webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    # Partial, so replacing a secret still works while *holding* two live ones does not -- a
    # second live row would mean a revoked secret kept working. The same device as
    # `uq_provider_credentials_live`.
    op.create_index(
        "uq_booking_webhooks_live",
        "booking_webhooks",
        ["org_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "booking_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(length=40), nullable=False, server_default="unknown"),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="booked"),
        sa.Column("invitee_email_norm", sa.String(length=320), nullable=True),
        sa.Column("invitee_name", sa.String(length=300), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("matched_by", sa.String(length=20), nullable=False, server_default="unmatched"),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "org_id", "provider", "external_id", name="uq_booking_events_org_provider_ext"
        ),
        # Generated from the tuples in `models.py`, so the constraint and the code that writes
        # these columns cannot disagree about the vocabulary. That drift has already cost this
        # project a 500 on every one-click unsubscribe.
        sa.CheckConstraint(_one_of("kind", BOOKING_EVENT_KINDS), name="ck_booking_events_kind"),
        sa.CheckConstraint(
            _one_of("matched_by", BOOKING_MATCHES), name="ck_booking_events_matched_by"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_booking_events_org_received",
        "booking_events",
        ["org_id", "received_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_booking_events_org_received", table_name="booking_events", schema=SCHEMA)
    op.drop_table("booking_events", schema=SCHEMA)
    op.drop_index("uq_booking_webhooks_live", table_name="booking_webhooks", schema=SCHEMA)
    op.drop_table("booking_webhooks", schema=SCHEMA)
