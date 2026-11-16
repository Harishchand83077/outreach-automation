"""A membership looked up by subject alone must be ordered.

**Written because it was not.** `current_user` selected a membership with
`select(Member).where(Member.user_id == subject)` and no `order_by`, so Postgres was free to
return either row for a person who had two -- a different workspace per request, with nothing on
the screen naming the workspace to make it visible. `tests/integration/test_api_auth.py`'s
`TestSeveralMemberships` proves the behaviour; this file proves no *other* lookup can reintroduce
it, including one written next year by somebody who never read that test.

**Why a source scan rather than a test per call site.** The property is "every query of this shape
says which row it wants", which is a property of the tree, not of one function. The repo already
takes this route where that is the case: `test_send_guard.py` walks the whole tree for SMTP calls
and `test_cors_methods.py` walks the router for declared methods. A query scoped to one org as
well (`user_id` *and* `org_id`) is excluded, because `uq_members_org_user` makes it single by
construction -- there is nothing to order.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path("app")

#: Scripts are run by hand by one person against one database and report what they find; a
#: smoke check that picked a surprising row would say so on the terminal.
SKIP = {pathlib.Path("app/scripts/mcp_smoke.py")}


def _mentions(node: ast.AST, *names: str) -> bool:
    """Does this expression name any of these attributes anywhere inside it?"""
    wanted = set(names)
    return any(
        isinstance(child, ast.Attribute) and child.attr in wanted for child in ast.walk(node)
    )


def _selects_a_member_by_subject(call: ast.Call) -> bool:
    """`select(Member)` filtered on `Member.user_id`, and not also on `Member.org_id`."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "select"):
        return False
    if not any(isinstance(arg, ast.Name) and arg.id == "Member" for arg in call.args):
        return False
    return True


def _chain_root(node: ast.AST) -> ast.AST:
    """Walk back down `select(...).where(...).order_by(...)` to the `select(` call."""
    current = node
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        current = current.func.value
    return current


def _inner_links(tree: ast.AST) -> set[int]:
    """Chain nodes that something else is chained onto.

    `ast.walk` yields `select(...)`, `select(...).where(...)` and the whole thing as three
    separate calls, and the inner two cannot see the `order_by` that comes after them -- so
    judging each one would report the same query three times and condemn a correct one. Only the
    outermost link of a chain is a query that actually runs.
    """
    inner: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            inner.add(id(node.func.value))
    return inner


def _unordered_subject_lookups() -> list[str]:
    problems: list[str] = []
    for path in sorted(APP.glob("**/*.py")):
        if path in SKIP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inner = _inner_links(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in inner:
                continue
            root = _chain_root(node)
            if not (isinstance(root, ast.Call) and _selects_a_member_by_subject(root)):
                continue
            # The whole chain, so `.where(...)`'s arguments and `.order_by(...)` are both visible.
            text = ast.unparse(node)
            if "user_id" not in text:
                continue
            if "org_id" in text:
                # Scoped to one org as well, so `uq_members_org_user` makes it single.
                continue
            if "order_by" in text:
                continue
            problems.append(f"{path.as_posix()}:{node.lineno}: {text}")
    return problems


def test_no_membership_is_resolved_by_subject_without_an_order() -> None:
    problems = _unordered_subject_lookups()

    assert not problems, (
        "a membership is selected by user_id with no order_by, so a person with two of them "
        "gets an arbitrary workspace and no error says so:\n  " + "\n  ".join(problems)
    )


def test_the_guard_can_see_the_lookups_it_is_written_for() -> None:
    """If this ever reads nothing, its passing means nothing.

    Asserted by finding the *ordered* lookups: the ones the guard would have to notice if the
    `order_by` were deleted. A count would rot on the next refactor, so this checks the two files
    that resolve a membership by subject are both still being parsed.
    """
    seen: set[str] = set()
    for path in sorted(APP.glob("**/*.py")):
        if path in SKIP:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inner = _inner_links(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in inner:
                continue
            root = _chain_root(node)
            if not (isinstance(root, ast.Call) and _selects_a_member_by_subject(root)):
                continue
            text = ast.unparse(node)
            if "user_id" in text and "org_id" not in text and "order_by" in text:
                seen.add(path.as_posix())

    assert "app/api/deps.py" in seen, seen
    assert "app/services/signup.py" in seen, seen
