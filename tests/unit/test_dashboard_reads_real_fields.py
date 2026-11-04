"""Every field a screen reads off a response must be a field the route returns.

**Written because the Hiring contacts tab blanked the dashboard.** `/contact-candidates` returns
`contact_candidates`; the screen read `data.candidates`. `undefined.length` threw during render,
React unmounted the whole tree, and the operator got a white page with no message and no nav.

It is the project's signature defect once more, and it arrived one day after a guard was written
for the *paths* a screen calls. That guard passed: the path exists. Nothing compared the field
names, which is the half that fails silently — and silently here means the tool stops working
rather than one request failing.

The pair that matters:

* `test_dashboard_calls_real_routes` — the screen asks for a route that exists.
* this file — and reads fields that route returns.

A render error no longer blanks the dashboard either (`ErrorBoundary.jsx`), but a boundary turns
this class of bug into a visible message rather than preventing it. This prevents it.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROUTERS = sorted(pathlib.Path("app/api/routers").glob("*.py"))
SCREENS = sorted(pathlib.Path("frontend/src").glob("*.jsx"))

#: `const posts = useLoad('/job-posts', onError)` -> (posts, /job-posts). Only the wrapper, not
#: the raw helpers: a `getJson(...).then(setX)` loses the association between the path and the
#: name the fields are later read off, and guessing at it would produce false positives.
_LOADER = re.compile(r"const\s+(\w+)\s*=\s*useLoad\(\s*[`'\"]([^`'\"]+)[`'\"]")


def _field_reads(source: str, variable: str) -> set[str]:
    """`posts.data.job_posts` and `posts.data?.job_posts` -> {"job_posts"}."""
    pattern = re.compile(rf"\b{re.escape(variable)}\.data\??\.(\w+)")
    return set(pattern.findall(source))


def _returned_keys() -> dict[str, set[str]]:
    """Path -> the literal top-level keys its GET handler returns.

    Read from the `return {...}` of the decorated function. A handler whose return is not a dict
    literal is skipped rather than guessed at, and `test_enough_routes_were_understood` keeps
    this from quietly becoming vacuous.
    """
    found: dict[str, set[str]] = {}
    for path in ROUTERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            routes = {
                decorator.args[0].value
                for decorator in node.decorator_list
                if isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            }
            if not routes:
                continue
            keys: set[str] = set()
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Dict):
                    keys |= {
                        key.value
                        for key in inner.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
            if keys:
                for route in routes:
                    found.setdefault(route, set()).update(keys)
    return found


def _normalise(path: str) -> str:
    return re.sub(r"\$\{[^}]*\}|\{[^}]*\}", "*", path).split("?")[0].rstrip("/") or "/"


def _lookup(returned: dict[str, set[str]], called: str) -> set[str] | None:
    """The keys for one called path, matched segment-wise so a template hole lines up with a
    route parameter."""
    target = _normalise(called).split("/")
    for route, keys in returned.items():
        candidate = _normalise(route).split("/")
        if len(candidate) == len(target) and all(
            a == b or "*" in (a, b) for a, b in zip(candidate, target, strict=True)
        ):
            return keys
    return None


def _reads() -> list[tuple[str, str, str, set[str]]]:
    """(file, variable, path, fields read) for every `useLoad` in the dashboard."""
    out: list[tuple[str, str, str, set[str]]] = []
    for path in SCREENS:
        source = path.read_text(encoding="utf-8")
        for variable, route in _LOADER.findall(source):
            out.append((path.name, variable, route, _field_reads(source, variable)))
    return out


def test_there_is_something_to_check() -> None:
    """A regex that matches nothing passes every assertion below it."""
    reads = _reads()
    assert len(reads) >= 10, f"the loader scan found only {len(reads)} call sites"
    assert any(fields for _, _, _, fields in reads), "no field reads found at all"


def test_enough_routes_were_understood() -> None:
    """A dict-literal parser that stops working would make the check vacuous rather than red."""
    returned = _returned_keys()
    assert len(returned) >= 25, f"only understood {len(returned)} GET handlers"
    assert "contact_candidates" in returned["/contact-candidates"]


def test_every_field_a_screen_reads_is_a_field_the_route_returns() -> None:
    returned = _returned_keys()
    wrong: list[str] = []

    for where, variable, route, fields in _reads():
        keys = _lookup(returned, route)
        if keys is None:
            # The handler's return is not a dict literal, so there is nothing to compare against.
            continue
        for field in sorted(fields - keys):
            wrong.append(f"{where}: {variable}.data.{field} — {route} returns {sorted(keys)}")

    assert wrong == [], (
        "a screen reads fields its route does not return. Reading one is `undefined`, and using "
        "it throws during render: " + "; ".join(wrong)
    )


def test_the_check_would_have_caught_the_defect_it_was_written_for() -> None:
    """A guard nobody has seen fail is a guard nobody should trust.

    `candidates.data.candidates` against a route that returns `contact_candidates` is exactly
    what blanked the dashboard.
    """
    returned = _returned_keys()
    keys = _lookup(returned, "/contact-candidates")

    assert keys is not None
    assert "candidates" not in keys, "the defect was reading this name"
    assert "contact_candidates" in keys, "and this is the real one"
