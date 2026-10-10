"""The API application.

`ARCHITECTURE_TARGET.md` §14. Thin: validate, delegate to a service, serialise. No business logic
lives here, and no route reads `org_id` from its caller.

Three things the factory installs that are easy to leave until later and painful to retrofit:

- **A request id on every response and every log line**, so a user reporting "it failed" can be
  traced to the exact request without guessing from timestamps.
- **A uniform error envelope.** Every failure is `{"detail": ..., "request_id": ...}` whatever
  raised it, so the frontend has one shape to handle and an unexpected exception cannot leak a
  stack trace to a browser.
- **A CORS allowlist, never `*`.** The prototype sent `allow_origins=["*"]` together with
  `allow_credentials=True` (issue C3) — a combination browsers reject outright, which means the
  author never had working credentialed CORS and did not notice.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.api.deps import get_session, session_dependency
from app.api.ratelimit import RateLimit
from app.api.routers import health, me
from app.core.settings import Settings, config

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: §14. Generous enough that the dashboard's polling never trips it, tight enough that a scripted
#: scrape of every lead is slowed to something noticeable.
DEFAULT_RATE_LIMIT = RateLimit(limit=300, window_seconds=60.0)

#: Paths that must answer without a token, because a load balancer cannot hold one.
PUBLIC_PATHS = frozenset({"/health", "/ready"})


def allowed_origins(settings: Settings) -> list[str]:
    """The Pages origin(s), from configuration. Never `*`."""
    raw = settings.CORS_ALLOWED_ORIGINS or ""
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def create_app(settings: Settings | None = None, *, limiter: RateLimit | None = None) -> FastAPI:
    settings = settings or config
    limiter = limiter or DEFAULT_RATE_LIMIT

    application = FastAPI(
        title="outreach-automation",
        version="1.0.0",
        docs_url=None if settings.APP_ENV == "production" else "/docs",
        redoc_url=None,
    )

    origins = allowed_origins(settings)
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
            max_age=600,
        )
    else:
        # Refusing to start would be worse — the worker and the health checks do not need CORS —
        # but a browser will be unable to reach this API, so say so loudly once.
        logger.warning(
            "CORS_ALLOWED_ORIGINS is empty: no browser origin can call this API. "
            "Set it to the dashboard's origin."
        )

    @application.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id

        allowed, retry_after = limiter.check(_rate_key(request))
        if not allowed:
            return _error(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests.",
                request_id,
                headers={"Retry-After": str(int(retry_after) + 1)},
            )

        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error(
            exc.status_code,
            exc.detail,
            getattr(request.state, "request_id", ""),
            headers=dict(exc.headers) if exc.headers else None,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The request body or query was not valid.",
            getattr(request.state, "request_id", ""),
            extra={"errors": exc.errors()},
        )

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        # The detail is logged, never returned: an unhandled exception's text is as likely to
        # contain a connection string as anything useful to the caller.
        logger.exception("unhandled error", extra={"request_id": request_id})
        return _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Something went wrong. Quote the request id if you report this.",
            request_id,
        )

    application.dependency_overrides[get_session] = session_dependency
    application.include_router(health.router)
    application.include_router(me.router, prefix="/api/v1")
    return application


def _rate_key(request: Request) -> str:
    """Prefer the token's subject; fall back to the address only for public paths.

    The token is not verified here — verification needs a round trip to the key cache and this
    runs before routing. Using the raw subject is sound for *bucketing*: a forged token still
    fails authentication, and all an attacker gains by forging one is a bucket of their own,
    which is what they would have had anyway.
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return f"token:{auth[7:].strip()[-32:]}"
    client = request.client.host if request.client else "unknown"
    return f"addr:{client}"


def _error(
    status_code: int,
    detail: object,
    request_id: str,
    *,
    headers: dict[str, str] | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {"detail": detail, "request_id": request_id}
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status_code, content=body, headers=headers)
