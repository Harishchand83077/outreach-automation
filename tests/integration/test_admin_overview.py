"""The one screen that shows all three gates together.

Three different systems decide whether a person can send, and nothing showed them side by side:
**Supabase** decides whether they may sign in, **this database** decides which workspace they are
in and what they may do, and **Google** decides whether they may connect a mailbox at all while
the OAuth client is unpublished.

So "why can this person not send?" meant checking a Supabase dashboard, a Google Cloud console
and a `psql` session, and holding the answer in your head.
"""

# Importing a fixture is how pytest shares one between modules, and taking it as a parameter
# then "redefines" the imported name -- which is exactly what is supposed to happen.
# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.core.settings import Settings
from app.db.models import Invitation, Mailbox, MailCredential, Member, Org
from app.services.credentials import seal

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = Fernet.generate_key().decode("ascii")


def _settings() -> Settings:
    return Settings(_env_file=None, GROQ_API_KEY="gsk_test", CREDENTIAL_KEY=KEY)  # type: ignore[arg-type]


def _address(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


async def _mailbox(db, org, address: str, *, connected: bool, invalid: str | None = None):
    row = Mailbox(org_id=org.id, from_name="Asha", from_email=address, reply_to=address)
    db.add(row)
    await db.flush()
    if connected:
        db.add(
            MailCredential(
                org_id=org.id,
                mailbox_id=row.id,
                kind="google_oauth",
                account_email=address,
                secret=seal("refresh-token", _settings()).token,
                connected_by="asha@example.com",
                invalid_reason=invalid,
            )
        )
    await db.commit()
    return row


class TestWhoIsInThisWorkspace:
    async def test_members_and_invitations_are_both_listed(self, api, owner, db, org):
        invited = _address("waiting")
        db.add(Invitation(org_id=org.id, email=invited, role="member", invited_by="me"))
        await db.commit()

        body = api.get("/api/v1/admin/overview", headers=owner).json()

        assert any(person["is_you"] for person in body["people"])
        assert invited in [row["email"] for row in body["invited"]]

    async def test_a_member_row_is_the_evidence_they_signed_in(self, api, owner):
        """We cannot see Supabase from here and do not pretend to.

        A `members` row is written on the first authenticated request, so its existence is the
        only honest signal this system has that the identity provider let them in.
        """
        body = api.get("/api/v1/admin/overview", headers=owner).json()

        assert all(person["signed_in"] is True for person in body["people"])
        assert all(row["signed_in"] is False for row in body["invited"])


class TestWhatEachMailboxNeeds:
    async def test_a_connected_and_an_unconnected_mailbox_are_told_apart(self, api, db, keypair):
        """In a workspace of its own, so the counts are about these two mailboxes only."""
        fresh = Org(name="Mixed")
        db.add(fresh)
        await db.commit()
        sub = f"sub-{uuid.uuid4()}"
        db.add(Member(org_id=fresh.id, user_id=sub, email=_address("a"), role="owner"))
        await db.commit()
        good = await _mailbox(db, fresh, _address("good"), connected=True)
        bare = await _mailbox(db, fresh, _address("bare"), connected=False)

        private, _ = keypair
        body = api.get(
            "/api/v1/admin/overview",
            headers={"Authorization": f"Bearer {token_for(private, subject=sub)}"},
        ).json()

        rows = {row["from_email"]: row for row in body["mailboxes"]}
        assert rows[good.from_email]["connected"] is True
        assert rows[bare.from_email]["connected"] is False
        assert body["totals"]["connected"] == 1
        assert body["totals"]["mailboxes"] == 2

    async def test_a_lapsed_credential_is_counted_as_needing_a_reconnect(self, api, owner, org, db):
        await _mailbox(
            db, org, _address("lapsed"), connected=True, invalid="Google would not renew this."
        )

        body = api.get("/api/v1/admin/overview", headers=owner).json()

        assert body["totals"]["needs_reconnect"] >= 1

    async def test_no_provider_text_is_echoed(self, api, owner, org, db):
        """`invalid_reason` can quote a provider's reply, and this reaches screenshots."""
        await _mailbox(db, org, _address("quiet"), connected=True, invalid="SECRET-PROVIDER-TEXT")

        rendered = api.get("/api/v1/admin/overview", headers=owner).text

        assert "SECRET-PROVIDER-TEXT" not in rendered


class TestTheStepThisSystemCannotTake:
    async def test_it_names_the_google_console_rather_than_guessing(self, api, owner):
        """There is no API for the OAuth consent screen's test-user list.

        Reporting it as an unknown with a link is honest. Reporting it as a tick would be a
        number somebody trusts and nothing maintains.
        """
        body = api.get("/api/v1/admin/overview", headers=owner).json()

        assert body["google_test_users"]["required_while_unpublished"] is True
        assert "console.cloud.google.com" in body["google_test_users"]["console_url"]


class TestOnlyAdminsSeeIt:
    async def test_a_member_is_refused(self, api, member):
        assert api.get("/api/v1/admin/overview", headers=member).status_code == 403

    async def test_it_never_returns_a_credential(self, api, owner, org, db):
        await _mailbox(db, org, _address("creds"), connected=True)

        rendered = api.get("/api/v1/admin/overview", headers=owner).text.lower()

        for forbidden in ("password", "secret", "refresh-token", "api_key"):
            assert forbidden not in rendered
