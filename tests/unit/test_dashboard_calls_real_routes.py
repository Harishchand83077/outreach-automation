"""Every path the dashboard asks for must be a path the API serves.

**This is the project's signature defect pointed at the one boundary where it had no guard.**
A correct screen and a correct route with nothing checking they agree is how
`/connect/google/status` was called without the `mailbox_id` it accepts, how `api.js` flattened a
structured error into `[object Object]`, and how the review queue was deployed calling a cursor
parameter the running API did not have.

It is also the cheaper half of a bigger problem: **the dashboard called about thirty of ninety
routes.** Two whole phases of work -- the job-hunt product and the Phase 14 sending controls --
were reachable only with curl, which is the same failure as Phase 8's: deployed, correct and
unusable, and only opening it showed that. A test cannot insist a feature has a screen, but it
can insist that the screens we do have are asking for something real.

A source check rather than a rendering test, for the reason
`test_connect_sends_mailbox_id.py` is one: the frontend suite covers `api.js` and renders
nothing, and adding a browser to assert a URL string is a dependency tree to audit for the life
of the project.
"""

from __future__ import annotations

import pathlib
import re

from app.api.app import create_app
from app.core.settings import Settings

SOURCES = sorted(pathlib.Path("frontend/src").glob("*.jsx"))

#: Every function that takes an API path as its first argument: `api.js`'s own helpers, plus the
#: `useLoad` wrapper the newer screens load through.
#:
#: **`useLoad` is here because leaving it out made this guard blind to two whole screens.** The
#: first version listed only the `api.js` helpers, and pointing a panel at a route that does not
#: exist did not fail it -- because that panel calls `useLoad('/admin/...')` and the wrapper
#: calls `getJson` with a variable. A guard that cannot see the code it was written for is worse
#: than no guard, since its passing is read as evidence. `test_the_helper_list_is_complete`
#: keeps the list honest as `api.js` grows.
_PATH_TAKERS = (
    "getJson",
    "postJson",
    "putJson",
    "patchJson",
    "deleteJson",
    "uploadFile",
    "fetchBlob",
    "saveAs",
    "useLoad",
)

#: The opening of such a call, up to and including the quote that starts its path argument.
_CALL = re.compile(r"\b(?:" + "|".join(_PATH_TAKERS) + r")\s*\(\s*([`'\"])")

#: An exported function in `api.js` whose first parameter is named `path`.
_EXPORTED_TAKER = re.compile(
    r"export\s+(?:async\s+)?function\s+(\w+)\s*\(\s*path\b|"
    r"export\s+const\s+(\w+)\s*=\s*\(?\s*path\b"
)

#: `{anything}` in a FastAPI route pattern becomes `*`.
_PARAM = re.compile(r"\{[^}]*\}")


def _read_literal(source: str, start: int, quote: str) -> str | None:
    """Read one string literal, handling `${...}` holes that contain braces and quotes.

    Written by hand rather than as a regex because the first version was a regex and it produced
    two false positives on real call sites -- `/leads/${lead.id}/${paused ? 'resume' : 'pause'}`
    stopped at the inner quote, and `${provider || providers[0]?.provider || ''}` stopped at the
    first closing brace. A guard that reports working code as broken is one somebody deletes.
    """
    out: list[str] = []
    index = start
    depth = 0
    while index < len(source):
        char = source[index]
        if depth == 0 and char == quote:
            return "".join(out)
        if char == "\\":
            index += 2
            continue
        if quote == "`" and char == "$" and source[index : index + 2] == "${":
            depth += 1
            out.append("*")
            index += 2
            continue
        if depth:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            index += 1
            continue
        if char == "\n":
            return None  # not a single-line literal; not a path
        out.append(char)
        index += 1
    return None


def _normalise(path: str) -> str:
    """One shape for a dashboard call and a FastAPI route, so they can be compared.

    Query strings go: a route is identified by its path, and whether a parameter is supported is
    a different question from whether the route exists.
    """
    path = _PARAM.sub("*", path)
    path = path.split("?")[0]
    return path.rstrip("/") or "/"


def _matches(called: str, served: str) -> bool:
    """Whether one dashboard call could be this route.

    Segment by segment, with `*` on **either** side matching anything. Set membership was the
    first version and it was wrong in a way worth keeping: the dashboard builds
    `/leads/${lead.id}/${paused ? 'resume' : 'pause'}`, whose last segment is a literal chosen at
    run time rather than a path parameter. That is a real call to a real pair of routes, and a
    comparison that cannot express it reports working code as broken.
    """
    left = called.split("/")
    right = served.split("/")
    if len(left) != len(right):
        return False
    return all(a == b or "*" in (a, b) for a, b in zip(left, right, strict=True))


def _served() -> set[str]:
    app = create_app(
        Settings(  # type: ignore[call-arg]
            _env_file=None, GROQ_API_KEY="gsk_test", CREDENTIAL_KEY="unit-test-key"
        )
    )
    return {
        _normalise(path.replace("/api/v1", "", 1))
        for path in app.openapi()["paths"]
        if path.startswith("/api/v1")
    }


def _called() -> dict[str, str]:
    """Normalised path -> the file that asks for it."""
    found: dict[str, str] = {}
    for path in SOURCES:
        source = path.read_text(encoding="utf-8")
        for match in _CALL.finditer(source):
            raw = _read_literal(source, match.end(), match.group(1))
            if raw and raw.startswith("/"):
                found.setdefault(_normalise(raw), path.name)
    return found


def test_there_is_something_to_check() -> None:
    """A regex that matches nothing passes every assertion below it -- the shape of the
    `tools/list` ordering check that compared a list with itself."""
    assert SOURCES
    found = _called()
    assert len(found) > 45, (
        "the call-site scan has stopped matching; this guard is blind. It found "
        f"{len(found)} paths, and the dashboard calls far more than that."
    )
    # The newer screens load through `useLoad`, so at least one path must come from each of
    # them -- the exact blindness this list was widened to fix.
    assert "JobHunt.jsx" in found.values()
    assert "Sending.jsx" in found.values()


def test_every_path_the_dashboard_calls_is_served() -> None:
    served = _served()
    missing = sorted(
        f"{path} (in {where})"
        for path, where in _called().items()
        if not any(_matches(path, route) for route in served)
    )

    assert missing == [], (
        "the dashboard asks for paths this API does not serve, which is a request that fails "
        f"the first time somebody opens the screen: {missing}"
    )


def test_the_check_would_catch_a_path_that_does_not_exist() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    served = _served()

    assert any(_matches("/campaigns/*/mailboxes", route) for route in served)
    assert not any(_matches("/campaigns/*/mailbox-pool", route) for route in served)
    # And the shape that forced segment matching: a final segment chosen at run time.
    assert any(_matches("/leads/*/*", route) for route in served)
    # A hole must not match across a `/`, or every call would match something.
    assert not any(_matches("/leads/*", route) for route in served if route == "/leads/*/pause")


def test_the_helper_list_is_complete() -> None:
    """Every path-taking helper `api.js` exports must be one this guard looks for.

    Without this, adding a helper silently narrows the check: the new call sites would be
    invisible and the guard would keep passing, which is how the first version of it came to
    miss two entire screens.
    """
    source = pathlib.Path("frontend/src/api.js").read_text(encoding="utf-8")
    exported = {name for pair in _EXPORTED_TAKER.findall(source) for name in pair if name}

    assert exported, "no path-taking helper found in api.js; this check is looking at nothing"
    assert exported <= set(_PATH_TAKERS), sorted(exported - set(_PATH_TAKERS))
