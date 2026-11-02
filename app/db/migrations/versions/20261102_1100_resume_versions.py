"""Tailored resumes, their PDFs and the application tracker (F20, ADR-043).

Revision ID: 5a2c9e7b1f40
Revises: 3c1f80a7b4e2
Create Date: 2026-11-02

Three empty tables. No existing row is read or changed.

**`resume_versions` is immutable, and a trigger is what makes that true.** An `applications` row
points at the exact document that was sent, so the question somebody asks a month later -- "what
did they actually read?" -- has no answer if the row can be edited afterwards. The same device as
`app.events`, including the escape hatch: `app.allow_resume_version_delete` must be set for a
DELETE to pass, so an accidental `DELETE FROM app.resume_versions` fails while a lawful erasure
is possible and obvious in the code that performs it.

**The PDF is a separate table because of that immutability, not only because of its size.** A
rendering happens after the version exists, so there is nowhere on an unupdatable row to put it;
and `bytea` on the version would travel with every row of the versions list.

**Both check constraints on `applications` are generated from the tuples in `models.py`**, so the
vocabulary cannot drift from what the code writes. That drift -- a literal in a caller that
cannot see the constraint change -- is this project's most repeated defect, and it has cost it a
500 on every one-click unsubscribe once already.

The downgrade drops all three. Stated plainly: that deletes the record of which resume was sent
with which application. There is nothing to migrate it into, and a downgrade of this revision is
a decision to lose it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.models import APPLICATION_STATUSES

revision = "5a2c9e7b1f40"
down_revision = "3c1f80a7b4e2"
branch_labels = None
depends_on = None

SCHEMA = "app"

# Read from the models rather than retyped, for the reason the docstring gives.
_STATUSES = ",".join(f"'{value}'" for value in APPLICATION_STATUSES)


def upgrade() -> None:
    op.create_table(
        "resume_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("header", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "edit_ops",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column(
            "fact_ids",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_resume_versions_org_created",
        "resume_versions",
        ["org_id", "created_at"],
        schema=SCHEMA,
    )

    op.create_table(
        "resume_renders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resume_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.resume_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "rendered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("resume_version_id", name="uq_resume_renders_version"),
        schema=SCHEMA,
    )

    op.create_table(
        "applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "job_post_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resume_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{SCHEMA}.resume_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="planned"),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("follow_up_due", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "job_post_id", name="uq_applications_org_post"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_applications_status"),
        sa.CheckConstraint(
            "(status IN ('applied','interviewing') AND applied_at IS NOT NULL) "
            "OR (status = 'planned' AND applied_at IS NULL) "
            "OR status = 'closed'",
            name="ck_applications_applied_at",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_applications_org_status", "applications", ["org_id", "status"], schema=SCHEMA
    )

    # Immutability. UPDATE is refused outright; DELETE is refused unless the session has said it
    # means to, which keeps an accident loud and a lawful erasure deliberate.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.resume_versions_are_immutable()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE'
               AND coalesce(current_setting('app.allow_resume_version_delete', true), 'off')
                   = 'on' THEN
                RETURN OLD;
            END IF;
            RAISE EXCEPTION
                'app.resume_versions is immutable; % is not permitted. A change makes a new '
                'version, because an application points at the document that was sent '
                '(set app.allow_resume_version_delete for lawful erasure)', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_resume_versions_immutable
            BEFORE UPDATE OR DELETE ON app.resume_versions
            FOR EACH ROW EXECUTE FUNCTION app.resume_versions_are_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_resume_versions_immutable ON app.resume_versions")
    op.execute("DROP FUNCTION IF EXISTS app.resume_versions_are_immutable()")
    op.drop_index("ix_applications_org_status", table_name="applications", schema=SCHEMA)
    op.drop_table("applications", schema=SCHEMA)
    op.drop_table("resume_renders", schema=SCHEMA)
    op.drop_index("ix_resume_versions_org_created", table_name="resume_versions", schema=SCHEMA)
    op.drop_table("resume_versions", schema=SCHEMA)
