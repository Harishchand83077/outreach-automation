"""Who else is in this workspace, and inviting somebody who is not here yet.

**Written because the order mattered and should not have.** Adding a teammate was an `INSERT`
against production, and it had to happen *before* that person's first sign-in: afterwards
`provision()` has already given them a workspace of their own, and adding the row then leaves
them holding **two** memberships, which `deps.current_user` resolves by taking whichever the
database returns first. `uq_members_org_user` is scoped per org, so nothing refuses the second
row -- the failure is silent and it is a tenancy failure.

An invitation says the same thing ahead of time, so nothing has to happen in a particular order.

**Everything here is `admin`**, and listing is too. The member list is who may read your leads;
that is not a secret from the team, but it is not a thing a reviewer needs either, and the rule
across this API is that the gate is on the rules rather than on the work.

`org_id` is never taken from the caller. It comes from the membership, as everywhere (ADR-004).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import ROLE_ORDER, CurrentUserDep, SessionDep, require_role
from app.core.settings import config
from app.db.models import Invitation, Mailbox, MailCredential, Member
from app.db.state import record_event
from app.services import api_tokens
from app.services.mail.expiry import grant_status

router = APIRouter(tags=["team"], dependencies=[Depends(require_role("admin"))])


@router.get("/team")
async def list_team(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The people in this workspace, and the seats being held for people who are not."""
    members = (
        await session.scalars(
            select(Member).where(Member.org_id == user.org_id).order_by(Member.created_at)
        )
    ).all()
    pending = (
        await session.scalars(
            select(Invitation)
            .where(
                Invitation.org_id == user.org_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .order_by(Invitation.created_at)
        )
    ).all()
    return {
        "members": [
            {
                "id": str(member.id),
                "email": member.email,
                "role": member.role,
                "joined_at": member.created_at.isoformat(),
                "is_you": member.user_id == user.user_id,
            }
            for member in members
        ],
        "invitations": [
            {
                "id": str(row.id),
                "email": row.email,
                "role": row.role,
                "invited_by": row.invited_by,
                "created_at": row.created_at.isoformat(),
            }
            for row in pending
        ],
    }


@router.get("/admin/overview")
async def admin_overview(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """Everything an admin has to keep track of, in one place.

    **Written because three of the gates live in three different systems** and nothing showed
    them together, so "why can this person not send?" meant checking a Supabase dashboard, a
    Google Cloud console and this database, and holding the answer in your head.

    1. **Supabase** decides whether somebody may sign in at all. We cannot see it from here --
       the identity provider owns that table and we deliberately never join across the boundary
       (`deps.py`) -- so what this reports is the shadow it casts: a member row exists only
       *after* that person has authenticated at least once. `signed_in` is that, stated as what
       it is rather than as a guess about Supabase.
    2. **This database** decides which workspace they are in and what they may do.
    3. **Google** decides whether they may connect a mailbox. While the OAuth client is
       unpublished (ADR-018) only addresses on its *test users* list can, capped at 100 --
       and that list is managed in the Cloud console with no API we can read. So it is reported
       as the unknown it is, with the link, rather than as a tick somebody would trust.

    Nothing here is a credential, and no `invalid_reason` is echoed: that text can quote a
    provider's reply and this reaches a browser and screenshots.
    """
    members = (
        await session.scalars(
            select(Member).where(Member.org_id == user.org_id).order_by(Member.created_at)
        )
    ).all()
    pending = (
        await session.scalars(
            select(Invitation)
            .where(
                Invitation.org_id == user.org_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
            )
            .order_by(Invitation.created_at)
        )
    ).all()

    mailboxes = (
        await session.scalars(
            select(Mailbox).where(Mailbox.org_id == user.org_id).order_by(Mailbox.created_at)
        )
    ).all()
    credentials = {
        row.mailbox_id: row
        for row in (
            await session.scalars(
                select(MailCredential).where(
                    MailCredential.mailbox_id.in_([m.id for m in mailboxes])
                )
            )
        ).all()
        if mailboxes
    }

    connected = 0
    needs_reconnect = 0
    mailbox_rows: list[dict[str, Any]] = []
    for mailbox in mailboxes:
        credential = credentials.get(mailbox.id)
        grant = grant_status(credential)
        is_connected = credential is not None and credential.invalid_reason is None
        connected += 1 if is_connected else 0
        needs_reconnect += 1 if credential is not None and not is_connected else 0
        mailbox_rows.append(
            {
                "id": str(mailbox.id),
                "from_email": mailbox.from_email,
                "connected": is_connected,
                "needs_reconnect": credential is not None and not is_connected,
                # Who pressed Connect, so an admin can tell whose account this actually is.
                "connected_by": credential.connected_by if credential else None,
                "days_left": grant.days_left,
                "paused": mailbox.paused,
            }
        )

    return {
        "people": [
            {
                "id": str(member.id),
                "email": member.email,
                "role": member.role,
                # A member row is written on the first authenticated request, so its existence
                # *is* the evidence that Supabase let them in. Nothing else here can see that.
                "signed_in": True,
                "joined_at": member.created_at.isoformat(),
                "is_you": member.user_id == user.user_id,
            }
            for member in members
        ],
        "invited": [
            {
                "id": str(row.id),
                "email": row.email,
                "role": row.role,
                "invited_by": row.invited_by,
                "signed_in": False,
                "created_at": row.created_at.isoformat(),
            }
            for row in pending
        ],
        "mailboxes": mailbox_rows,
        "totals": {
            "members": len(members),
            "invited": len(pending),
            "mailboxes": len(mailboxes),
            "connected": connected,
            "needs_reconnect": needs_reconnect,
        },
        "workspace": {
            # Reported, never editable here. The send mode has exactly one write path and it is
            # owner-gated with a typed confirmation (ADR-010).
            "signup_mode": config.SIGNUP_MODE,
            "grant_days": config.GOOGLE_GRANT_DAYS,
            "google_oauth_configured": config.google_oauth_configured,
            "max_orgs": config.MAX_ORGS,
        },
        # The step this system cannot take for you, named with the place to take it.
        "google_test_users": {
            "required_while_unpublished": config.GOOGLE_GRANT_DAYS > 0,
            "console_url": "https://console.cloud.google.com/auth/audience",
            "note": (
                "While the OAuth client is unpublished, only addresses on its test-user list "
                "can connect a mailbox (100 maximum). That list lives in the Google Cloud "
                "console and cannot be read or changed from here, so add each person there "
                "before they press Connect Gmail."
            ),
        },
    }


@router.post("/team/invitations", status_code=status.HTTP_201_CREATED)
async def invite(
    user: CurrentUserDep,
    session: SessionDep,
    email: Annotated[str, Body(embed=True)],
    role: Annotated[str, Body(embed=True)] = "member",
) -> dict[str, Any]:
    """Hold a seat in this workspace for an address that has not signed in yet.

    **This is not a token and it is not secret.** A pending invitation grants nothing on its own:
    it is consulted only when somebody authenticates through Supabase *as that address*, so the
    security boundary is still the identity provider. What it changes is which org that person
    lands in. Matching on the address is unavoidable -- a Supabase `sub` does not exist until the
    first sign-in -- and it is the same assumption every invitation email makes.
    """
    address = (email or "").strip().lower()
    if "@" not in address or len(address) > 320:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{email!r} is not an email address.",
        )
    if role not in ROLE_ORDER:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown role {role!r}. Use owner, admin or member.",
        )
    if not user.may(role):
        # You cannot hand out more than you hold. Without this an `admin` invites an `owner`,
        # who can then change the send mode -- a privilege escalation through the one door that
        # was built to let strangers in.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot invite somebody as {role}: that is above your own role.",
        )

    existing_member = await session.scalar(
        select(Member).where(func.lower(Member.email) == address)
    )
    if existing_member is not None:
        # **Refused rather than quietly creating a second membership.** This is the exact failure
        # the table exists to prevent, and an invitation accepted by somebody who already has a
        # workspace would recreate it at the other end.
        if existing_member.org_id == user.org_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{address} is already in this workspace.",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{address} already has a workspace of their own, so they cannot be invited "
                "into this one. A person belongs to one workspace here."
            ),
        )

    pending = await session.scalar(
        select(Invitation).where(
            func.lower(Invitation.email) == address,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    if pending is not None:
        if pending.org_id == user.org_id:
            # Idempotent within the org: re-inviting is what somebody does when they are not
            # sure the first one worked, and a 409 would only make them ask for the id.
            return _as_dict(pending, already=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{address} has already been invited to another workspace.",
        )

    invitation = Invitation(
        org_id=user.org_id, email=address, role=role, invited_by=user.email or user.user_id
    )
    session.add(invitation)
    await session.flush()
    await record_event(
        session,
        org_id=user.org_id,
        type="team.invited",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"email": address, "role": role},
    )
    await session.commit()
    return _as_dict(invitation, already=False)


@router.delete("/team/invitations/{invitation_id}")
async def revoke(
    invitation_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Withdraw a seat that has not been taken.

    Revoked rather than deleted, so "who was invited and then was not" stays answerable, and so
    the partial unique index stops blocking a fresh invitation to the same address.
    """
    invitation = await session.get(Invitation, invitation_id)
    if invitation is None or invitation.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    if invitation.accepted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That invitation has already been accepted. Remove the person from the team "
                "instead."
            ),
        )
    invitation.revoked_at = datetime.now(tz=UTC)
    await record_event(
        session,
        org_id=user.org_id,
        type="team.invitation_revoked",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"email": invitation.email},
    )
    await session.commit()
    return {"revoked": True}


@router.delete("/team/members/{member_id}")
async def remove_member(
    member_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Take somebody's access away.

    Two refusals, and both are about leaving a workspace reachable. You cannot remove yourself --
    an admin who does is locked out of the org they were administering, with no route back that
    does not involve SQL. And the last `owner` cannot be removed, because an org with no owner
    has nobody who may change its send mode (ADR-010), which is the one control that must always
    have somebody behind it.

    **Their data stays.** A member is a key, not a container: the campaigns, leads and drafts
    belong to the org, and deleting them because somebody left would destroy the record of what
    was sent to whom -- which `docs/PRIVACY.md` is explicit must remain attributable.
    """
    member = await session.get(Member, member_id)
    if member is None or member.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    if member.user_id == user.user_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot remove your own access. Ask another admin to do it.",
        )
    if member.role == "owner":
        owners = await session.scalar(
            select(func.count())
            .select_from(Member)
            .where(Member.org_id == user.org_id, Member.role == "owner")
        )
        if (owners or 0) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This is the only owner. Make somebody else an owner first.",
            )

    await session.delete(member)
    await record_event(
        session,
        org_id=user.org_id,
        type="team.member_removed",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"email": member.email, "role": member.role},
    )
    await session.commit()
    return {"removed": True}


def _as_dict(invitation: Invitation, *, already: bool) -> dict[str, Any]:
    return {
        "id": str(invitation.id),
        "email": invitation.email,
        "role": invitation.role,
        "created_at": invitation.created_at.isoformat(),
        "already_invited": already,
    }


# --------------------------------------------------------------- MCP tokens (ADR-028)
#
# `admin`, like everything else in this router. A token reads the workspace's leads and replies,
# which a `member` can already do in the dashboard -- but a credential that leaves the browser
# and lives in somebody's MCP client for ninety days is a *rule* rather than the work, and the
# rule across this API is that the gate goes on the rules.


@router.get("/mcp-tokens")
async def list_mcp_tokens(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The live tokens for this workspace. Never a secret -- only the hash is stored."""
    rows = await api_tokens.list_tokens(session, org_id=user.org_id)
    return {
        "tokens": [
            {
                "id": str(row.id),
                "name": row.name,
                # Enough to recognise the row when revoking it, useless as a credential.
                "prefix": f"{row.token_prefix}\u2026",
                "scopes": row.scopes,
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                "created_by_you": row.user_id == user.user_id,
            }
            for row in rows
        ],
        "endpoint": "/mcp",
        "default_ttl_days": api_tokens.DEFAULT_TTL_DAYS,
        # Named rather than hard-coded in the dashboard, so the screen cannot label a row with a
        # scope string the server does not actually issue.
        "propose_scope": api_tokens.PROPOSE_SCOPE,
    }


@router.post("/mcp-tokens", status_code=status.HTTP_201_CREATED)
async def create_mcp_token(
    user: CurrentUserDep,
    session: SessionDep,
    name: Annotated[str, Body(embed=True)] = "My Claude",
    ttl_days: Annotated[int | None, Body(embed=True)] = api_tokens.DEFAULT_TTL_DAYS,
    may_propose: Annotated[bool, Body(embed=True)] = False,
) -> dict[str, Any]:
    """Mint a token and return it **once**.

    The secret is not stored and there is no route that reads one back: a credential a route can
    return is a credential a stolen session can return. If it is lost, revoke it and mint another
    -- which is cheap, and is the behaviour that keeps the stored form useless.

    **`may_propose` defaults to false and has to be asked for** (ADR-030). It lets the holder
    write a draft into this workspace's review queue -- not send one, and not approve one, since
    no tool can do either -- and most tokens do not need it. Default-off is the same decision as
    `SEND_MODE=disabled` and `SIGNUP_MODE=closed`: the narrow setting is the one you get without
    choosing, and widening is deliberate.
    """
    if ttl_days is not None and (ttl_days < 1 or ttl_days > 365):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ttl_days must be between 1 and 365, or null for no expiry.",
        )

    scopes = [api_tokens.READ_SCOPE]
    if may_propose:
        # Additive rather than instead of: an agent that could write but not read would be able
        # to propose an email about a lead it could not look up, which is not a useful tool.
        scopes.append(api_tokens.PROPOSE_SCOPE)

    minted = await api_tokens.create_token(
        session,
        org_id=user.org_id,
        user_id=user.user_id,
        name=name,
        ttl_days=ttl_days,
        scopes=scopes,
    )
    await record_event(
        session,
        org_id=user.org_id,
        type="mcp.token_created",
        actor="user",
        actor_id=user.email or user.user_id,
        # The name and the prefix, never the secret.
        # The name, the prefix and what it may do -- never the secret. The scopes belong in the
        # trail because "who gave a credential the ability to write?" has to be answerable later.
        data={
            "token_id": str(minted.id),
            "name": minted.name,
            "prefix": minted.prefix,
            "scopes": list(minted.scopes),
        },
    )
    await session.commit()

    return {
        "id": str(minted.id),
        "name": minted.name,
        # Shown once. The dashboard says so beside it.
        "token": minted.secret,
        "expires_at": minted.expires_at.isoformat() if minted.expires_at else None,
        "scopes": list(minted.scopes),
    }


@router.delete("/mcp-tokens/{token_id}")
async def revoke_mcp_token(
    token_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Revoke a token. Takes effect on the next call, because verification reads the row."""
    revoked = await api_tokens.revoke_token(session, org_id=user.org_id, token_id=token_id)
    if not revoked:
        # A token id from another workspace resolves to nothing rather than to a 403 that would
        # confirm it exists.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such token in this workspace."
        )
    await record_event(
        session,
        org_id=user.org_id,
        type="mcp.token_revoked",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"token_id": str(token_id)},
    )
    await session.commit()
    return {"revoked": True}
