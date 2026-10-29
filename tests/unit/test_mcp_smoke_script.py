"""Structural guards on the live MCP smoke check (`app/scripts/mcp_smoke.py`).

The script exists to exercise the deployed `/mcp` endpoint over the real network, which the
integration suite cannot do: it drives a `TestClient` against a test database, and every defect
this project found on its first deploy was invisible to exactly that.

Doing so means **minting a real credential against a real database**, which is why this file
exists. Two properties have to hold or the cure is worse than the disease:

1. **It never prints the secret.** A smoke check whose output is pasted into a terminal, a CI
   log or a chat window must not carry a credential with it (hard rule 2).
2. **It revokes what it minted**, on every path including failure. A token left behind by a test
   run is a credential nobody remembers exists, which is the thing ADR-028's expiry and
   revocation were for.

These are source checks rather than behavioural ones, for the same reason
`test_connect_sends_mailbox_id.py` and `test_dashboard_state_is_declared.py` are: the properties
are about what the file *can* do, and a behavioural test would have to run the script against a
live deployment to observe them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "app" / "scripts" / "mcp_smoke.py"


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _printed_expressions(tree: ast.Module) -> list[ast.expr]:
    """Every argument handed to `print`, anywhere in the file."""
    printed: list[ast.expr] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            printed.extend(node.args)
    return printed


class TestItNeverPrintsTheSecret:
    """The one output channel this script has must not carry the credential."""

    def test_the_secret_attribute_is_never_printed(self, tree: ast.Module) -> None:
        # `MintedToken.secret` is the only place the plaintext exists. Reading it is the
        # script's job; printing it is the incident. Checked over the whole printed expression
        # tree, so `print(f"{minted.secret}")` and `print("t=" + minted.secret)` both fail.
        for expression in _printed_expressions(tree):
            for node in ast.walk(expression):
                if isinstance(node, ast.Attribute) and node.attr == "secret":
                    pytest.fail(
                        "mcp_smoke.py prints a `.secret` attribute. The script's output is "
                        "pasted into terminals and logs; the credential must not travel with it."
                    )

    def test_no_logging_call_takes_the_secret_either(self, tree: ast.Module) -> None:
        # `print` is not the only way out of a process. Structured logs are scrubbed as a second
        # defence (`app/core/logging.py`), and the first defence is not handing them a secret.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_log = isinstance(func, ast.Attribute) and func.attr in {
                "debug",
                "info",
                "warning",
                "error",
                "exception",
            }
            if not is_log:
                continue
            for argument in node.args + [kw.value for kw in node.keywords]:
                for inner in ast.walk(argument):
                    if isinstance(inner, ast.Attribute) and inner.attr == "secret":
                        pytest.fail("mcp_smoke.py logs a `.secret` attribute.")


class TestItCleansUpAfterItself:
    """A token minted by a check has to die with the check."""

    def test_it_revokes_the_token_it_minted(self, tree: ast.Module) -> None:
        revokes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "revoke_token"
        ]
        assert revokes, "mcp_smoke.py must revoke the token it minted."

    def test_the_revocation_is_in_a_finally_block(self, source: str, tree: ast.Module) -> None:
        """Not merely called — called on the failure path too.

        The run this matters for is the one that fails: an exception between minting and the
        happy-path revoke would leave a live credential behind, and a failing smoke check is
        exactly when nobody is reading the output carefully.
        """
        in_finally = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for statement in node.finalbody:
                for inner in ast.walk(statement):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "revoke_token"
                    ):
                        in_finally = True
        assert in_finally, (
            "The revocation must be in a `finally:` block. A smoke check that only cleans up "
            "when it passes leaves a credential behind precisely when it failed."
        )


class TestItCannotSend:
    """The same containment the endpoint has, asserted on the script that drives it."""

    def test_it_imports_nothing_that_can_send(self, tree: ast.Module) -> None:
        # `tests/unit/test_send_guard.py` covers the tree structurally; this is the narrower
        # rule that the mail services are not reachable from here at all, so a later edit
        # cannot use an import that is already in the file.
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("app.services.mail"), (
                    f"mcp_smoke.py imports {node.module}. A read-only check has no business "
                    "near the mail services."
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("app.services.mail")

    def test_it_only_calls_tools_the_catalogue_declares_read_only(self, source: str) -> None:
        """The script's tool list must come from the catalogue, not be typed out again.

        A hand-written list is a second copy of the catalogue, and this project's signature
        defect is two correct halves that disagree. If a tool is added, the check should
        exercise it without anybody remembering this file exists.
        """
        assert "CATALOGUE" in source or "BY_NAME" in source, (
            "mcp_smoke.py must read the tool list from `app.services.mcp.tools`, not restate it."
        )
