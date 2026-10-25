"""The CORS method allowlist must cover every method the routes actually declare.

**Written because it did not.** Two `PUT` routes shipped -- uploading a campaign attachment and
saving a campaign schedule -- while `allow_methods` still listed GET, POST, PATCH, DELETE and
OPTIONS. The browser's preflight for `PUT` came back `400`, `fetch` rejected at the network
layer, and the dashboard reported:

    The API at https://api.arkvane.in did not answer /campaigns/<id>/attachment with JSON.
    Check that the backend is running and that VITE_API_URL points at it.

Which sent the investigation at the backend, at `VITE_API_URL` and at DNS. The backend was
answering perfectly; nothing was wrong with any of them. **The API log had no entry at all**,
because a refused preflight means the real request is never sent -- and "no log line" reads like
"the request never got here", which is true and unhelpful.

That is the same shape as every other defect in this project: a correct producer (routes
declaring `PUT`) and a correct consumer (a middleware enforcing a list), with nothing checking
that the two agreed. A list of methods maintained by memory is exactly the kind of agreement that
drifts, so this test maintains it instead.
"""

from __future__ import annotations

from app.api.app import create_app
from app.core.settings import Settings


def _app() -> object:
    """An app with CORS actually installed.

    `create_app` only adds `CORSMiddleware` when `CORS_ALLOWED_ORIGINS` is set --
    deliberately, since the worker and the health checks do not need it. CI runs
    without one, so a test that assumed the middleware was always there failed
    there while passing on a developer machine that happened to have the variable.
    Configuring it explicitly is also more honest: this is about the *list*, and
    the list only exists when CORS does.
    """
    return create_app(
        Settings(
            _env_file=None,
            GROQ_API_KEY="gsk_test",
            SMTP_EMAIL="asha@outreach.example",
            SMTP_PASSWORD="not-a-real-password",
            CORS_ALLOWED_ORIGINS="https://dashboard.example",
        )
    )


#: Methods Starlette adds on its own for every route. Not something the app declared, and not
#: something a browser preflights for a simple request.
_IMPLICIT = {"HEAD"}


def declared_methods() -> set[str]:
    """Every HTTP method reachable on this application.

    Walked recursively, because this FastAPI version keeps an `_IncludedRouter` wrapper per
    `include_router` call rather than flattening everything into `app.routes`. A non-recursive
    version finds nothing at all and the assertion passes vacuously -- which is worse than
    failing, so `test_the_walker_finds_the_routes` guards it.
    """
    return _walk(_app().routes) - _IMPLICIT


def _walk(routes: object) -> set[str]:
    """Collect methods, following whichever nesting this FastAPI version uses.

    `include_router` leaves an `_IncludedRouter` whose real routes hang off `original_router`,
    not off `routes`. Both are followed, so this keeps working if a future version flattens
    them again.
    """
    found: set[str] = set()
    for route in routes or []:  # type: ignore[union-attr]
        methods = getattr(route, "methods", None)
        if methods:
            found |= set(methods)
        for attr in ("routes", "original_router"):
            nested = getattr(route, attr, None)
            if nested is not None:
                found |= _walk(getattr(nested, "routes", nested))
    return found


def configured_methods() -> set[str]:
    """What the CORS middleware is configured to allow."""
    app = _app()
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            return set(middleware.kwargs["allow_methods"])
    raise AssertionError("CORSMiddleware is not installed")


def test_cors_methods_cover_every_route() -> None:
    """Every method a route declares is one a browser is allowed to preflight.

    The assertion a human would otherwise have to remember to make by hand, every time a route
    is added.
    """
    missing = declared_methods() - configured_methods()

    assert not missing, (
        f"these methods are used by routes but refused by CORS: {sorted(missing)}. "
        "A browser's preflight gets a 400 and the real request is never sent, which the "
        "dashboard reports as 'the API did not answer with JSON'."
    )


def test_put_is_allowed() -> None:
    """The specific regression. Named separately so a failure says what broke rather than
    producing a set difference somebody has to interpret."""
    assert "PUT" in configured_methods()


def test_options_is_allowed_or_no_preflight_can_succeed() -> None:
    assert "OPTIONS" in configured_methods()


def test_nothing_unused_is_allowed() -> None:
    """The allowlist is not a wish list.

    `OPTIONS` is exempt: it is the preflight itself and never appears as a route's own method.
    Allowing a method no route serves does not create a hole on its own -- the router still
    answers 405 -- but it does mean the list has stopped describing the application, and a list
    that has stopped being true is one nobody trusts enough to keep accurate.
    """
    extra = configured_methods() - declared_methods() - {"OPTIONS"}

    assert not extra, f"CORS allows methods no route uses: {sorted(extra)}"


def test_the_walker_finds_the_routes() -> None:
    """Guards the test above from passing vacuously.

    The first version of `declared_methods` checked `isinstance(route, Route)` against
    `app.routes` and found **nothing**, because this FastAPI version nests routers. Every
    assertion then compared against an empty set and passed while proving nothing. A test that
    cannot fail is worse than no test, because it is counted as coverage.
    """
    found = declared_methods()

    assert "GET" in found
    assert "POST" in found
    assert "PUT" in found, "the attachment and schedule routes declare PUT"
    assert len(found) >= 4
