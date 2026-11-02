"""Provider keys and proposed hiring contacts (F9, ADR-041).

Revision ID: 08d9c5fbdc1b
Revises: a2e94c710bd5
Create Date: 2026-11-02

Two new tables and nothing else. No existing row is read or changed, so applying this alters no
behaviour: until somebody saves their own provider key, every lookup is refused for not having
one, which is exactly what happens today.

**Both check constraints are generated from the tuples in `models.py`** rather than written out
here. A literal copied into a migration is a literal that cannot see the tuple change, and that
is this project's most repeated defect -- `ck_suppressions_reason` refusing the one-click route's
invented reason is the instance that cost the most.

`provider_credentials` holds ciphertext under the same Fernet key as `mail_credentials`. The
downgrade therefore **drops the keys**, which is stated here because a reverted migration that
silently loses a credential is a migration somebody reverts twice: the keys are the user's own
and are re-pasted in a moment, and keeping orphaned ciphertext around would be worse.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.models import (
    CONTACT_CANDIDATE_STATES,
    CONTACT_EMAIL_STATUSES,
    CONTACT_PROVIDERS,
    PROVIDER_CREDENTIAL_KINDS,
)

revision = "08d9c5fbdc1b"
down_revision = "a2e94c710bd5"
branch_labels = None
depends_on = None

SCHEMA = "app"


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column(
            "id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("label", sa.String(120)),
        sa.Column("added_by", sa.String(200)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("invalid_reason", sa.Text()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            _one_of("provider", CONTACT_PROVIDERS), name="ck_provider_credentials_provider"
        ),
        sa.CheckConstraint(
            _one_of("kind", PROVIDER_CREDENTIAL_KINDS), name="ck_provider_credentials_kind"
        ),
        schema=SCHEMA,
    )
    # Hard rule 8 in the schema: one live key per provider per workspace, so "rotate five keys to
    # dodge a free tier" is not a loop somebody can write later. Partial on `revoked_at IS NULL`,
    # so replacing a key the provider reissued still works -- what is refused is holding several.
    op.create_index(
        "uq_provider_credentials_live",
        "provider_credentials",
        ["org_id", "provider"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "contact_candidates",
        sa.Column(
            "id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_post_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="SET NULL"),
        ),
        # SET NULL rather than CASCADE: deleting a lead must not delete the record that a
        # provider once proposed that address, or the next lookup proposes them again.
        sa.Column(
            "lead_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL"),
        ),
        sa.Column("company_domain", sa.String(253), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("full_name", sa.String(300)),
        sa.Column("title", sa.String(300)),
        sa.Column("email", sa.String(320)),
        sa.Column("email_norm", sa.String(320)),
        sa.Column("email_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("confidence", sa.Integer()),
        sa.Column("profile_url", sa.String(2000)),
        sa.Column("state", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            _one_of("provider", CONTACT_PROVIDERS), name="ck_contact_candidates_provider"
        ),
        sa.CheckConstraint(
            _one_of("email_status", CONTACT_EMAIL_STATUSES),
            name="ck_contact_candidates_email_status",
        ),
        sa.CheckConstraint(
            _one_of("state", CONTACT_CANDIDATE_STATES), name="ck_contact_candidates_state"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "uq_contact_candidates_email",
        "contact_candidates",
        ["org_id", "email_norm"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("email_norm IS NOT NULL"),
    )
    op.create_index(
        "ix_contact_candidates_org_state",
        "contact_candidates",
        ["org_id", "state"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_contact_candidates_org_state", table_name="contact_candidates", schema=SCHEMA)
    op.drop_index("uq_contact_candidates_email", table_name="contact_candidates", schema=SCHEMA)
    op.drop_table("contact_candidates", schema=SCHEMA)
    op.drop_index("uq_provider_credentials_live", table_name="provider_credentials", schema=SCHEMA)
    op.drop_table("provider_credentials", schema=SCHEMA)
