"""Per-mailbox credentials, so ten people can each send from their own Gmail.

Revision ID: b93d7e4a1c58
Revises: a81f3c6d2e07

Until now there was one sending account for the whole deployment, from `SMTP_EMAIL` and
`SMTP_PASSWORD` in the environment. That is correct for one person and impossible for ten.

**Its own table, not columns on `mailboxes`.** `mailboxes` is read on every send-loop cycle, and
a secret should not be dragged through a query that does not need it -- the same reasoning that
put attachment bytes in their own table (ADR-016). It also means the one table holding secrets
is a single, obvious thing to reason about, exclude from a dump, or re-encrypt.

The refresh token is stored **encrypted** (`app/services/credentials.py`, Fernet, key from the
environment), so the nightly dump that ships to S3 contains no usable Gmail credential.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "b93d7e4a1c58"
down_revision: str | None = "a81f3c6d2e07"
branch_labels: None = None
depends_on: None = None

SCHEMA = "app"


def upgrade() -> None:
    op.create_table(
        "mail_credentials",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("mailbox_id", UUID(as_uuid=True), nullable=False),
        # google_oauth | app_password. The second keeps the existing environment-credential
        # path valid, so the live deployment goes on sending while this lands.
        sa.Column("kind", sa.String(32), nullable=False),
        # The address Google told us this token belongs to. Checked against the mailbox's
        # `from_email` at connect time: authorising the wrong account is an easy mistake to make
        # when several are signed in, and it fails at send time otherwise.
        sa.Column("account_email", sa.String(320), nullable=False),
        # Encrypted. Never returned by a route, never logged, never in an event payload.
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=True),
        # When the short-lived access token we last minted stops working. Nullable because an
        # app password does not expire.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        # Set when a refresh fails. An OAuth client in Testing status expires refresh tokens
        # after 7 days, so this is an expected state with a known fix, not an exception: the
        # mailbox is paused with a reason and the dashboard asks for a reconnect.
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("connected_by", sa.String(200), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mailbox_id"], [f"{SCHEMA}.mailboxes.id"], ondelete="CASCADE"),
        # One credential per mailbox. Reconnecting replaces it rather than adding a second,
        # or there would be a question about which one a send uses.
        sa.UniqueConstraint("mailbox_id", name="uq_mail_credentials_mailbox"),
        sa.CheckConstraint("kind IN ('google_oauth','app_password')", name="ck_mail_cred_kind"),
        schema=SCHEMA,
    )

    # Where an OAuth round trip is parked while the user is at Google. Short-lived rows with a
    # `state` nonce: without one, anybody could hand a signed-in user a crafted callback URL and
    # attach *their* mailbox to that user's org.
    op.create_table(
        "oauth_states",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("mailbox_id", UUID(as_uuid=True), nullable=True),
        sa.Column("redirect_to", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], [f"{SCHEMA}.orgs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("state", name="uq_oauth_states_state"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_oauth_states_created", "oauth_states", ["created_at"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_states_created", table_name="oauth_states", schema=SCHEMA)
    op.drop_table("oauth_states", schema=SCHEMA)
    op.drop_table("mail_credentials", schema=SCHEMA)
