"""allow hashed suppressions for erasure

Revision ID: cad167cbb0b0
Revises: e70c0c9bd226
Created: 2026-10-11 08:37:31.600359+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'cad167cbb0b0'
down_revision: str | None = 'e70c0c9bd226'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    """Widen `suppressions.kind` to admit `email_hash`.

    Erasure must leave something behind that still recognises the address, or forgetting
    somebody would make them contactable again on the next import — turning a privacy request
    into the harm it was meant to prevent. The digest is that something.

    Additive: no existing row changes, and the two existing kinds remain valid.
    """
    op.drop_constraint("ck_suppressions_kind", "suppressions", schema="app", type_="check")
    op.create_check_constraint(
        "ck_suppressions_kind",
        "suppressions",
        "kind IN ('email','domain','email_hash')",
        schema="app",
    )


def downgrade() -> None:
    """Refuse to narrow the constraint while hashed rows exist.

    Dropping them would silently un-suppress people who asked to be erased. If this really must
    be reversed, delete those rows deliberately first — and know what it means.
    """
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text("SELECT count(*) FROM app.suppressions WHERE kind = 'email_hash'")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} hashed suppression(s) exist. Narrowing this constraint would "
            "un-suppress erased recipients; remove them deliberately first."
        )
    op.drop_constraint("ck_suppressions_kind", "suppressions", schema="app", type_="check")
    op.create_check_constraint(
        "ck_suppressions_kind",
        "suppressions",
        "kind IN ('email','domain')",
        schema="app",
    )
