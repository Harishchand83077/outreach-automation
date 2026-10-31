"""No caller may invent a suppression reason (slice S11.4).

`suppressions.reason` is constrained by the database to four values. A caller that passes a
fifth gets an `IntegrityError` at commit, which surfaces as a 500 explaining nothing — which is
exactly what the one-click unsubscribe route did on its first run, and what CI caught.

This is the project's most repeated defect in one line: **a literal that has to match a database
constraint, copied into a caller that cannot see it change.** Previous instances were
`direction == "outbound"` where the column holds `'out'`, `outcome != "ok"` where success is
`"success"`, and `Event.created_at` where the column is `Event.at`. The structural cure is to
make the constraint and the callers read the same tuple, and then to assert that they do.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.db.models import SUPPRESSION_REASONS

APP = Path(__file__).resolve().parents[2] / "app"


def _reason_arguments() -> list[tuple[Path, ast.expr]]:
    """Every `reason=` keyword passed to an `add_suppression` call in the tree."""
    found: list[tuple[Path, ast.expr]] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if name is None and isinstance(node.func, ast.Name):
                name = node.func.id
            if name != "add_suppression":
                continue
            for keyword in node.keywords:
                if keyword.arg == "reason":
                    found.append((path, keyword.value))
    return found


def test_the_constraint_and_the_code_share_one_tuple():
    """Four values, defined once. If this grows, the constraint grows with it."""
    assert SUPPRESSION_REASONS == ("opt_out", "bounce", "manual", "complaint")


def test_every_suppression_reason_in_the_tree_is_one_the_database_allows():
    callers = _reason_arguments()
    assert callers, "no add_suppression call sites found; has the function been renamed?"

    for path, node in callers:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value in SUPPRESSION_REASONS, (
                f"{path.name} passes reason={node.value!r}, which the database will refuse. "
                f"Allowed: {', '.join(SUPPRESSION_REASONS)}."
            )
        # Anything that is not a plain string -- a subscript of SUPPRESSION_REASONS, a variable,
        # a parameter -- is left alone deliberately. The point is to catch an invented literal,
        # not to refuse every expression; a caller reading the tuple is the behaviour wanted.
