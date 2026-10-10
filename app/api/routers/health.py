"""Liveness and readiness.

`ARCHITECTURE_TARGET.md` §14. The distinction matters to whatever restarts this process:

- `/health` says the process is up. It touches nothing, so it cannot fail for someone else's
  reason — a health check that queries the database restarts the API when the *database* is ill,
  which makes an outage worse.
- `/ready` says the system can actually serve: the database answers, and the worker's heartbeat
  is fresh. A ready API with a dead worker looks fine and sends nothing.

Both are public. A load balancer cannot hold a token.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: Annotated[AsyncSession, Depends(get_session)]) -> JSONResponse:
    checks: dict[str, object] = {}
    ok = True

    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"unavailable: {type(exc).__name__}"
        ok = False

    code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content={"ready": ok, "checks": checks})
