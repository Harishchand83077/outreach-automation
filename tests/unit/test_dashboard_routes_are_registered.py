"""Every screen has a route, a way to reach it, and something that draws it.

**Written for the slice that split `App.jsx` into one file per screen.** Three of this project's
guards already check that a screen asks for a route the API serves
(`test_dashboard_calls_real_routes.py`), reads fields that route returns
(`test_dashboard_reads_real_fields.py`) and declares the state it sets
(`test_dashboard_state_is_declared.py`). None of them notices the failure a refactor actually
produces: a component that still exists, still correct, with nothing routing to it.

That failure is ADR-048's defect in its purest form -- "deployed, correct and unusable" -- and it
is one line of a diff. A screen is registered in four places, and this asserts all four agree:

1. a `ROUTES` entry, so it has a URL somebody can send;
2. a `view === "<id>"` branch, so something draws it;
3. a `nav` entry whose `id` is a real route id, so the sidebar and the command palette can
   reach it (the palette is built from the same list);
4. a `nav` destination that is a path `ROUTES` can match, so the link does not land on the
   overview fallback.

A source check rather than a rendering test, for the reason the sibling guards are: the dashboard
suite renders nothing, and adding a browser to assert a string is a dependency tree to audit for
the life of the project.
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path("frontend/src/App.jsx")

#: `{ path: "/leads/:leadId", id: "leads", scoped: true }` -> ("/leads/:leadId", "leads").
_ROUTE = re.compile(r"""\{\s*path:\s*["']([^"']+)["']\s*,\s*id:\s*["'](\w+)["']""")

#: `view === "leads"` -> "leads". What actually draws a screen.
_BRANCH = re.compile(r"""view\s*===\s*["'](\w+)["']""")

#: A `nav` entry's id and, when it is a plain string rather than a template, its destination.
_NAV_ID = re.compile(r"""^\s*id:\s*["'](\w+)["'],?\s*$""", re.MULTILINE)
_NAV_TO = re.compile(r"""^\s*to:\s*["']([^"']+)["'],?\s*$""", re.MULTILINE)


def _source() -> str:
    return APP.read_text(encoding="utf-8")


def _nav_block(source: str) -> str:
    """The `const nav = [ ... ];` literal, which the sidebar and the palette are both built from."""
    start = source.index("const nav = [")
    # The list ends at the first `];` that starts a line, which is how this file is formatted.
    end = source.index("\n  ];", start)
    return source[start:end]


def _routes(source: str) -> list[tuple[str, str]]:
    start = source.index("const ROUTES = [")
    end = source.index("\n];", start)
    return _ROUTE.findall(source[start:end])


def _matches(pattern: str, path: str) -> bool:
    """`router.jsx`'s `match`, in Python: `:name` takes a segment and `*` takes the rest."""
    wanted = [p for p in pattern.split("/") if p]
    given = [p for p in path.split("/") if p]
    for index, part in enumerate(wanted):
        if part == "*":
            return True
        if index >= len(given):
            return False
        if part.startswith(":"):
            continue
        if part != given[index]:
            return False
    return len(wanted) == len(given)


def test_every_route_has_something_that_draws_it() -> None:
    source = _source()
    ids = {route_id for _, route_id in _routes(source)}
    drawn = set(_BRANCH.findall(source))
    orphans = sorted(ids - drawn)
    assert not orphans, (
        "these route ids have no `view === ...` branch, so the URL exists and renders the "
        f"overview instead: {orphans}"
    )


def test_every_branch_has_a_route() -> None:
    """The other direction: a screen drawn by an id no route produces is unreachable."""
    source = _source()
    ids = {route_id for _, route_id in _routes(source)}
    drawn = set(_BRANCH.findall(source))
    unreachable = sorted(drawn - ids)
    assert not unreachable, (
        "these ids are drawn but no `ROUTES` entry produces them, so nothing can reach the "
        f"screen: {unreachable}"
    )


def test_every_nav_entry_points_at_a_real_route() -> None:
    source = _source()
    routes = _routes(source)
    ids = {route_id for _, route_id in routes}
    nav = _nav_block(source)

    unknown = sorted(set(_NAV_ID.findall(nav)) - ids)
    assert not unknown, (
        f"these nav entries carry an id no `ROUTES` entry produces: {unknown}. The sidebar's "
        "`aria-current` and the breadcrumb both read the id back, so they would never light up."
    )

    for destination in _NAV_TO.findall(nav):
        assert any(_matches(pattern, destination) for pattern, _ in routes), (
            f"the nav links to {destination!r}, which no `ROUTES` pattern matches, so clicking it "
            "falls through to the overview."
        )


def test_the_guard_is_looking_at_something() -> None:
    """A guard that silently reads nothing passes for ever and proves nothing."""
    source = _source()
    assert len(_routes(source)) >= 10
    assert len(_BRANCH.findall(source)) >= 10
    assert _NAV_ID.findall(_nav_block(source))


def test_each_screen_is_drawn_in_the_group_its_route_sends_it_to() -> None:
    """A route without `scoped: true` is drawn before the campaign gate; a scoped one after it.

    `/opportunities` and `/today` had a `view ===` branch, so the guards above passed, but the
    branch sat after the campaign gate while the route was unscoped: the screen rendered nothing.
    """
    source = _source()
    scoped: dict[str, bool] = {}
    start = source.index("const ROUTES = [")
    for line in source[start : source.index("\n];", start)].splitlines():
        found = _ROUTE.search(line)
        if found:
            scoped[found.group(2)] = scoped.get(found.group(2), False) or "scoped: true" in line
    gate = source.index("!campaign && !campaignsLoaded")
    misplaced = []
    for route_id, is_scoped in scoped.items():
        for match in re.finditer(rf"""view\s*===\s*["']{route_id}["']""", source):
            if (match.start() > gate) != is_scoped:
                misplaced.append(route_id)
    assert not misplaced, (
        f"drawn in the wrong group, so they render nothing: {sorted(set(misplaced))}"
    )
