"""Connecting Gmail must act on the mailbox the operator is looking at.

**Written because it did not, and because this router had no tests at all.**

`_mailbox_for` falls back to `order_by(created_at).limit(1)` -- the org's *oldest* mailbox --
when no `mailbox_id` is given. That fallback is right for the ordinary case, a new workspace with
one mailbox. It is wrong the moment an org has two, and the dashboard was never sending an id:
`App.jsx` called `/connect/google/status` and `/connect/google/start` with nothing at all.

So on an org with three mailboxes, the Sending-account panel described, and Connect Gmail would
have connected, whichever mailbox happened to be created first -- regardless of which campaign
was on screen. The callback refuses an account whose address does not match the mailbox's own,
so the only symptom would have been a mismatch message naming an address from a *different*
campaign, which reads like the wrong Google account was picked.

The same shape as every other defect in this project: a correct producer (the routes, which have
always accepted `mailbox_id`) and a correct consumer (`_mailbox_for`, which has always honoured
it), with nothing checking that the caller actually passed it.

Two halves are asserted here. This file holds the server's half -- that an id is honoured and
that it cannot reach across orgs. `tests/unit/test_connect_sends_mailbox_id.py` holds the
caller's half, because an API that honours a parameter nobody sends is the bug itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routers.connect import _mailbox_for
from app.db.models import Mailbox, Org

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class _User:
    """Only `org_id` is read, and ADR-004 is the reason: the org comes from the membership,
    never from the caller. Passing a whole authenticated user here would suggest otherwise."""

    org_id: uuid.UUID


async def _mailbox(db: AsyncSession, org_id: uuid.UUID, address: str, *, age_days: int) -> Mailbox:
    """A mailbox with an explicit age, because the fallback orders by `created_at` and rows
    written in one transaction can otherwise share a timestamp and order arbitrarily."""
    row = Mailbox(
        org_id=org_id,
        from_name="Asha",
        from_email=address,
        reply_to=address,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )
    db.add(row)
    await db.flush()
    return row


class TestTheMailboxThatGetsConnected:
    async def test_an_explicit_id_wins_over_the_oldest(self, db: AsyncSession, org) -> None:
        """The regression. Without this the newest mailbox was unreachable from the dashboard."""
        oldest = await _mailbox(db, org.id, "first@example.com", age_days=30)
        wanted = await _mailbox(db, org.id, "second@example.com", age_days=1)

        chosen = await _mailbox_for(db, _User(org.id), wanted.id)

        assert chosen is not None
        assert chosen.id == wanted.id, (
            "asking for a specific mailbox returned a different one, so the dashboard would "
            "connect an account the operator never looked at"
        )
        assert chosen.id != oldest.id

    async def test_no_id_still_falls_back_to_the_oldest(self, db: AsyncSession, org) -> None:
        """The fallback is kept deliberately: a new workspace has exactly one mailbox and no
        campaign to take an id from, and refusing to answer there would make the panel unusable
        for every ordinary user in order to serve the multi-mailbox case."""
        oldest = await _mailbox(db, org.id, "first@example.com", age_days=30)
        await _mailbox(db, org.id, "second@example.com", age_days=1)

        chosen = await _mailbox_for(db, _User(org.id), None)

        assert chosen is not None
        assert chosen.id == oldest.id

    async def test_an_id_from_another_org_resolves_to_nothing(self, db: AsyncSession, org) -> None:
        """ADR-004, asserted rather than assumed: the id is a caller-supplied value, and the one
        thing it must never do is reach a row belonging to somebody else. The org is filtered
        first, so a foreign id matches no row at all."""
        stranger = Org(name="Someone Else")
        db.add(stranger)
        await db.flush()
        theirs = await _mailbox(db, stranger.id, "theirs@example.com", age_days=5)
        await _mailbox(db, org.id, "ours@example.com", age_days=5)

        chosen = await _mailbox_for(db, _User(org.id), theirs.id)

        assert chosen is None, "a mailbox id from another org must not resolve"

    async def test_an_unknown_id_resolves_to_nothing_rather_than_the_oldest(
        self, db: AsyncSession, org
    ) -> None:
        """Falling back here would be worse than failing: the caller asked for a specific
        mailbox, so quietly substituting another is how the wrong account gets connected while
        the screen says it worked."""
        await _mailbox(db, org.id, "ours@example.com", age_days=5)

        assert await _mailbox_for(db, _User(org.id), uuid.uuid4()) is None
