"""One optional file per campaign, attached to the emails it sends.

Revision ID: f2c7d1a60b94
Revises: d3b8c04e9a17

**The bytes live in their own table, not in a column on `campaigns`.** A campaign row is read
on every dashboard render -- `campaign_summary` builds every row of the campaigns list from it
-- and a `bytea` column would be dragged along by all of it, several megabytes at a time, for a
screen that wants a name and a status. A separate table means the blob is read exactly once per
send and never otherwise.

**And in Postgres rather than S3.** It is one file per campaign, capped at 5 MB: at any plausible
number of campaigns this is a rounding error against 23 GB free, it is included in the nightly
`pg_dump` that already exists, and it needs no bucket, no IAM policy and no second failure mode
where the database says a campaign has an attachment and the object store disagrees. Hard rule 5
also applies -- no new cloud resources without asking. If attachments ever became large or
numerous this is the thing to revisit, and ADR-016 says so.

`drafts.attach` is nullable on purpose: NULL means "nobody has said", and the campaign decides.
A reviewer who unticks the box writes `false`, and that survives.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "f2c7d1a60b94"
down_revision: str | None = "d3b8c04e9a17"
branch_labels: None = None
depends_on: None = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_id", UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        # Lets the UI say "this is the same file you uploaded last time" and lets an operator
        # confirm what actually went out, without downloading several megabytes to find out.
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("uploaded_by", sa.String(200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], [f"{SCHEMA}.campaigns.id"], ondelete="CASCADE"),
        # One file per campaign. Replacing it is an upsert rather than a second row, so there is
        # never a question of which one is attached.
        sa.UniqueConstraint("campaign_id", name="uq_attachments_campaign"),
        sa.CheckConstraint("size_bytes > 0", name="ck_attachments_size_positive"),
        sa.CheckConstraint(
            f"size_bytes <= {5 * 1024 * 1024}", name="ck_attachments_size_within_limit"
        ),
        schema=SCHEMA,
    )

    op.add_column(
        "drafts",
        sa.Column("attach", sa.Boolean(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("drafts", "attach", schema=SCHEMA)
    op.drop_table("attachments", schema=SCHEMA)
