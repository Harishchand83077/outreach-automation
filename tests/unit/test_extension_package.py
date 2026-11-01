"""The extension package's promises, checked from the Python suite (F8, slice S13.4).

`extension/tests/safety.test.ts` already asserts all of this, and this file asserts it again on
purpose. The reason is where each guard runs: the npm suite needs `npm ci` in the `extension`
directory, so it is one CI job that can be skipped, reordered or forgotten, while this one runs
in the suite that gates every change to the repository. The same bargain as
`test_connect_sends_mailbox_id.py` and `test_dashboard_state_is_declared.py` — a source check
in the suite that always runs beats a better check in a suite that sometimes does.

What it proves is the claim the whole feature rests on: **the extension suggests and cannot do
anything else.** Not by policy, by absence of the code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "extension"
SRC = PACKAGE / "src"

#: Line and block comments removed, so the prose explaining a rule cannot satisfy it. Every one
#: of the forbidden names below appears in a comment in this package, deliberately — the comments
#: are where the reasons live.
_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE = re.compile(r"^\s*//.*$", re.MULTILINE)


def _code(path: Path) -> str:
    return _LINE.sub(" ", _BLOCK.sub(" ", path.read_text(encoding="utf-8")))


def _sources() -> list[Path]:
    return sorted(SRC.rglob("*.ts"))


def test_the_package_is_present_and_has_source() -> None:
    """A guard over an empty directory passes for the wrong reason."""
    assert (PACKAGE / "public" / "manifest.json").is_file()
    assert len(_sources()) >= 5


@pytest.mark.parametrize(
    ("pattern", "promise"),
    [
        (r"\.submit\s*\(|requestSubmit\s*\(", "the extension never submits a form"),
        (r"\bDataTransfer\b|\.files\s*=|new\s+File\s*\(|FormData", "it never uploads a file"),
        (r"dispatchEvent\s*\(|\.click\s*\(", "it never acts on the page for the user"),
        (
            r"chrome\.webNavigation|chrome\.history|chrome\.cookies|chrome\.tabs\.sendMessage",
            "it reads a page only on a button press, under activeTab",
        ),
        (
            r"getAttribute\(\s*[\"']value[\"']\s*\)|\.checked\b",
            "it reads a field's question, never its contents",
        ),
    ],
)
def test_nothing_in_the_package_could_break(pattern: str, promise: str) -> None:
    offenders = [path.name for path in _sources() if re.search(pattern, _code(path))]

    assert offenders == [], f"{offenders} breaks the promise that {promise}"


def test_only_the_panels_own_textarea_is_ever_written() -> None:
    """`value =` is allowed in the panel and nowhere else.

    Writing a suggestion into the panel's own textarea is the feature. Writing one into a page's
    field is the thing this extension exists not to do — and the panel has no handle on the page
    to do it with, which the next test is about.
    """
    writers = [path.name for path in _sources() if re.search(r"\.value\s*=(?!=)", _code(path))]

    assert writers == ["panel.ts"]


def test_there_is_no_channel_from_the_panel_into_the_page() -> None:
    """The strongest form of "it cannot fill the form": nothing is listening.

    `collect.ts` registers no message listener, so even a panel that wanted to write into the
    page has nothing to ask. A listener added here would be the first step towards one.
    """
    source = "\n".join(_code(path) for path in _sources())

    assert "chrome.runtime.onMessage" not in source
    assert "chrome.tabs.sendMessage" not in source


def test_one_fetch_to_one_path() -> None:
    """A second place a URL is decided is how a credential reaches a host nobody chose."""
    source = "\n".join(_code(path) for path in _sources())

    assert len(re.findall(r"\bfetch\s*\(", source)) == 1
    assert "/ext/v1" in _code(SRC / "api.ts")


def test_the_token_cannot_reach_the_other_two_doors() -> None:
    """ADR-039 from the client's side: there is no base path here for `/api/v1` or `/mcp`."""
    source = "\n".join(_code(path) for path in _sources())

    assert "/api/v1" not in source
    assert "/mcp" not in source


def test_the_token_is_stored_locally_and_never_synced() -> None:
    """`chrome.storage.sync` would copy a bearer credential to every browser the user signs into."""
    source = "\n".join(_code(path) for path in _sources())

    assert "chrome.storage.sync" not in source
    assert "chrome.storage.local" in source


class TestTheManifest:
    @staticmethod
    def _manifest() -> dict[str, object]:
        text = (PACKAGE / "public" / "manifest.json").read_text(encoding="utf-8")
        return dict(json.loads(text))

    def test_it_declares_no_content_script(self) -> None:
        """Nothing runs on any page until the button is pressed."""
        manifest = self._manifest()

        assert manifest["manifest_version"] == 3
        assert manifest["content_scripts"] == []
        assert "activeTab" in manifest["permissions"]  # type: ignore[operator]

    def test_it_ships_with_no_host_access(self) -> None:
        """The API origin is requested when the user saves its address, naming that one host."""
        manifest = self._manifest()

        assert manifest["host_permissions"] == []

    def test_it_asks_for_no_permission_that_would_watch_browsing(self) -> None:
        granted = set(manifest_permissions := self._manifest()["permissions"])  # type: ignore[call-overload]
        assert manifest_permissions is not None

        assert granted.isdisjoint({"tabs", "webNavigation", "history", "cookies", "downloads"})
