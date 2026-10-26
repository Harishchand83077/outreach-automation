"""Ten people on one deployment, and none of them able to reach another's data.

Tenancy was built in Phase 1 (ADR-004) and has been tested per-repository ever since. What is new
is that strangers can now *create* workspaces, so the tests that matter here are the ones that
check a second org gets its own everything and can see none of the first's.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.core.settings import Settings
from app.db.models import Campaign, Lead, Mailbox, MailCredential, Member, Org
from app.services.credentials import seal
from app.services.mail.connection import MailboxUnavailable, connection_status, login_for
from app.services.signup import SignupRefused, provision

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = Fernet.generate_key().decode("ascii")


def settings_with(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "SMTP_EMAIL": "shared@outreach.example",
        "SMTP_PASSWORD": "from-the-environment",
        "CREDENTIAL_KEY": KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


class TestSigningUp:
    async def test_a_new_person_gets_their_own_workspace_and_owns_it(self, db):
        member = await provision(
            db,
            user_id=f"sub-{uuid.uuid4()}",
            email="asha@arkvane.in",
            settings=settings_with(SIGNUP_MODE="open"),
        )
        await db.commit()

        assert member.role == "owner"
        org = await db.get(Org, member.org_id)
        assert org is not None
        assert org.name == "Asha"

    async def test_two_people_get_two_workspaces(self, db):
        settings = settings_with(SIGNUP_MODE="open")

        one = await provision(
            db, user_id=f"sub-{uuid.uuid4()}", email="a@example.com", settings=settings
        )
        two = await provision(
            db, user_id=f"sub-{uuid.uuid4()}", email="b@example.com", settings=settings
        )
        await db.commit()

        assert one.org_id != two.org_id

    async def test_closed_refuses_and_writes_nothing(self, db):
        before = await db.scalar(select(func.count()).select_from(Org))

        with pytest.raises(SignupRefused):
            await provision(
                db, user_id="sub-nobody", email="stranger@example.com", settings=settings_with()
            )

        assert await db.scalar(select(func.count()).select_from(Org)) == before

    async def test_the_workspace_cap_refuses_rather_than_degrading(self, db):
        """One small instance and one shared free LLM quota. Refusing the next person with a
        sentence beats everybody's campaigns quietly getting slower."""
        settings = settings_with(SIGNUP_MODE="open", MAX_ORGS=1)
        existing = await db.scalar(select(func.count()).select_from(Org))
        if not existing:
            await provision(
                db, user_id=f"sub-{uuid.uuid4()}", email="first@example.com", settings=settings
            )
            await db.commit()

        with pytest.raises(SignupRefused, match="workspace limit"):
            await provision(
                db, user_id=f"sub-{uuid.uuid4()}", email="second@example.com", settings=settings
            )

    async def test_signing_in_twice_does_not_make_two_workspaces(self, db):
        """Two tabs, two first requests, one person."""
        settings = settings_with(SIGNUP_MODE="open")
        user_id = f"sub-{uuid.uuid4()}"

        first = await provision(db, user_id=user_id, email="c@example.com", settings=settings)
        await db.commit()

        found = await db.scalar(select(Member).where(Member.user_id == user_id))
        assert found is not None
        assert found.org_id == first.org_id
        assert (
            await db.scalar(
                select(func.count()).select_from(Member).where(Member.user_id == user_id)
            )
            == 1
        )


class TestTwoWorkspacesCannotSeeEachOther:
    async def test_a_campaign_belongs_to_exactly_one_org(self, db):
        settings = settings_with(SIGNUP_MODE="open")
        asha = await provision(
            db, user_id=f"sub-{uuid.uuid4()}", email="asha@example.com", settings=settings
        )
        ben = await provision(
            db, user_id=f"sub-{uuid.uuid4()}", email="ben@example.com", settings=settings
        )
        await db.flush()

        db.add(Campaign(org_id=asha.org_id, name="Asha's campaign", persona={}))
        await db.commit()

        theirs = await db.scalar(
            select(func.count()).select_from(Campaign).where(Campaign.org_id == ben.org_id)
        )
        assert theirs == 0

    async def test_a_lead_in_one_workspace_is_invisible_to_the_other(self, db, org, lead_factory):
        other = Org(name="Somebody else")
        db.add(other)
        await db.flush()
        await lead_factory(state="NEW")
        await db.commit()

        assert (
            await db.scalar(select(func.count()).select_from(Lead).where(Lead.org_id == other.id))
            == 0
        )


class TestCredentialsPerMailbox:
    async def test_the_owners_own_mailbox_with_no_credential_uses_the_environment(self, db, org):
        """What keeps the live deployment sending while all of this lands.

        Both conditions hold here and nowhere else: the org is the one the migration granted the
        environment account to, and the address is the one `SMTP_EMAIL` authenticates as.
        """
        org.uses_environment_mailbox = True
        mailbox = Mailbox(
            org_id=org.id,
            from_name="Asha",
            from_email="shared@outreach.example",
            daily_cap=100,
        )
        db.add(mailbox)
        await db.commit()

        login = await login_for(db, mailbox=mailbox, settings=settings_with())

        assert login.username == "shared@outreach.example"
        assert login.password == "from-the-environment"
        assert not login.uses_oauth

    async def test_an_app_password_credential_is_used_instead(self, db, org):
        settings = settings_with()
        mailbox = Mailbox(
            org_id=org.id, from_name="Ben", from_email="ben@outreach.example", daily_cap=100
        )
        db.add(mailbox)
        await db.flush()
        db.add(
            MailCredential(
                org_id=org.id,
                mailbox_id=mailbox.id,
                kind="app_password",
                account_email="ben@outreach.example",
                secret=seal("their-own-password", settings).token,
            )
        )
        await db.commit()

        login = await login_for(db, mailbox=mailbox, settings=settings)

        assert login.username == "ben@outreach.example"
        assert login.password == "their-own-password"

    async def test_a_lapsed_connection_is_reported_not_retried(self, db, org):
        """A Testing-status OAuth client expires refresh tokens every 7 days, so this is a
        weekly event by design. Retrying a credential that will never work again is how a
        provider starts treating us as an attack."""
        from app.services.mail.connection import MailboxUnavailable

        settings = settings_with()
        mailbox = Mailbox(
            org_id=org.id, from_name="Cat", from_email="cat@outreach.example", daily_cap=100
        )
        db.add(mailbox)
        await db.flush()
        db.add(
            MailCredential(
                org_id=org.id,
                mailbox_id=mailbox.id,
                kind="google_oauth",
                account_email="cat@outreach.example",
                secret=seal("a-refresh-token", settings).token,
                invalid_reason="Google would not renew this connection. Reconnect the mailbox.",
            )
        )
        await db.commit()

        with pytest.raises(MailboxUnavailable, match="Reconnect"):
            await login_for(db, mailbox=mailbox, settings=settings)

    async def test_the_status_endpoint_never_carries_the_secret(self, db, org):
        """Everything a person needs and nothing an attacker does."""
        settings = settings_with()
        mailbox = Mailbox(
            org_id=org.id, from_name="Dee", from_email="dee@outreach.example", daily_cap=100
        )
        db.add(mailbox)
        await db.flush()
        db.add(
            MailCredential(
                org_id=org.id,
                mailbox_id=mailbox.id,
                kind="google_oauth",
                account_email="dee@outreach.example",
                secret=seal("a-refresh-token", settings).token,
            )
        )
        await db.commit()

        status = await connection_status(db, mailbox_id=mailbox.id)

        assert status["connected"] is True
        assert status["account_email"] == "dee@outreach.example"
        rendered = repr(status)
        assert "a-refresh-token" not in rendered
        assert "secret" not in status

    async def test_a_login_never_prints_its_own_secret(self, db, org):
        org.uses_environment_mailbox = True
        mailbox = Mailbox(
            org_id=org.id, from_name="Eve", from_email="shared@outreach.example", daily_cap=100
        )
        db.add(mailbox)
        await db.commit()

        login = await login_for(db, mailbox=mailbox, settings=settings_with())

        assert "from-the-environment" not in repr(login)


class TestTheEnvironmentAccountIsNotEverybodysAccount:
    """The fallback to `SMTP_EMAIL` had no owner, and public sign-up turned that into a hole.

    It was written so the live deployment kept sending while per-mailbox credentials landed, and
    for a single-tenant install it is exactly right. Once strangers can sign up it means a
    mailbox nobody has connected authenticates as **the deployment's own Gmail**: a stranger's
    campaign sends through the operator's account, spends the operator's daily cap, and stakes
    the operator's sending reputation on mail the operator never saw.

    Every test here would have passed before this change by sending the stranger's mail.
    """

    async def test_an_unconnected_mailbox_may_not_borrow_the_environment_account(self, db, org):
        stranger = Org(name="Somebody who signed up")
        db.add(stranger)
        await db.flush()
        mailbox = Mailbox(
            org_id=stranger.id,
            from_name="Stranger",
            from_email="them@elsewhere.example",
            daily_cap=100,
        )
        db.add(mailbox)
        await db.commit()

        with pytest.raises(MailboxUnavailable) as caught:
            await login_for(db, mailbox=mailbox, settings=settings_with())

        # Refusing is only half of it. The message is what the dashboard shows, so it has to say
        # what to do rather than only that something is wrong.
        assert "connect" in str(caught.value).lower()
        assert "them@elsewhere.example" in str(caught.value)

    async def test_naming_a_mailbox_after_the_operators_address_does_not_grant_it(self, db, org):
        """The check that an address-only rule would fail.

        `uq_mailboxes_org_from_email` is scoped per org, so a second workspace may create a
        mailbox whose `from_email` is the operator's own address. Matching on the address alone
        would hand that stranger the operator's password -- turning the guard into the attack.
        """
        impostor_org = Org(name="Impostor")
        db.add(impostor_org)
        await db.flush()
        impostor = Mailbox(
            org_id=impostor_org.id,
            from_name="Impostor",
            from_email="shared@outreach.example",
            daily_cap=100,
        )
        db.add(impostor)
        await db.commit()

        with pytest.raises(MailboxUnavailable):
            await login_for(db, mailbox=impostor, settings=settings_with())

    async def test_the_owner_may_not_send_a_different_address_through_it_either(self, db, org):
        """One org, several mailboxes, and only one of them is the account that logs in.

        Sending `rahul@arkvane.in` through a Gmail authenticated as somebody else is the
        mismatched-envelope problem already on record: nothing bounces, and the message is simply
        unauthenticated against a domain whose SPF and DKIM say otherwise.
        """
        org.uses_environment_mailbox = True
        second = Mailbox(
            org_id=org.id, from_name="Another", from_email="other@outreach.example", daily_cap=100
        )
        db.add(second)
        await db.commit()

        with pytest.raises(MailboxUnavailable):
            await login_for(db, mailbox=second, settings=settings_with())

    async def test_a_connected_stranger_sends_as_themselves(self, db, org):
        """The point of the whole feature: connect your own Gmail, send as yourself."""
        settings = settings_with()
        stranger = Org(name="Somebody who signed up")
        db.add(stranger)
        await db.flush()
        mailbox = Mailbox(
            org_id=stranger.id, from_name="Dev", from_email="dev@theirco.example", daily_cap=100
        )
        db.add(mailbox)
        await db.flush()
        db.add(
            MailCredential(
                org_id=stranger.id,
                mailbox_id=mailbox.id,
                kind="app_password",
                account_email="dev@theirco.example",
                secret=seal("their-own-password", settings).token,
            )
        )
        await db.commit()

        login = await login_for(db, mailbox=mailbox, settings=settings)

        assert login.username == "dev@theirco.example"
        assert login.password == "their-own-password"
