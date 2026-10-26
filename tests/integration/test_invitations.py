"""Adding a teammate without the order mattering.

**The failure this replaces was silent, and it was a tenancy failure.** Adding somebody meant
inserting a `members` row by hand *before* their first sign-in. Afterwards, `provision()` has
already given them a workspace of their own -- and inserting the row then leaves them holding two
memberships, which `deps.current_user` resolves with `scalar()`, taking whichever the database
returns first. `uq_members_org_user` is scoped per org, so nothing refuses the second row.

So the tests that matter here are about **order**: invited then signed in, signed in then
invited, and the two ways a person could end up in two places at once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.settings import Settings
from app.db.models import Invitation, Member, Org
from app.services.signup import SignupRefused, provision

pytestmark = pytest.mark.asyncio(loop_scope="session")


def settings_with(**kw: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "GROQ_API_KEY": "gsk_test"}
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


def address() -> str:
    return f"asha-{uuid.uuid4().hex[:10]}@example.com"


async def _invite(db, org, email, role="member") -> Invitation:
    row = Invitation(org_id=org.id, email=email.lower(), role=role, invited_by="owner@example.com")
    db.add(row)
    await db.commit()
    return row


class TestAnInvitationDecidesWhichWorkspace:
    async def test_an_invited_person_joins_that_org_rather_than_a_new_one(self, db, org):
        """The whole point. Before this they were given a workspace of their own."""
        email = address()
        await _invite(db, org, email, role="member")

        member = await provision(
            db, user_id=f"sub-{uuid.uuid4()}", email=email, settings=settings_with()
        )
        await db.commit()

        assert member.org_id == org.id
        assert member.role == "member"

    async def test_the_invitation_is_consumed(self, db, org):
        email = address()
        invitation = await _invite(db, org, email)

        await provision(db, user_id=f"sub-{uuid.uuid4()}", email=email, settings=settings_with())
        await db.commit()
        await db.refresh(invitation)

        assert invitation.accepted_at is not None

    async def test_one_membership_and_exactly_one(self, db, org):
        """The silent failure, asserted directly: two rows is a tenancy bug, not a duplicate."""
        email = address()
        user_id = f"sub-{uuid.uuid4()}"
        await _invite(db, org, email)

        await provision(db, user_id=user_id, email=email, settings=settings_with())
        await db.commit()

        count = await db.scalar(
            select(func.count()).select_from(Member).where(Member.user_id == user_id)
        )
        assert count == 1

    async def test_an_invitation_beats_a_closed_signup_mode(self, db, org):
        """An admin naming this address is a stronger statement than a global policy.

        Without this, a deployment kept `closed` so strangers cannot wander in would also be
        unable to add its own team -- which is the configuration this project ships by default.
        """
        email = address()
        await _invite(db, org, email)

        member = await provision(
            db,
            user_id=f"sub-{uuid.uuid4()}",
            email=email,
            settings=settings_with(SIGNUP_MODE="closed"),
        )
        await db.commit()

        assert member.org_id == org.id

    async def test_a_stranger_is_still_refused_when_signup_is_closed(self, db, org):
        """The invitation path must not become a hole in the default."""
        with pytest.raises(SignupRefused):
            await provision(
                db,
                user_id=f"sub-{uuid.uuid4()}",
                email=address(),
                settings=settings_with(SIGNUP_MODE="closed"),
            )

    async def test_case_does_not_matter(self, db, org):
        """An invitation that fails to match the sign-in does nothing, and says nothing."""
        email = address()
        await _invite(db, org, email.upper())

        member = await provision(
            db, user_id=f"sub-{uuid.uuid4()}", email=email.lower(), settings=settings_with()
        )
        await db.commit()

        assert member.org_id == org.id

    async def test_a_used_invitation_is_not_reused(self, db, org):
        """Otherwise one invitation is a permanent back door into a workspace."""
        email = address()
        invitation = await _invite(db, org, email)
        invitation.accepted_at = datetime.now(tz=UTC)
        await db.commit()

        member = await provision(
            db,
            user_id=f"sub-{uuid.uuid4()}",
            email=email,
            settings=settings_with(SIGNUP_MODE="open"),
        )
        await db.commit()

        assert member.org_id != org.id

    async def test_a_revoked_invitation_is_not_honoured(self, db, org):
        email = address()
        invitation = await _invite(db, org, email)
        invitation.revoked_at = datetime.now(tz=UTC)
        await db.commit()

        with pytest.raises(SignupRefused):
            await provision(
                db,
                user_id=f"sub-{uuid.uuid4()}",
                email=email,
                settings=settings_with(SIGNUP_MODE="closed"),
            )


class TestInviting:
    """The route, including the two refusals that stop a person being in two places."""

    async def test_somebody_who_already_has_a_workspace_cannot_be_invited(self, db, org):
        """This is the failure the table exists to prevent, caught at the other end too."""
        from app.api.routers.team import invite

        email = address()
        stranger = Org(name="Their Own")
        db.add(stranger)
        await db.commit()
        db.add(Member(org_id=stranger.id, user_id=f"sub-{uuid.uuid4()}", email=email, role="owner"))
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await invite(_user(org), db, email=email, role="member")

        assert caught.value.status_code == 409
        assert "workspace of their own" in caught.value.detail

    async def test_an_admin_cannot_invite_an_owner(self, db, org):
        """You cannot hand out more than you hold.

        Without it, the door built to let people in is a privilege escalation: an admin invites
        an owner, who may then change the send mode (ADR-010).
        """
        from app.api.routers.team import invite

        with pytest.raises(HTTPException) as caught:
            await invite(_user(org, role="admin"), db, email=address(), role="owner")

        assert caught.value.status_code == 403

    async def test_an_owner_may_invite_an_owner(self, db, org):
        from app.api.routers.team import invite

        body = await invite(_user(org, role="owner"), db, email=address(), role="owner")

        assert body["role"] == "owner"

    async def test_re_inviting_the_same_address_is_idempotent(self, db, org):
        """What somebody does when they are not sure the first one worked."""
        from app.api.routers.team import invite

        email = address()
        first = await invite(_user(org), db, email=email)
        second = await invite(_user(org), db, email=email)

        assert second["id"] == first["id"]
        assert second["already_invited"] is True

    async def test_a_second_workspace_cannot_invite_the_same_address(self, db, org):
        """Two pending invitations is a race whose winner is whoever signs in first."""
        from app.api.routers.team import invite

        email = address()
        await invite(_user(org), db, email=email)

        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await invite(_user(other), db, email=email)

        assert caught.value.status_code == 409

    async def test_a_malformed_address_is_refused(self, db, org):
        from app.api.routers.team import invite

        with pytest.raises(HTTPException) as caught:
            await invite(_user(org), db, email="not-an-address")

        assert caught.value.status_code == 422


class TestRemovingSomebody:
    async def test_the_last_owner_cannot_be_removed(self, db):
        """An org with no owner has nobody who may change its send mode."""
        from app.api.routers.team import remove_member

        lonely = Org(name="One Person")
        db.add(lonely)
        await db.commit()
        owner = Member(
            org_id=lonely.id, user_id=f"sub-{uuid.uuid4()}", email=address(), role="owner"
        )
        db.add(owner)
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await remove_member(owner.id, _user(lonely, user_id="somebody-else"), db)

        assert caught.value.status_code == 409
        assert "only owner" in caught.value.detail

    async def test_you_cannot_remove_yourself(self, db, org):
        """An admin who does is locked out of the org they were administering."""
        from app.api.routers.team import remove_member

        me = Member(org_id=org.id, user_id=f"sub-{uuid.uuid4()}", email=address(), role="admin")
        db.add(me)
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await remove_member(me.id, _user(org, user_id=me.user_id), db)

        assert caught.value.status_code == 409

    async def test_a_member_of_another_org_is_not_found_rather_than_forbidden(self, db, org):
        """ADR-004 asserted rather than assumed: another org's rows do not exist here."""
        from app.api.routers.team import remove_member

        other = Org(name="Not Yours")
        db.add(other)
        await db.commit()
        theirs = Member(
            org_id=other.id, user_id=f"sub-{uuid.uuid4()}", email=address(), role="member"
        )
        db.add(theirs)
        await db.commit()

        with pytest.raises(HTTPException) as caught:
            await remove_member(theirs.id, _user(org), db)

        assert caught.value.status_code == 404


class _User:
    """Enough of `CurrentUser` for these routes. `org_id` never comes from the caller."""

    def __init__(self, org_id, role="admin", user_id="sub-acting", email="admin@example.com"):
        self.org_id = org_id
        self.role = role
        self.user_id = user_id
        self.email = email

    def may(self, role: str) -> bool:
        from app.api.deps import ROLE_ORDER

        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(role, 99)


def _user(org, **kw):
    return _User(org.id, **kw)
