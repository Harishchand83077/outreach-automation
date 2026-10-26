"""The addresses one org may send to while its send mode is `allowlist`.

**The values, not the decision.** `allowlist_permits()` in `smtp.py` is still the only function in
the tree that compares a recipient against anything, and `tests/unit/test_send_guard.py` asserts
that structurally over every file. This module only loads the list and hands it over -- exactly
the division ADR-010 made when the *mode* moved out of the environment and into `orgs.send_mode`.

It is loaded on **every** send, whatever the mode, and not skipped when the mode is `live` or
`disabled`. Skipping would mean asking "which mode are we in?" out here, which is the one question
hard rule 1 says may be asked in one place only. One indexed lookup per message, at a hundred
messages a day, is not a cost worth buying a second copy of the gate with.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SendAllowlistEntry


async def allowed_for(session: AsyncSession, org_id: uuid.UUID) -> frozenset[str]:
    """Every address this org has allow-listed, normalised for comparison.

    An empty set is a real answer: it means this org may send to nobody while it is in
    `allowlist` mode. The caller must not treat that as "unknown, so permit" -- `allowlist_permits`
    takes `None` and an empty collection to mean the same thing for exactly that reason.
    """
    rows = (
        await session.scalars(
            select(SendAllowlistEntry.email_norm).where(SendAllowlistEntry.org_id == org_id)
        )
    ).all()
    return frozenset(row.strip().lower() for row in rows if row and row.strip())
