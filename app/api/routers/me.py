"""Who the caller is, according to us rather than according to them.

The first authenticated endpoint, and the one the dashboard calls on load to find out whether a
session is still good. It returns the org and role from the `members` row — never anything the
caller supplied.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, platform_operator_flag
from app.db.models import Member, Org

router = APIRouter(tags=["me"])


@router.get("/me")
async def me(
    user: CurrentUserDep,
    session: SessionDep,
    is_operator: Annotated[bool, Depends(platform_operator_flag)],
) -> dict[str, object]:
    # Every workspace this person is a member of, in the same order `current_user` resolves
    # them, so the first row is always the active one. **Listing is not switching**: a workspace
    # nobody can name is a workspace nobody can notice they are in, which is how an arbitrary
    # choice stayed invisible. Choosing comes later, with somewhere to keep the choice.
    rows = (
        await session.execute(
            select(Member.org_id, Org.name, Member.role)
            .join(Org, Org.id == Member.org_id)
            .where(Member.user_id == user.user_id)
            .order_by(Member.created_at, Member.id)
        )
    ).all()
    workspaces = [
        {
            "org_id": str(org_id),
            "name": name,
            "role": role,
            "active": org_id == user.org_id,
        }
        for org_id, name, role in rows
    ]
    active = next((w for w in workspaces if w["active"]), None)
    return {
        "user_id": user.user_id,
        "email": user.email,
        "org_id": str(user.org_id),
        "org_name": active["name"] if active else None,
        "role": user.role,
        "workspaces": workspaces,
        # The scope above orgs (ADR-024). Reported here so the dashboard can decide whether
        # to draw the screen at all, rather than calling the route and reading a 403 -- a UI
        # that learns what it may do from failed requests logs an error on every load for
        # every ordinary user.
        "platform_operator": is_operator,
    }
