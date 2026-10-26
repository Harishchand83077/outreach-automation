"""Letting somebody sign up and get their own workspace.

Until now access was a row inserted by hand with SQL: `docs/DEPLOY.md` §4.1 told the owner to
find their Supabase `sub` and write it into `members`. That is correct for one person and
impossible for ten.

**A sign-up creates a new org, with that person as its owner.**

The alternative -- everybody joining one shared org as `member` -- was rejected in
`MULTI_TENANT_PLAN.md` §2. Separate orgs give complete separation through the mechanism that is
already load-bearing and already tested: `org_id` is on every table, every repository call takes
an `OrgContext`, and `app/api/deps.py` never takes an org from the request. Roles inside an org
answer a different question (who on *your own team* may change *your own* sending rules), and
reusing them for tenancy would have been a second, weaker answer to a question already settled.

**The default is `closed`**, which is exactly today's behaviour: a valid token with no membership
gets 403. That matters more than it looks. A migration that quietly opened public sign-up on a
system that sends email would be the worst default this project could ship, and the same
reasoning put `orgs.send_mode` in as a nullable column rather than one with a default.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings, config
from app.db.models import Invitation, Member, Org

logger = logging.getLogger(__name__)


class SignupRefused(Exception):
    """This person may not have an account here. The message is shown to them."""


def _org_name_for(email: str | None) -> str:
    """A workspace name somebody recognises on first sight.

    The local part of their address, which is almost always their name or their company. They
    can rename it; what matters is that the first screen does not say "Org 7f3a".
    """
    if not email or "@" not in email:
        return "My workspace"
    local = email.split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
    return local.title() or "My workspace"


def may_sign_up(email: str | None, settings: Settings | None = None) -> None:
    """Raise `SignupRefused` unless this address may create a workspace."""
    settings = settings or config
    mode = settings.SIGNUP_MODE

    if mode == "closed":
        raise SignupRefused(
            "This account is not a member of any organisation, and sign-up is closed. "
            "Ask the owner for an invitation."
        )

    if mode == "allowlist":
        domains = settings.signup_allowed_domains
        if not domains:
            # Fails closed. An allowlist nobody filled in is a configuration mistake, and the
            # safe reading of "allow these domains: none" is "allow nobody", not "allow all".
            raise SignupRefused("Sign-up is restricted and no domains are allowed yet.")
        domain = (email or "").rpartition("@")[2].lower()
        if domain not in domains:
            raise SignupRefused("Sign-up is restricted to approved email domains.")

    return None


async def provision(
    session: AsyncSession,
    *,
    user_id: str,
    email: str | None,
    settings: Settings | None = None,
) -> Member:
    """Create an org for this person and make them its owner.

    Caller commits. Raises `SignupRefused` if the settings do not permit it.

    **A global cap, not just a per-request check.** `MAX_ORGS` exists because this runs on one
    small instance with one shared LLM quota, and the failure mode of an open sign-up is not a
    security breach but a hundred strangers quietly exhausting a free tier until the people who
    actually use the tool get nothing. Refusing the hundred-and-first with a sentence is better
    than degrading for everybody.
    """
    settings = settings or config

    # **The invitation is checked first, and it bypasses `SIGNUP_MODE`.** Somebody with the
    # `admin` role in that org named this address deliberately, which is a stronger statement
    # than any global policy -- and a deployment that stays `closed` so strangers cannot wander
    # in still has to be able to add its own team.
    invitation = await _pending_invitation(session, email)
    if invitation is not None:
        return await _accept(session, invitation, user_id=user_id, email=email)

    may_sign_up(email, settings)

    orgs = await session.scalar(select(func.count()).select_from(Org))
    if orgs is not None and orgs >= settings.MAX_ORGS:
        raise SignupRefused(
            "This deployment has reached its workspace limit. Ask the owner to raise it."
        )

    org = Org(name=_org_name_for(email))
    session.add(org)
    await session.flush()

    member = Member(org_id=org.id, user_id=user_id, email=email, role="owner")
    session.add(member)
    try:
        await session.flush()
    except IntegrityError:
        # Two tabs, two first requests, one person. The second loses the race and finds the row
        # the first wrote -- which is the right answer, not an error worth showing anybody.
        await session.rollback()
        existing = await session.scalar(select(Member).where(Member.user_id == user_id))
        if existing is None:
            raise
        return existing

    # No email address, no personal data: `user_id` is Supabase's opaque `sub`. This line exists
    # so that somebody reading the log can see a workspace was created and when.
    logger.info("workspace provisioned", extra={"org_id": str(org.id)})
    return member


async def _pending_invitation(session: AsyncSession, email: str | None) -> Invitation | None:
    """An unused, unrevoked invitation for this address, or None.

    Lower-cased on both sides. An invitation to `Asha@Example.com` that fails to match a sign-in
    as `asha@example.com` is an invitation that silently does nothing, and the person it was for
    ends up in a workspace of their own wondering where everything went.
    """
    if not email:
        return None
    return await session.scalar(
        select(Invitation).where(
            func.lower(Invitation.email) == email.strip().lower(),
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )


async def _accept(
    session: AsyncSession, invitation: Invitation, *, user_id: str, email: str | None
) -> Member:
    """Join the inviting org instead of creating a new one.

    The invitation is marked accepted in the **same transaction** as the membership it creates,
    for the reason every other write in this project pairs that way: a row saying somebody was
    invited, with no membership, and a membership nobody can account for are both worse than
    either half failing.
    """
    member = Member(org_id=invitation.org_id, user_id=user_id, email=email, role=invitation.role)
    session.add(member)
    invitation.accepted_at = datetime.now(tz=UTC)
    try:
        await session.flush()
    except IntegrityError:
        # Two tabs, one person, as below. Whoever lost the race finds the row the winner wrote.
        await session.rollback()
        existing = await session.scalar(select(Member).where(Member.user_id == user_id))
        if existing is None:
            raise
        return existing

    # No address: `user_id` is opaque and `org_id` is ours. This line exists so somebody reading
    # the log can see that a seat was taken and which workspace gained a person.
    logger.info(
        "invitation accepted",
        extra={"org_id": str(invitation.org_id), "role": invitation.role},
    )
    return member
