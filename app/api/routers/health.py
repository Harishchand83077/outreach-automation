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

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SettingsDep, get_session
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


#: When this process started. Read once at import, which is as close to "started" as a module
#: can get, and it is the number that answers "did the deploy actually restart anything?" --
#: a rollback that silently left the old container running reports an uptime in days.
_STARTED_AT = datetime.now(UTC)


@router.get("/version")
async def version(settings: SettingsDep) -> dict[str, str]:
    """Which build is running. **Public, and that is a deliberate call.**

    Four times in this project a merged pull request was read as a shipped one. Cloudflare
    rebuilds the dashboard on every merge to `main` and the API is deployed by hand, so the two
    drift apart routinely -- and the symptom is a screen asking for a route the running API does
    not serve, which arrives as a bare 404. **A 404 cannot say "this API is older than this
    screen"**, so the diagnosis took a session each time.

    It is public because the thing that needs it is a dashboard that may not be able to sign in
    against a mismatched API, and because a commit hash of a private repository is not a
    secret: it names a build without describing it. Nothing else is here -- no dependency
    versions, no paths, no settings -- because an inventory of what is installed is a list of
    which advisories to try.

    `unknown` is answered honestly rather than guessed. A development run has no build
    arguments, and a deployment that does not know its own build must say so, because the
    comparison the dashboard makes is only sound when both sides know theirs.
    """
    return {
        "commit": settings.BUILD_COMMIT or "unknown",
        "built_at": settings.BUILD_TIME or "unknown",
        "started_at": _STARTED_AT.isoformat(),
    }
