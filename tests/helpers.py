"""Shared test helpers.

A real module rather than something imported out of a conftest: with two `conftest.py` files on
the path, a bare `import conftest` resolved to whichever came first and silently lost this
function.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path
from types import ModuleType

_TRIPLE_QUOTES = ('"""', "'''")


def executable_source(module: ModuleType) -> str:
    """Return a module's source with comments and docstrings removed.

    Several tests assert that a dangerous string no longer appears in a module — a lock against
    quietly reintroducing it. A plain source grep would also match the comments that *explain*
    why the string was removed, so those are stripped first. Ordinary string literals are kept,
    because that is the form a reintroduced bug would take (`status="MEETING_BOOKED"`).
    """
    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.string.lstrip("rbfuRBFU")[:3] in _TRIPLE_QUOTES:
            continue  # docstring or long prose block
        kept.append(token.string)
    return "\n".join(kept)
