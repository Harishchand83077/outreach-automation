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
from app.db.models import Invitation, Member
from app.db.state import record_event

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
