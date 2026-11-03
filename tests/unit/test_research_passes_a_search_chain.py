"""The research handler must hand `research_lead` a search chain and a budget (F17, ADR-046).

**This test exists because it did not, for three phases.** `research_lead` has accepted `search`
and `budget` since Phase 3, `_try_search` was correct and tested, and `build_provider` and
`SearchBudget` existed and were tested -- while `handle_research_lead`, the only caller in the
system, passed neither. So `search` was None on every real research job and the search fallback
had never once run outside a test.

That is this project's signature defect: a correct producer, a correct consumer, and nothing
joining them. It is the same shape as research never queuing a draft, as nothing ever writing a
`Draft(kind="followup")`, as the two `draft_reply` payload shapes that matched nothing, and as
every outbound message going out with no `In-Reply-To`. Every one of those was invisible to a
test suite that exercised both halves separately.

A structural check rather than a component test, for the reason `test_connect_sends_mailbox_id`
is one: the property is "somebody passes this argument", and the only place that can be asserted
is the call site. Comments and string constants are stripped first, because the comment in the
handler explaining this fix names both keywords, and prose must never satisfy an assertion about
code.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


class _Strip(ast.NodeTransformer):
    """Blank every string constant, so a docstring or a message cannot satisfy a check."""

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _calls_to(path: Path, func: str) -> list[ast.Call]:
    tree = _Strip().visit(ast.parse(path.read_text(encoding="utf-8")))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == func)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == func)
        )
    ]


def _callers(func: str) -> list[tuple[Path, ast.Call]]:
    found: list[tuple[Path, ast.Call]] = []
    for path in APP.rglob("*.py"):
        if path.name == "service.py" and path.parent.name == "research":
            continue  # where it is defined, not where it is called
        for call in _calls_to(path, func):
            found.append((path, call))
    return found


class TestTheOnlyCallerPassesAChain:
    def test_research_lead_is_called_somewhere(self) -> None:
        """A guard whose target has been renamed passes for the wrong reason. The suite has one
        test of that shape already recorded as having proved nothing, so this one checks it is
        looking at something."""
        assert _callers("research_lead"), "nothing calls research_lead; this guard is stale"

    def test_every_caller_passes_both_search_and_budget(self) -> None:
        """Either alone does nothing. `_try_search` requires both to be non-None -- a chain with
        no budget cannot check a ceiling, and a budget with no chain has nobody to charge -- so
        passing one is the same silence as passing neither."""
        for path, call in _callers("research_lead"):
            names = {keyword.arg for keyword in call.keywords}
            assert "search" in names, f"{path.name} calls research_lead with no search provider"
            assert "budget" in names, f"{path.name} calls research_lead with no budget"


class TestTheSearchFallbackIsReachable:
    """The half the structural check cannot see: that the thing being passed is a chain built
    from the workspace's own keys, rather than any object at all."""

    def test_the_handler_builds_the_chain_from_the_workspace(self) -> None:
        handler = APP / "worker" / "handlers.py"
        tree = _Strip().visit(ast.parse(handler.read_text(encoding="utf-8")))

        built = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_chain"
        ]

        assert built, "the research handler does not build a search chain"
        # `build_chain` takes the OrgContext positionally, which is what scopes it to one
        # workspace. A call that passed no context could only be reaching across every org's
        # keys, so the arity is the assertion.
        assert all(len(node.args) >= 2 for node in built)
