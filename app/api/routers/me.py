"""Who the caller is, according to us rather than according to them.

The first authenticated endpoint, and the one the dashboard calls on load to find out whether a
session is still good. It returns the org and role from the `members` row — never anything the
caller supplied.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUserDep, platform_operator_flag

router = APIRouter(tags=["me"])


@router.get("/me")
async def me(
    user: CurrentUserDep,
    is_operator: Annotated[bool, Depends(platform_operator_flag)],
) -> dict[str, object]:
    return {
        "user_id": user.user_id,
        "email": user.email,
        "org_id": str(user.org_id),
        "role": user.role,
        # The scope above orgs (ADR-024). Reported here so the dashboard can decide whether
        # to draw the screen at all, rather than calling the route and reading a 403 -- a UI
        # that learns what it may do from failed requests logs an error on every load for
        # every ordinary user.
        "platform_operator": is_operator,
    }
