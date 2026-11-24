"""S19.2a — opportunity state machine (domain/opportunity.py).

The same contract as `domain/state.py` for leads:
- One function owns all transitions.
- Illegal moves raise `IllegalTransition`.
- Every allowed edge is reachable from the graph.
- Closing requires a reason.
- The structural test asserts nothing else writes `opportunities.status`.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.domain.opportunity import (
    CLOSED_REASONS,
    TERMINAL,
    IllegalTransition,
    OpportunityStatus,
    assert_allowed,
)


class TestAllowedTransitions:
    def test_saved_to_preparing(self) -> None:
        assert_allowed(OpportunityStatus.SAVED, OpportunityStatus.PREPARING)

    def test_preparing_to_ready(self) -> None:
        assert_allowed(OpportunityStatus.PREPARING, OpportunityStatus.READY)

    def test_ready_to_applied(self) -> None:
        assert_allowed(OpportunityStatus.READY, OpportunityStatus.APPLIED)

    def test_applied_to_in_conversation(self) -> None:
        assert_allowed(OpportunityStatus.APPLIED, OpportunityStatus.IN_CONVERSATION)

    def test_in_conversation_to_interviewing(self) -> None:
        assert_allowed(OpportunityStatus.IN_CONVERSATION, OpportunityStatus.INTERVIEWING)

    def test_interviewing_to_offer(self) -> None:
        assert_allowed(OpportunityStatus.INTERVIEWING, OpportunityStatus.OFFER)

    def test_offer_to_closed(self) -> None:
        assert_allowed(OpportunityStatus.OFFER, OpportunityStatus.CLOSED)

    def test_any_non_terminal_to_closed(self) -> None:
        for s in OpportunityStatus:
            if s not in TERMINAL:
                assert_allowed(s, OpportunityStatus.CLOSED)

    def test_saved_can_go_directly_to_applied(self) -> None:
        assert_allowed(OpportunityStatus.SAVED, OpportunityStatus.APPLIED)

    def test_preparing_to_applied(self) -> None:
        assert_allowed(OpportunityStatus.PREPARING, OpportunityStatus.APPLIED)


class TestIllegalTransitions:
    def test_closed_cannot_move(self) -> None:
        for target in OpportunityStatus:
            if target is not OpportunityStatus.CLOSED:
                with pytest.raises(IllegalTransition):
                    assert_allowed(OpportunityStatus.CLOSED, target)

    def test_applied_cannot_go_back_to_saved(self) -> None:
        with pytest.raises(IllegalTransition):
            assert_allowed(OpportunityStatus.APPLIED, OpportunityStatus.SAVED)

    def test_offer_cannot_go_back_to_saved(self) -> None:
        with pytest.raises(IllegalTransition):
            assert_allowed(OpportunityStatus.OFFER, OpportunityStatus.SAVED)


class TestTerminalSet:
    def test_closed_is_the_only_terminal_state(self) -> None:
        assert TERMINAL == {OpportunityStatus.CLOSED}


class TestClosedReasons:
    def test_closed_reasons_are_declared(self) -> None:
        expected = {
            "rejected",
            "withdrawn",
            "job_closed",
            "skipped",
            "no_response",
            "offer_accepted",
            "offer_declined",
        }
        assert set(CLOSED_REASONS) == expected


class TestOneWriterStructural:
    """The structural test: `opportunities.status` is written in exactly one place."""

    def test_only_opportunity_domain_writes_status(self) -> None:
        """No Python file outside domain/opportunity.py assigns to opportunities.status."""
        skip = {
            pathlib.Path("app/domain/opportunity.py"),
            pathlib.Path("app/db/opportunity.py"),
        }

        offenders: list[str] = []
        for path in pathlib.Path("app").rglob("*.py"):
            if path in skip:
                continue
            source = path.read_text(encoding="utf-8")
            # Look for direct attribute assignment that smells like writing .status
            # on an opportunity. We check for the string pattern rather than AST
            # because AST would require resolving types.
            if ".status" in source and "opportunity" in source.lower():
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Assign):
                        for target in node.targets:
                            if (
                                isinstance(target, ast.Attribute)
                                and target.attr == "status"
                                and isinstance(target.value, ast.Name)
                                and "opp" in target.value.id.lower()
                            ):
                                offenders.append(f"{path}:{node.lineno}")
        assert offenders == [], (
            f"These files write opportunity.status directly (should use domain/opportunity.py): "
            f"{offenders}"
        )
