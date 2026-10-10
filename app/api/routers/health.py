"""Liveness and readiness.

`ARCHITECTURE_TARGET.md` §14, §18. The distinction matters to whatever restarts this process:

- `/health` says the process is up. It touches nothing, so it cannot fail for someone else's
  reason — a health check that queries the database restarts the API when the *database* is ill,
  which makes an outage worse.
- `/ready` says the system can actually serve: the database answers, and the worker's heartbeat
  is fresh. **A ready api with a dead worker looks fine and sends nothing**, which is exactly the
  failure an external uptime monitor pinging this endpoint is meant to catch (§18).

Both are public. A load balancer cannot hold a token, and neither can a free uptime monitor.
Neither endpoint reveals anything an unauthenticated caller could act on: `/ready` answers with
booleans and an age in seconds, never the error text, the database name or the task detail.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.db import ops

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
        # The exception type, not its message: a driver error quotes the connection string.
        ok = False

    if ok:
        # Only asked once the database answered, because the heartbeat lives in it and a failed
        # query here would report a dead worker when the real fault is the database.
        try:
            worker = await ops.latest_heartbeat(session)
        except Exception as exc:
            checks["worker"] = f"unknown: {type(exc).__name__}"
            ok = False
        else:
            if worker is None:
                # Never beaten: either the worker has not been started or it has never managed
                # to write. Not ready either way — nothing will be researched, drafted or sent.
                checks["worker"] = "no heartbeat"
                ok = False
            else:
                checks["worker"] = "stale" if worker.stale else "ok"
                checks["worker_seen_seconds_ago"] = worker.age_seconds
                if worker.stale:
                    ok = False

    code = status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(status_code=code, content={"ready": ok, "checks": checks})
