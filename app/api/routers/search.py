"""Search across one workspace (ADR-086). Read-only, database only, never networked."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import CurrentUserDep, SessionDep
from app.services.search import workspace

router = APIRouter(tags=["search"])


@router.get("/search")
async def search_workspace(
    user: CurrentUserDep, session: SessionDep, q: str = ""
) -> dict[str, Any]:
    """Ranked hits for the signed-in person's workspace. `org_id` comes from the membership."""
    hits = await workspace.search(session, user.ctx, q)
    return {"hits": [hit.as_dict() for hit in hits]}
