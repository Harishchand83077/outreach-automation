"""Say which org owns the credentials in `.env`.

Until now `login_for` fell back to `SMTP_EMAIL`/`SMTP_PASSWORD` for **any** mailbox with no
credential of its own. That was written when there was one org, and for a single-tenant install
it is still exactly right. Public sign-up changes what it means: a stranger's unconnected
mailbox would authenticate as the deployment's own Gmail, spending the operator's daily quota and
staking the operator's sending reputation on mail the operator never saw.

The fallback needs an owner, and ownership has to be a fact in the database rather than a guess
from an address -- `uq_mailboxes_org_from_email` is scoped *per org*, so a second org may create
a mailbox named after the operator's own address, and an address-only rule would hand that
stranger the password. The column is the answer, and nothing in the API writes it.

**Grants it to the oldest org only.** On this deployment that is the one `members` row inserted
by hand in `DEPLOY.md` §4.1, long before anyone could sign up. Picking the oldest rather than
updating every row matters if a workspace was ever created in between: the operator's install is
the first one, and the grant should not widen just because the migration ran late.

Reversible: dropping the column restores the previous behaviour exactly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c7a2e94b5d31"
down_revision: str | None = "b93d7e4a1c58"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column(
            "uses_environment_mailbox",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        schema="app",
    )
    # The deployment's own workspace, and only it. `LIMIT 1` inside `IN` rather than `=` so the
    # statement is valid on an empty table: a fresh install has no orgs yet and must not fail.
    op.execute(
        """
        UPDATE app.orgs SET uses_environment_mailbox = true
        WHERE id IN (SELECT id FROM app.orgs ORDER BY created_at, id LIMIT 1)
        """
    )


def downgrade() -> None:
    op.drop_column("orgs", "uses_environment_mailbox", schema="app")
