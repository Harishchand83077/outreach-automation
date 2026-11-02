"""The referral finder touches no network and holds no address (F10, ADR-042).

Hard rule 4 is the oldest rule in this project: `linkedin_url` is stored and displayed, never
fetched. ADR-038 added that LinkedIn data enters only through a file the user uploads. This file
asserts both over the source tree, so the rules hold against code nobody has written yet -- the
same device as `test_send_guard.py` and `test_contacts_button_press_only.py`.

The second half is about what is *kept*. A connections export carries `Email Address`, and
storing it would turn a referral finder into somebody's entire address book, belonging to people
who agreed to connect with the user and have never heard of us. Three independent guards: the
parser drops the column, the schema has nowhere to put it, and a parse of a file containing an
address is checked for it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
PACKAGE = APP / "services" / "referrals"
ROUTER = APP / "api" / "routers" / "referrals.py"

_BLOCK = re.compile(r'"""(?:.|\n)*?"""')
_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)


def _code(path: Path) -> str:
    """Docstrings and comments stripped: the prose explaining a rule names every term in it."""
    return _LINE.sub(" ", _BLOCK.sub(" ", path.read_text(encoding="utf-8")))


def _sources() -> list[Path]:
    return [*sorted(PACKAGE.rglob("*.py")), ROUTER]


def test_there_is_source_to_check() -> None:
    assert len(_sources()) >= 4


class TestNothingFetchesAnything:
    def test_no_module_imports_an_http_client(self) -> None:
        """The strongest form of hard rule 4 here: there is nothing to fetch *with*."""
        offenders: list[str] = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in {"httpx", "requests", "urllib", "aiohttp", "socket"}:
                        offenders.append(f"{path.name}: {name}")

        assert offenders == []

    @pytest.mark.parametrize("host", ["linkedin.com", "www.linkedin.com", "api.linkedin"])
    def test_no_linkedin_host_appears_anywhere(self, host: str) -> None:
        """Not in a constant, not in a URL being built, not in a default argument.

        The profile URL that reaches a response comes from the user's own uploaded row and is
        shown for them to open. Nothing in this package constructs a LinkedIn address.
        """
        for path in _sources():
            assert host not in _code(path), f"{path.name} names {host}"

    def test_nothing_here_can_send(self) -> None:
        """A referral note is text the user pastes into LinkedIn themselves.

        The tree-wide send guard already covers this package, so this is the narrower claim:
        nothing here even reaches the outbox or the message builder.
        """
        source = "\n".join(_code(path) for path in _sources())

        for forbidden in ("smtp", "send_one", "claim_send", "build_message", "SendLoop"):
            assert forbidden not in source


class TestNoAddressIsKept:
    def test_the_parser_drops_the_email_column(self) -> None:
        from app.services.referrals.parsing import parse_export

        export = (
            b"Notes:\n"
            b'"When exporting your connection data..."\n'
            b"\n"
            b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            b"Asha,Rao,https://example.com/in/asha,asha@example.com,Acme Inc,"
            b"Head of Engineering,21 Mar 2024\n"
        )

        parsed = parse_export(export)

        assert len(parsed.connections) == 1
        assert "asha@example.com" not in repr(parsed.connections[0])

    def test_the_table_has_nowhere_to_put_one(self) -> None:
        """The second line of the same defence: a parser change could not start storing it."""
        from sqlalchemy import inspect

        from app.db.models import Connection

        columns = {column.name for column in inspect(Connection).columns}

        for forbidden in ("email", "email_norm", "address", "phone"):
            assert forbidden not in columns

    def test_the_router_returns_no_address(self) -> None:
        """No key and no read in any response mentions an address.

        **String literals are excluded, and the first version of this test did not exclude
        them** -- it failed on the router's own note saying that email addresses are discarded.
        Prose describing a rule must not be able to break it, which is the same reason the
        comment-stripping helper at the top of this file exists.
        """
        tree = ast.parse(ROUTER.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            for inner in ast.walk(node.value):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    continue
                if isinstance(inner, ast.Attribute):
                    assert "email" not in inner.attr.lower(), ast.unparse(inner)
                if isinstance(inner, ast.Name):
                    assert "email" not in inner.id.lower(), inner.id


class TestTheNoteIsBuiltByRules:
    def test_no_model_is_reachable_from_this_package(self) -> None:
        """A referral ask goes to somebody who knows the user.

        A fabricated line there is not a weak email -- it is the user telling a friend something
        untrue about themselves, to a reader who would notice. So the note is substitution into
        a fixed shape, and there is no router, no chain and no prompt anywhere in here.
        """
        source = "\n".join(_code(path) for path in _sources())

        for forbidden in ("LLMRouter", "llm", "structured(", "prompt"):
            assert forbidden not in source, (
                f"a model is reachable from the referral finder: {forbidden}"
            )
