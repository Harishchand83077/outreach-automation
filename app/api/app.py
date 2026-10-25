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
from app.api.routers import campaigns, connect, health, imports, me, operations, review
from app.core.logging import configure_logging, log_context
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
    # Installed here rather than in a `__main__`: uvicorn imports the factory, so this is the
    # only place guaranteed to run before the first request is logged. Idempotent.
    configure_logging(settings)

    production = settings.APP_ENV == "production"
    application = FastAPI(
        title="outreach-automation",
        version="1.0.0",
        # In production all three go, not just the HTML page. Closing `/docs` while leaving
        # `/openapi.json` open hands over the same map in a more convenient format — every
        # route, every parameter, every schema, to anyone who finds the host. This was found
        # on the live deployment: `/docs` answered 404 and `/openapi.json` answered 200.
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )

    origins = allowed_origins(settings)
    if origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            # **Every method the routes actually declare**, which is not a list to keep in
            # your head: two PUT routes shipped and their preflight was refused with a 400,
            # so the browser reported "the API did not answer with JSON" about an API that
            # was answering fine. `test_cors_methods_cover_every_route` asserts this list
            # against the router, so the next method added cannot be forgotten here.
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
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

        # Bound for the whole request, so every log line underneath carries it without any
        # handler having to remember to pass it — including the ones inside services that know
        # nothing about HTTP (§18).
        with log_context(request_id=request_id):
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
        response = _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Something went wrong. Quote the request id if you report this.",
            request_id,
        )
        # **CORS headers, added by hand, because nothing else will add them here.** A handler
        # for bare `Exception` is installed in Starlette's `ServerErrorMiddleware`, which sits
        # *outside* every user middleware including `CORSMiddleware` — so a 500 never passes
        # back through it and arrives at the browser with no `Access-Control-Allow-Origin`. The
        # browser then refuses to expose the response and `fetch` rejects, so the dashboard
        # reported "the API did not answer with JSON; check that the backend is running" while
        # the backend was running perfectly and had answered with JSON.
        #
        # That cost real time on the live system: it pointed the investigation at CORS, DNS and
        # connectivity when the fault was a duplicate key three layers in. A 500 the operator
        # can read is worth this much duplication.
        #
        # Reflected only for an origin already on the allowlist — never echoed back to whoever
        # asked, which would be a wildcard with extra steps.
        origin = request.headers.get("origin")
        if origin and origin in origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Vary"] = "Origin"
        return response

    application.dependency_overrides[get_session] = session_dependency
    application.include_router(health.router)
    application.include_router(me.router, prefix="/api/v1")
    application.include_router(campaigns.router, prefix="/api/v1")
    application.include_router(review.router, prefix="/api/v1")
    application.include_router(imports.router, prefix="/api/v1")
    application.include_router(operations.router, prefix="/api/v1")
    application.include_router(connect.router, prefix="/api/v1")
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
