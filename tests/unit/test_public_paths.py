"""`PUBLIC_PATHS` must be the list of routes that really answer without a token (S17.3).

Found while adding `/version`: **`PUBLIC_PATHS` was read by nothing.** What makes a route public
in this app is declaring no identity dependency, and the frozenset was a comment with
parentheses -- so a route added without auth would not appear in it, and a route named in it
could quietly grow an auth dependency with the list still claiming otherwise.

Which is this project's signature shape once more: a correct declaration and a correct
implementation with nothing checking that they agreed. The cure is the one adopted after
`reason="one-click unsubscribe"` made every opt-out a 500 -- the two sides are compared by a
test rather than by whoever happens to read the file.

Asserted in **both** directions deliberately. A route that forgot its auth is the dangerous
one; a list that over-claims is the one that makes somebody reason wrongly about the first.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.api.app import PUBLIC_PATHS, create_app
from app.core.settings import Settings

#: The dependency functions that resolve a caller to an identity. Read off `app/api/deps.py`
#: rather than guessed at: `current_user` is the membership every org-scoped route takes,
#: `current_platform_operator` is ADR-024's scope above the workspaces, and `require_role` is
#: its role-gated form.
_IDENTITY = frozenset(
    {
        "current_user",
        "current_platform_operator",
        "require_role",
        "role_dependency",
    }
)

#: Routes that **do** authenticate, in the handler body rather than through a dependency.
#:
#: Deliberate rather than sloppy: ADR-028's two-doors rule needs each door to refuse the wrong
#: *kind* of key, which means reading the header itself and deciding -- a shared `Depends` would
#: accept every credential the other doors accept. The cost is that a dependency scan cannot
#: see it, so these are asserted separately by reading the handler, which is the difference
#: between a guard and a list of excuses.
#: Keyed by **method and path**, because `/mcp` also serves `GET` and `DELETE` stubs that answer
#: 405 by design (the 2026-07-28 revision dropped the standalone stream and the session delete)
#: and a 405 needs no credential to be correct. Found by this guard keyed on the path alone,
#: which is also why the unsubscribe endpoint's `GET`-reads-`POST`-acts split is expressible
#: here at all.
_AUTH_IN_BODY = frozenset(
    {
        ("POST", "/mcp"),
        ("GET", "/ext/v1/whoami"),
        ("POST", "/ext/v1/suggest"),
        ("POST", "/ext/v1/suggestions/{suggestion_id}/outcome"),
        # S19.4: job clipper, same bearer-in-header pattern as the other ext routes
        ("POST", "/ext/v1/jobs/capture"),
        # S22: remember an answer, already-saved check, recent saves
        ("POST", "/ext/v1/answers"),
        ("POST", "/ext/v1/jobs/lookup"),
        ("GET", "/ext/v1/jobs/recent"),
    }
)

#: The name each of those must call. One name, because both doors resolve their credential
#: through a module-level helper and a route that reached for the header directly would be a
#: third implementation of the same rule.
_BEARER = "_bearer"

#: Genuinely unauthenticated, each with the decision that made it so. Listed by exact method
#: and path rather than by prefix, so a *new* open route fails this test and has to be argued
#: for here instead of appearing quietly.
_DELIBERATELY_OPEN = frozenset(
    {
        # The 405 stubs on the MCP path. Named so that a *third* method appearing there is a
        # failure rather than a silent addition to a door.
        ("GET", "/mcp"),
        ("DELETE", "/mcp"),
        # ADR-033: a receiving mail server POSTs this by itself and a recipient must be able to
        # act without an account, so the token carries its own HMAC proof of which lead it names.
        ("GET", "/u/{token}"),
        ("POST", "/u/{token}"),
        # ADR-045: a booking tool posts from its own servers with no session. The URL token says
        # which workspace, the body signature says it really came from that workspace's tool.
        ("POST", "/hooks/booking/{token}"),
        # ADR-058: an OAuth 2.1 authorization server's required public surface. The token
        # endpoint authenticates the *grant* -- the code plus its PKCE verifier -- which is the
        # whole point of it.
        ("POST", "/oauth/token"),
        ("GET", "/.well-known/oauth-protected-resource"),
        ("GET", "/.well-known/oauth-authorization-server"),
        # ADR-018: Google's redirect is a plain browser navigation with no `Authorization`
        # header, so the `state` row is the authentication -- written by an authenticated
        # request moments earlier, naming the org and the user. Without it somebody could hand a
        # signed-in user a crafted callback and attach their own mailbox to that user's org.
        ("GET", "/api/v1/connect/google/callback"),
        # FastAPI's own, and `/docs` is closed in production by `create_app`.
        ("GET", "/openapi.json"),
        ("HEAD", "/openapi.json"),
        ("GET", "/docs"),
        ("HEAD", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("HEAD", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        ("HEAD", "/redoc"),
    }
)


def _routes() -> list[tuple[str, str, APIRoute]]:
    """`(method, served path, route)` for every route and method, prefix included.

    This FastAPI keeps `_IncludedRouter` wrappers in `app.routes` rather than flattening them,
    so a single-level `isinstance(route, APIRoute)` scan finds **zero** routes -- and a guard
    that scans nothing passes trivially while reading as evidence.
    `test_there_is_something_to_check` is why that cannot happen silently here.

    The path is rebuilt from the wrapper's prefix because the inner route keeps its own
    unprefixed spelling: `/campaigns` is served at `/api/v1/campaigns`, and comparing the inner
    one against `PUBLIC_PATHS` would match a `/version` that is not this `/version`.
    """
    app = create_app(Settings(_env_file=None))  # type: ignore[call-arg]
    found: list[tuple[str, str, APIRoute]] = []
    stack: list[tuple[str, object]] = [("", item) for item in app.routes]
    while stack:
        prefix, item = stack.pop()
        if isinstance(item, APIRoute):
            for method in sorted(item.methods or {"GET"}):
                found.append((method, prefix + item.path, item))
            continue
        inner = getattr(item, "original_router", None)
        if inner is None:
            continue
        context = getattr(item, "include_context", None)
        stack.extend(
            (prefix + (getattr(context, "prefix", "") or ""), route) for route in inner.routes
        )
    return found


def _dependency_names(dependant: Dependant) -> set[str]:
    """Every dependency function name in a route's resolved tree, however deeply nested."""
    out: set[str] = set()
    stack = [dependant]
    while stack:
        node = stack.pop()
        call = node.call
        if call is not None:
            out.add(getattr(call, "__name__", ""))
        stack.extend(node.dependencies)
    return out


def _requires_identity(route: APIRoute) -> bool:
    return bool(_dependency_names(route.dependant) & _IDENTITY)


def _calls(endpoint: object, name: str) -> bool:
    """Whether this function's body calls `name`.

    Parsed with `ast` rather than searched as text, so the comment explaining a door and the
    docstring describing it cannot satisfy an assertion about its code -- the lesson from the
    referral guard that passed on its own note saying addresses are discarded.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == name:
                return True
    return False


def test_there_is_something_to_check() -> None:
    """A vacuity check, because a walk that stops finding routes makes the rest pass.

    This project has already kept two guards that proved nothing -- one compared a list with
    itself, one scanned only `App.jsx` after the code had moved -- so a scan whose passing is
    read as evidence states its own preconditions.
    """
    routes = _routes()
    assert len(routes) > 50, f"only found {len(routes)} routes; the walk is broken"
    assert any(_requires_identity(r) for _, _, r in routes), "nothing requires an identity"
    assert not all(_requires_identity(r) for _, _, r in routes), "everything looks gated"


def test_every_path_in_the_list_really_is_public() -> None:
    """A list that over-claims is worse than no list: somebody reads it and stops checking."""
    served = {path: route for _, path, route in _routes()}
    missing = sorted(PUBLIC_PATHS - set(served))
    assert missing == [], f"PUBLIC_PATHS names routes this app does not serve: {missing}"

    for path in sorted(PUBLIC_PATHS):
        assert not _requires_identity(served[path]), f"{path} is listed public and needs a token"


def test_nothing_else_answers_without_a_token() -> None:
    """The direction that matters: a route that forgot its auth."""
    allowed = _DELIBERATELY_OPEN | _AUTH_IN_BODY
    offenders = sorted(
        f"{method} {path}"
        for method, path, route in _routes()
        if path not in PUBLIC_PATHS
        and (method, path) not in allowed
        and not _requires_identity(route)
    )
    assert offenders == [], (
        "these routes answer without resolving an identity. If that is deliberate, add each "
        f"one to _DELIBERATELY_OPEN with the reason: {offenders}"
    )


def test_a_route_that_authenticates_in_its_body_really_does() -> None:
    """The half that would otherwise be taken on trust.

    Exempting these from the dependency scan is only honest if something checks they do the
    work themselves.
    """
    served = {(method, path): route for method, path, route in _routes()}
    for key in sorted(_AUTH_IN_BODY):
        assert key in served, f"_AUTH_IN_BODY names {key}, which this app does not serve"
        assert _calls(served[key].endpoint, _BEARER), (
            f"{key} is exempt from the dependency scan and resolves no credential itself"
        )
