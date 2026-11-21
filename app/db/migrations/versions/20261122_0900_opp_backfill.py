"""S19.2c — link applications to opportunities; add channel.

Revision ID: a82c5f71e0b4
Revises: f71c9d84b2e5
Create Date: 2026-11-22

**needs-human review before running in production.**

This migration alters the existing `applications` table (adds two columns and
creates one opportunity row per existing application) so it cannot be run safely
without an operator confirming the target database is the right one and there are
no concurrent writes in progress.

The safe sequence on the production VM is:

    1. Stop the worker:        systemctl stop outreach-worker   (or Compose equivalent)
    2. Verify the target:      python -m app.scripts.check_env  — confirm DB=outreach
    3. Run:                    alembic upgrade head
    4. Inspect the result:     SELECT COUNT(*) FROM opportunities;
                               SELECT COUNT(*) FROM applications WHERE opportunity_id IS NULL;
    5. Restart the worker:     systemctl start outreach-worker

The migration adds:
  - applications.opportunity_id (nullable UUID FK → opportunities.id SET NULL)
  - applications.channel (varchar 40, nullable)

It then inserts one `saved` opportunity per existing application row (using the
application's job_post_id and org_id to populate company_domain and title from
job_posts), and writes opportunity_id back on the application row that produced it.
Rows without a matching job_post are still given an opportunity (title "Imported
application"), so the FK stays non-null only after the second UPDATE.

Downgrade drops both columns and their index. The orphaned opportunity rows are
left in place on downgrade — a data-touching migration should not delete rows it
created, as they may already have been edited.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a82c5f71e0b4"
down_revision = "f71c9d84b2e5"
branch_labels = None
depends_on = None

_SCHEMA = "public"


def upgrade() -> None:
    # 1. New columns on applications
    op.add_column(
        "applications",
        sa.Column(
            "opportunity_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey(
                f"{_SCHEMA}.opportunities.id",
                ondelete="SET NULL",
                name="fk_applications_opportunity_id",
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "applications",
        sa.Column("channel", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_applications_opportunity_id",
        "applications",
        ["opportunity_id"],
        unique=False,
    )

    # 2. Create one opportunity per existing application and link it back.
    #    Done in SQL so it runs in one round trip and uses no ORM state.
    conn = op.get_bind()

    # Insert opportunities, returning (opp.id, source application.id).
    # The title comes from job_posts.title if available, else "Imported application".
    conn.execute(
        sa.text(
            """
            WITH inserted AS (
                INSERT INTO opportunities (
                    id, org_id, job_post_id, company_domain, title,
                    status, created_at, updated_at
                )
                SELECT
                    gen_random_uuid(),
                    a.org_id,
                    a.job_post_id,
                    jp.company_domain,
                    COALESCE(jp.title, 'Imported application'),
                    'saved',
                    NOW(),
                    NOW()
                FROM applications a
                LEFT JOIN job_posts jp ON jp.id = a.job_post_id
                RETURNING id AS opp_id, job_post_id, org_id
            )
            UPDATE applications a
            SET opportunity_id = i.opp_id
            FROM inserted i
            WHERE i.job_post_id = a.job_post_id
              AND i.org_id = a.org_id
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_applications_opportunity_id", table_name="applications")
    op.drop_column("applications", "opportunity_id")
    op.drop_column("applications", "channel")
    # Orphaned opportunity rows from the upgrade are intentionally left in place.
