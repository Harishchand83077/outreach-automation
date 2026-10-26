"""Every `setX` the dashboard calls must actually exist.

**Written because two of them did not, in two consecutive changes, and nothing noticed.**

`App.jsx` called `setChecks(report)` with no `const [checks, setChecks] = useState(...)` anywhere
in the file. Vite does not resolve identifiers at build time, so `npm run build` was green; the
frontend suite covers `api.js` and renders nothing, so `npm test` was green; and the four CI jobs
were green. The button threw `ReferenceError` the first time a person pressed it, which is the one
moment nobody was watching.

This is the same bargain as `tests/unit/test_send_guard.py` and
`tests/unit/test_connect_sends_mailbox_id.py`: a source check rather than a component test.
Adding ESLint would catch this and more, and it is a dependency tree to audit for the life of the
project for a file of three thousand lines that no linter has ever been pointed at. A rule that
fits in a function is worth more than a rule that arrives with a toolchain.

It strips comments first, because the prose explaining a piece of state names it, and prose must
not satisfy an assertion about code.
"""

from __future__ import annotations

import pathlib
import re

APP = pathlib.Path("frontend/src/App.jsx")

#: A React state setter by convention: `set` then an upper-case letter. Not preceded by a dot, so
#: `localStorage.setItem` is a method call rather than a free identifier that must be declared.
_SETTER = re.compile(r"(?<![.\w])set[A-Z]\w*")

#: Platform globals that look like setters and are not. Listed explicitly rather than
#: pattern-matched: an allowlist that is awkward to extend is an allowlist doing its job.
_GLOBALS = frozenset({"setTimeout", "setInterval", "setImmediate"})
#: `const [thing, setThing] = useState(...)`
_USE_STATE = re.compile(r"const\s*\[\s*\w+\s*,\s*(set[A-Z]\w*)\s*\]")
#: `const setThing = ...` and `function setThing(`
_PLAIN = re.compile(r"(?:const|let|function)\s+(set[A-Z]\w*)\b")
#: A setter arriving as a destructured prop: `function Foo({ value, setThing })`
_PROP = re.compile(r"function\s+\w+\s*\(\s*\{([^}]*)\}")


def _code_only(source: str) -> str:
    """Drop comments and strings, so prose and copy cannot satisfy this test.

    Deliberately crude. The cost of a false *negative* here is a missed typo; the cost of a false
    positive is somebody deleting a real guard to get CI green, which is worse.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    source = re.sub(r"^\s*//.*$", " ", source, flags=re.M)
    source = re.sub(r"\s//\s.*$", " ", source, flags=re.M)
    return source


def test_every_setter_the_dashboard_calls_is_declared() -> None:
    source = _code_only(APP.read_text(encoding="utf-8"))

    declared: set[str] = set(_USE_STATE.findall(source)) | set(_PLAIN.findall(source))
    for params in _PROP.findall(source):
        declared.update(_SETTER.findall(params))

    used = set(_SETTER.findall(source))
    missing = sorted(used - declared - _GLOBALS)

    assert missing == [], (
        "App.jsx calls state setters that are never declared, so they throw "
        f"ReferenceError the first time somebody presses the control: {missing}"
    )


def test_the_check_would_have_caught_the_defect_it_was_written_for() -> None:
    """A guard nobody has seen fail is a guard nobody should trust.

    `setChecks(report)` with no `useState` beside it is exactly what shipped.
    """
    source = _code_only("function CampaignScreen() { setChecks(report) }")

    declared = set(_USE_STATE.findall(source)) | set(_PLAIN.findall(source))
    assert "setChecks" in set(_SETTER.findall(source)) - declared - _GLOBALS
