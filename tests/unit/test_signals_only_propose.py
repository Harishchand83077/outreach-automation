"""A signal proposes, and can never send, draft or create a lead (F11, ADR-049).

The rule that makes a watchlist safe is not that today's code happens to be careful. It is that
**nothing in the package can reach the things that act.** A signal is produced by a background
poll with no human in the loop, which is precisely the position ADR-034 wrote its rule for: a
thing that acts on its own may only ever restrict, and this one may only ever add a row to a
list somebody reads.

Asserted over the source, with comments and string constants stripped first, because the prose
explaining each rule names every symbol it forbids — the lesson from the referral guard that
failed on its own docstring.
"""

from __future__ import annotations

import ast
import pathlib

PACKAGE = pathlib.Path("app/services/signals")
ROUTER = pathlib.Path("app/api/routers/signals.py")

#: Modules that put an email on the wire, or write the row that the send loop reads.
FORBIDDEN_IMPORTS = (
    "app.services.mail",
    "app.db.outbox",
    "app.services.drafting",
    "app.worker.send_loop",
    "smtplib",
    "aiosmtplib",
)

#: Names that would mean this package decided to send, approve or create somebody to send to.
FORBIDDEN_NAMES = ("send_one", "claim_send", "SmtpSender", "approve", "save_draft")


class _Strip(ast.NodeTransformer):
    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _sources() -> list[tuple[pathlib.Path, ast.Module]]:
    paths = [*sorted(PACKAGE.rglob("*.py")), ROUTER]
    return [(path, _Strip().visit(ast.parse(path.read_text(encoding="utf-8")))) for path in paths]


def test_there_is_something_to_check() -> None:
    assert len(_sources()) >= 4, "the signals package has moved; this guard is looking at nothing"


def test_nothing_here_can_send() -> None:
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(name.startswith(bad) for bad in FORBIDDEN_IMPORTS), (
                    f"{path.name} imports {name}, which can put an email on the wire"
                )


def test_nothing_here_names_a_send_or_an_approval() -> None:
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
                raise AssertionError(f"{path.name} calls {node.attr}")
            if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                raise AssertionError(f"{path.name} names {node.id}")


def test_no_lead_is_ever_constructed_here() -> None:
    """**The rule of the slice.** A company hiring is a reason to write to somebody there; it is
    not consent, not an address and not a person. A signal that creates a lead would put
    somebody into a sending pipeline because their employer posted a job."""
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "Lead", f"{path.name} constructs a Lead"


def test_the_state_machine_is_never_driven_from_here() -> None:
    """A signal does not move a lead. Where it stands is decided by what was sent and what came
    back, and `domain/state.transition` is the one writer of that."""
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else target.id
                    if isinstance(target, ast.Name)
                    else ""
                )
                assert name != "transition", f"{path.name} moves a lead"


def test_the_guard_would_catch_a_lead_being_created() -> None:
    """A guard nobody has seen fail is a guard nobody should trust."""
    tree = _Strip().visit(ast.parse("def f():\n    return Lead(org_id=1)\n"))
    constructed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Lead"
    ]
    assert constructed, "the pattern this test matches on has stopped matching"
