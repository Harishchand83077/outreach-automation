"""Who the caller is, according to us rather than according to them.

The first authenticated endpoint, and the one the dashboard calls on load to find out whether a
session is still good. It returns the org and role from the `members` row — never anything the
caller supplied.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUserDep

router = APIRouter(tags=["me"])


@router.get("/me")
async def me(user: CurrentUserDep) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "org_id": str(user.org_id),
        "role": user.role,
    }
