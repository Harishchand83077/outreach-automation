"""Which addresses may this campaign send from?

**A question the API could not answer.** A mailbox could be created and never listed, so the New
campaign form asked for an address every time -- and anybody who meant the account they connected
last week typed it again. `POST /mailboxes` is idempotent on the address, so within one org that
quietly returned the same row; across campaigns with different addresses it made a second mailbox
that then had to be connected separately, with nothing on screen explaining why the new campaign
could not send.

`campaigns.mailbox_id` and `PATCH /campaigns/{id}` have accepted a choice since Phase 2. Nothing
produced the list to choose from. The signature defect again: a correct producer, a correct
consumer, and nothing in between.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.api.routers.campaigns import list_mailboxes
from app.core.settings import Settings
from app.db.models import Campaign, Mailbox, MailCredential, Org
from app.services.credentials import seal

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = Fernet.generate_key().decode("ascii")


def settings_with(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


class _User:
    """`org_id` comes from the membership, never from the caller (ADR-004)."""

    def __init__(self, org_id: uuid.UUID) -> None:
        self.org_id = org_id


async def _mailbox(db, org, address: str) -> Mailbox:
    row = Mailbox(org_id=org.id, from_name="Asha Rao", from_email=address, reply_to=address)
    db.add(row)
    await db.commit()
    return row


def _address(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


class TestListingTheSendingAccounts:
    async def test_the_org_sees_its_own_and_only_its_own(self, db, org):
        """ADR-004 asserted rather than assumed, on a route that is new."""
        mine = await _mailbox(db, org, _address("mine"))

        stranger = Org(name="Somebody Else")
        db.add(stranger)
        await db.commit()
        theirs = await _mailbox(db, stranger, _address("theirs"))

        body = await list_mailboxes(db, _User(org.id))

        ids = {row["id"] for row in body["mailboxes"]}
        assert str(mine.id) in ids
        assert str(theirs.id) not in ids

    async def test_an_unconnected_mailbox_is_listed_but_not_usable(self, db):
        """It has to appear, or somebody cannot tell it exists and makes a duplicate.

        It must not look like a choice, though: a picker that presents a dead end and a working
        account identically is one that sends people down the dead end.
        """
        fresh = Org(name="Brand New")
        db.add(fresh)
        await db.commit()
        mailbox = await _mailbox(db, fresh, _address("unconnected"))

        body = await list_mailboxes(db, _User(fresh.id))

        row = next(r for r in body["mailboxes"] if r["id"] == str(mailbox.id))
        assert row["connected"] is False
        assert row["usable"] is False

    async def test_a_connected_mailbox_is_usable_and_carries_its_countdown(self, db):
        fresh = Org(name="Connected")
        db.add(fresh)
        await db.commit()
        mailbox = await _mailbox(db, fresh, _address("connected"))
        db.add(
            MailCredential(
                org_id=fresh.id,
                mailbox_id=mailbox.id,
                kind="google_oauth",
                account_email=mailbox.from_email,
                secret=seal("refresh-token", settings_with()).token,
            )
        )
        await db.commit()

        body = await list_mailboxes(db, _User(fresh.id))

        row = next(r for r in body["mailboxes"] if r["id"] == str(mailbox.id))
        assert row["connected"] is True
        assert row["usable"] is True
        # The seven days travel with the row, so the picker can say which account is about to
        # stop rather than offering it as though nothing were wrong.
        assert row["days_left"] == 7

    async def test_a_lapsed_credential_is_reported_as_needing_a_reconnect(self, db):
        fresh = Org(name="Lapsed")
        db.add(fresh)
        await db.commit()
        mailbox = await _mailbox(db, fresh, _address("lapsed"))
        db.add(
            MailCredential(
                org_id=fresh.id,
                mailbox_id=mailbox.id,
                kind="google_oauth",
                account_email=mailbox.from_email,
                secret=seal("refresh-token", settings_with()).token,
                invalid_reason="Google would not renew this connection.",
            )
        )
        await db.commit()

        body = await list_mailboxes(db, _User(fresh.id))

        row = next(r for r in body["mailboxes"] if r["id"] == str(mailbox.id))
        assert row["needs_reconnect"] is True
        assert row["connected"] is False
        assert row["usable"] is False

    async def test_no_provider_reason_is_echoed_back_to_the_browser(self, db):
        """`invalid_reason` can quote a provider's reply, and this reaches screenshots.

        The same rule the send loop's per-mailbox reasons follow: say that it is broken and what
        to do, never repeat stored text verbatim.
        """
        fresh = Org(name="Quiet")
        db.add(fresh)
        await db.commit()
        mailbox = await _mailbox(db, fresh, _address("quiet"))
        db.add(
            MailCredential(
                org_id=fresh.id,
                mailbox_id=mailbox.id,
                kind="google_oauth",
                account_email=mailbox.from_email,
                secret=seal("refresh-token", settings_with()).token,
                invalid_reason="SECRET-PROVIDER-TEXT",
            )
        )
        await db.commit()

        body = await list_mailboxes(db, _User(fresh.id))

        assert "SECRET-PROVIDER-TEXT" not in str(body)

    async def test_it_says_how_many_campaigns_use_each_account(self, db, org, campaign):
        """Changing a shared account's sender is not the quiet edit it looks like."""
        mailbox = await _mailbox(db, org, _address("shared"))
        campaign.mailbox_id = mailbox.id
        db.add(
            Campaign(
                org_id=org.id,
                name=f"Second {uuid.uuid4().hex[:6]}",
                status="draft",
                mailbox_id=mailbox.id,
                persona={"sender_name": "Asha Rao"},
            )
        )
        await db.commit()

        body = await list_mailboxes(db, _User(org.id))

        row = next(r for r in body["mailboxes"] if r["id"] == str(mailbox.id))
        assert row["campaigns"] == 2

    async def test_a_workspace_with_no_mailbox_gets_an_empty_list_not_an_error(self, db):
        """The first screen a new person sees. It must not be a red banner."""
        fresh = Org(name="Nothing Yet")
        db.add(fresh)
        await db.commit()

        assert await list_mailboxes(db, _User(fresh.id)) == {"mailboxes": []}
