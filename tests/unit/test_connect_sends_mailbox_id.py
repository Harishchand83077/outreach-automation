"""The dashboard must tell the server which mailbox it means.

**Written because it did not.** `/connect/google/status` and `/connect/google/start` have always
accepted `mailbox_id`; `App.jsx` sent neither. On an org with one mailbox that is invisible,
because the server's fallback picks the only row there is. On an org with three it meant the
Sending-account panel described a mailbox belonging to a different campaign, and Connect Gmail
would have attached the credential to it.

`tests/integration/test_connect_mailbox_choice.py` proves the server honours the id. This file
proves somebody sends it, which is the half that was actually broken. An API that correctly
honours a parameter nobody passes is not a working feature.

**Why this is a source check rather than a component test.** The frontend's test suite covers
`api.js`, the only module with real logic, and has no component-rendering dependencies. Adding
React Testing Library to assert one fetch argument would be a new dependency tree to audit for
the life of the project, for a guard that is really about one line not getting dropped. This
repo already takes the structural route where the property is "the code says X" rather than "the
code does X" -- `test_send_guard.py` walks the whole tree for SMTP calls, and
`test_cors_methods.py` walks the router for declared methods. This is the same bargain: cheap,
dependency-free, and it fails for exactly the reason the defect happened.

Its limit is honest: it proves the id reaches the call, not that the value is the right one. The
integration test covers what the server then does with it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_JSX = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx"


def source() -> str:
    return APP_JSX.read_text(encoding="utf-8")


def test_the_file_is_where_this_test_thinks_it_is() -> None:
    """Guards every assertion below from passing vacuously.

    A missing or moved file would make `source()` raise rather than silently pass, but a *renamed
    route* would not -- so the route strings are asserted to be present before anything asserts
    what sits next to them. A test that cannot fail is worse than no test, because it is counted
    as coverage.
    """
    text = source()

    assert APP_JSX.is_file(), f"{APP_JSX} does not exist"
    assert "/connect/google/status" in text
    assert "/connect/google/start" in text


def _strip_comments(text: str) -> str:
    """Drop `//` lines.

    Necessary, not tidiness: the comments explaining this very fix contain the word `mailbox_id`,
    so a region that merely *talks* about the parameter would satisfy an assertion meant to prove
    the parameter is *sent*. Deleting the code and leaving the comment is a plausible edit, and it
    is precisely the edit this test exists to catch.
    """
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def _call_region(text: str, marker: str, *, before: int = 400, after: int = 400) -> str:
    """The code around a route string, which is where its arguments live."""
    index = text.index(marker)
    return _strip_comments(text[max(0, index - before) : index + after])


def test_status_is_asked_about_a_specific_mailbox() -> None:
    region = _call_region(source(), "/connect/google/status")

    assert "mailbox_id" in region, (
        "the connect status call does not mention mailbox_id, so the server falls back to the "
        "org's oldest mailbox and the panel can describe a mailbox from another campaign"
    )


def test_start_is_told_which_mailbox_to_connect() -> None:
    region = _call_region(source(), "/connect/google/start")

    assert "mailbox_id" in region, (
        "the connect start call does not pass mailbox_id, so Connect Gmail attaches the "
        "credential to whichever mailbox is oldest in the org"
    )


def test_the_id_is_url_encoded_in_the_status_query() -> None:
    """It goes into a query string, and a value interpolated raw is how a query string breaks.

    A uuid never needs encoding, which is exactly why this is worth pinning: the habit has to
    survive the day the parameter stops being a uuid.
    """
    region = _call_region(source(), "/connect/google/status")

    assert "encodeURIComponent" in region


@pytest.mark.parametrize("route", ["/connect/google/status", "/connect/google/start"])
def test_neither_call_hardcodes_a_mailbox(route: str) -> None:
    """A literal uuid in the source would pass the checks above while being worse than the bug."""
    region = _call_region(source(), route)

    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", region, re.IGNORECASE
    ), f"a uuid is hardcoded near {route}"


def test_a_comment_alone_does_not_satisfy_the_check() -> None:
    """The guard on the guard.

    If `_strip_comments` stopped working, every assertion above would be satisfiable by prose,
    and this file would quietly become documentation that reports itself as coverage.
    """
    commented_out = "// we should pass mailbox_id here one day\ngetJson('/connect/google/status')"

    assert "mailbox_id" not in _strip_comments(commented_out)
