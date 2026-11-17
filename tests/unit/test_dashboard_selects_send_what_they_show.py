"""A dropdown must submit the option it is displaying.

**Written because one did not, and the owner got a 500 for a form they filled in correctly.**
The hiring-contact lookup rendered:

    <select value={lookupWith || usable[0].provider} ...>

and posted `lookupWith`. The fallback lived in the markup only, so a dropdown nobody had touched
showed `hunter` and sent `""`. The server refused the empty name -- correctly -- and the refusal
escaped as "Something went wrong. Quote the request id if you report this."

A select is where this bites, and inputs are not: a text box showing a fallback is showing a
*draft* the person can see and edit, while a select showing the first option is making a choice
on their behalf that the submit path never learns about. So the rule is narrow on purpose --
`value={a || ""}` is fine everywhere, because an empty fallback displays nothing and sends
nothing.

The fix is always the same and always cheap: resolve the fallback once, above the handler, and
use that one name in both places. `JobHunt.jsx` already did exactly that for `chosenKind` a few
hundred lines above the select that did not.
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path("frontend/src")
SCREENS = sorted(SRC.glob("**/*.jsx"))

#: `<select ... value={...}>`, across newlines, up to the end of the opening tag.
_SELECT = re.compile(r"<select\b[^>]*?value=\{(?P<expr>[^}]*)\}", re.S)

#: A fallback to an empty string displays nothing, so there is nothing to disagree about.
_EMPTY_FALLBACK = re.compile(r"(\|\||\?\?)\s*(\"\"|'')\s*$")


def _offenders(source: str) -> list[str]:
    found: list[str] = []
    for match in _SELECT.finditer(source):
        expr = " ".join(match.group("expr").split())
        if "||" not in expr and "??" not in expr:
            continue
        if _EMPTY_FALLBACK.search(expr):
            continue
        found.append(expr)
    return found


def test_no_dropdown_decides_for_somebody_only_in_the_markup() -> None:
    problems: list[str] = []
    for path in SCREENS:
        for expr in _offenders(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}: value={{{expr}}}")

    assert not problems, (
        "a <select> displays a fallback its state does not hold, so an untouched dropdown "
        "submits something other than what it shows. Resolve it once above the handler and use "
        "that name in both places:\n  " + "\n  ".join(problems)
    )


def test_the_guard_can_see_the_screens_and_the_shape_it_forbids() -> None:
    """A regex that matches nothing passes quietly, which is worse than no rule at all."""
    assert len(SCREENS) > 5, SCREENS

    bad = "<select value={chosen || rows[0].id} onChange={x}>"
    good_empty = '<select value={chosen || ""} onChange={x}>'
    good_plain = "<select value={chosen} onChange={x}>"

    assert _offenders(bad), "the guard no longer recognises the defect it was written for"
    assert not _offenders(good_empty)
    assert not _offenders(good_plain)
