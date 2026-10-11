"""The lead state machine, exhaustively. No database, no clock.

The prototype had no machine at all — any module could write any string into `status`, and
`WAITING_REPLY` was a dead end no loop watched (issues A6, E3). These tests are the contract that
replaces that.
"""

from __future__ import annotations

import itertools

import pytest

from app.domain.state import (
    HUMAN_ONLY,
    NEEDS_HUMAN,
    RECOVERABLE,
    TERMINAL,
    WAITING,
    IllegalTransition,
    LeadState,
    assert_allowed,
    is_allowed,
    is_terminal,
    reachable_from,
)


class TestTheHappyPath:
    """The pipeline every lead is meant to walk."""

    def test_full_journey_is_legal_step_by_step(self):
        journey = [
            LeadState.NEW,
            LeadState.ENRICHING,
            LeadState.RESEARCHED,
            LeadState.DRAFTED,
            LeadState.AWAITING_APPROVAL,
            LeadState.APPROVED,
            LeadState.SENT,
            LeadState.REPLIED,
            LeadState.IN_CONVERSATION,
        ]
        for current, following in itertools.pairwise(journey):
            assert is_allowed(current, following), f"{current} -> {following} should be legal"

    def test_followup_then_reply(self):
        assert is_allowed(LeadState.SENT, LeadState.FOLLOWUP_SENT)
        assert is_allowed(LeadState.FOLLOWUP_SENT, LeadState.REPLIED)

    def test_multi_turn_conversation_loops(self):
        """A6: the prototype could not see a second reply at all."""
        assert is_allowed(LeadState.REPLIED, LeadState.IN_CONVERSATION)
        assert is_allowed(LeadState.IN_CONVERSATION, LeadState.REPLIED)

    def test_rejection_sends_the_draft_back_for_regeneration(self):
        assert is_allowed(LeadState.AWAITING_APPROVAL, LeadState.DRAFTED)


class TestIllegalMoves:
    def test_a_lead_cannot_skip_approval(self):
        """The whole point of the product: nothing is sent without a human."""
        assert not is_allowed(LeadState.DRAFTED, LeadState.SENT)
        assert not is_allowed(LeadState.DRAFTED, LeadState.APPROVED)
        assert not is_allowed(LeadState.RESEARCHED, LeadState.SENT)
        assert not is_allowed(LeadState.NEW, LeadState.SENT)

    def test_a_lead_cannot_be_sent_twice(self):
        assert not is_allowed(LeadState.SENT, LeadState.SENT)
        assert not is_allowed(LeadState.SENT, LeadState.APPROVED)

    def test_self_transitions_are_refused(self):
        for state in LeadState:
            assert not is_allowed(state, state), f"{state} -> itself should be refused"

    def test_meeting_booked_is_never_reached_from_a_machine_state(self):
        """B4: the prototype inferred MEETING_BOOKED from an LLM label. Only a human may set it,
        and only from REPLIED or IN_CONVERSATION — never from SENT or anywhere automated."""
        assert LeadState.MEETING_BOOKED in HUMAN_ONLY
        assert not is_allowed(LeadState.SENT, LeadState.MEETING_BOOKED)
        assert not is_allowed(LeadState.APPROVED, LeadState.MEETING_BOOKED)
        assert is_allowed(LeadState.REPLIED, LeadState.MEETING_BOOKED)
        assert is_allowed(LeadState.IN_CONVERSATION, LeadState.MEETING_BOOKED)

    def test_assert_allowed_raises_with_a_useful_message(self):
        with pytest.raises(IllegalTransition) as exc:
            assert_allowed("lead-1", LeadState.NEW, LeadState.SENT)
        assert exc.value.from_state is LeadState.NEW
        assert exc.value.to_state is LeadState.SENT
        assert "lead-1" in str(exc.value)


class TestSuppressionAlwaysWins:
    """An opt-out or bounce can arrive at any moment and must take effect immediately."""

    @pytest.mark.parametrize(
        "state",
        [
            LeadState.NEW,
            LeadState.ENRICHING,
            LeadState.DRAFTED,
            LeadState.AWAITING_APPROVAL,
            LeadState.APPROVED,
            LeadState.SENT,
            LeadState.FOLLOWUP_SENT,
            LeadState.REPLIED,
            LeadState.IN_CONVERSATION,
            LeadState.NO_RESPONSE,
        ],
    )
    def test_opt_out_reachable_from_anywhere_live(self, state):
        assert is_allowed(state, LeadState.OPTED_OUT)

    @pytest.mark.parametrize(
        "state",
        [LeadState.SENT, LeadState.FOLLOWUP_SENT, LeadState.APPROVED, LeadState.REPLIED],
    )
    def test_bounce_reachable_from_anywhere_live(self, state):
        assert is_allowed(state, LeadState.BOUNCED)

    def test_opt_out_and_bounce_never_reopen(self):
        """Somebody who asked to be left alone stays left alone."""
        for terminal in (LeadState.OPTED_OUT, LeadState.BOUNCED):
            assert reachable_from(terminal) == frozenset()
            for target in LeadState:
                assert not is_allowed(terminal, target)

    def test_a_booked_meeting_is_not_undone_by_a_late_bounce(self):
        assert not is_allowed(LeadState.MEETING_BOOKED, LeadState.BOUNCED)
        assert not is_allowed(LeadState.MEETING_BOOKED, LeadState.OPTED_OUT)


class TestTerminalsAndRecovery:
    def test_terminal_states_are_classified(self):
        assert is_terminal(LeadState.MEETING_BOOKED)
        assert is_terminal(LeadState.NO_RESPONSE)
        assert not is_terminal(LeadState.SENT)
        assert not is_terminal(LeadState.AWAITING_APPROVAL)

    def test_recoverable_terminals_can_be_retried_by_a_human(self):
        assert RECOVERABLE <= TERMINAL
        assert is_allowed(LeadState.FAILED, LeadState.NEW)
        assert is_allowed(LeadState.SKIPPED, LeadState.NEW)

    def test_a_late_reply_reopens_a_lead_we_gave_up_on(self):
        assert is_allowed(LeadState.NO_RESPONSE, LeadState.REPLIED)

    def test_not_interested_can_be_reopened(self):
        assert is_allowed(LeadState.NOT_INTERESTED, LeadState.REPLIED)

    def test_every_terminal_is_either_recoverable_or_deliberately_final(self):
        final = TERMINAL - RECOVERABLE
        assert final == {
            LeadState.MEETING_BOOKED,
            LeadState.OPTED_OUT,
            LeadState.BOUNCED,
        }


class TestClassificationSetsAreCoherent:
    """Guards against someone adding a state and forgetting to classify it."""

    def test_every_state_is_reachable_or_is_the_entry_point(self):
        reachable = {to for s in LeadState for to in reachable_from(s)}
        unreachable = set(LeadState) - reachable - {LeadState.NEW}
        assert unreachable == set(), f"unreachable states: {unreachable}"

    def test_no_state_is_both_terminal_and_waiting(self):
        assert TERMINAL & WAITING == frozenset()

    def test_no_state_is_both_terminal_and_needing_a_human(self):
        assert TERMINAL & NEEDS_HUMAN == frozenset()

    def test_every_non_terminal_state_leads_somewhere(self):
        """A non-terminal state with no exit is a trap — exactly what WAITING_REPLY was."""
        for state in LeadState:
            if is_terminal(state):
                continue
            assert reachable_from(state), f"{state} is a dead end but not marked terminal"

    def test_states_are_plain_strings_for_storage(self):
        assert LeadState.SENT == "SENT"
        assert str(LeadState.SENT) == "SENT"


class TestTheResearchPathIsReachable:
    """Every state the worker writes must be reachable from the one before it.

    This exists because `handle_research_lead` transitioned `NEW -> RESEARCHED`, which the table
    forbids, and **nothing in the system ever set `ENRICHING`**. Every research job did all of
    its work — fetched pages, called a model, stored findings — and then died on the final
    transition. The table was right and the caller was wrong, and no test compared the two.
    """

    def test_new_cannot_jump_straight_to_researched(self):
        from app.domain.state import LeadState, is_allowed

        assert not is_allowed(LeadState.NEW, LeadState.RESEARCHED)

    def test_the_path_the_worker_takes_is_legal_end_to_end(self):
        from app.domain.state import LeadState, is_allowed

        path = [
            LeadState.NEW,
            LeadState.ENRICHING,
            LeadState.RESEARCHED,
            LeadState.DRAFTED,
            LeadState.AWAITING_APPROVAL,
            LeadState.APPROVED,
            LeadState.SENT,
        ]
        for before, after in itertools.pairwise(path):
            assert is_allowed(before, after), f"{before} -> {after} must be legal"

    def test_drafted_cannot_be_skipped_on_the_way_to_approval(self):
        """The drafter jumped RESEARCHED -> AWAITING_APPROVAL, which the table refuses — after
        the model had been paid for and the draft already written. Two steps also leave an
        honest trail: a draft was produced, and then it passed validation.
        """
        from app.domain.state import LeadState, is_allowed

        assert not is_allowed(LeadState.RESEARCHED, LeadState.AWAITING_APPROVAL)
        assert is_allowed(LeadState.RESEARCHED, LeadState.DRAFTED)
        assert is_allowed(LeadState.DRAFTED, LeadState.AWAITING_APPROVAL)

    def test_a_lead_being_researched_can_still_be_skipped_or_failed(self):
        """A crash or a thin company must not strand a lead in ENRICHING forever."""
        from app.domain.state import LeadState, is_allowed

        assert is_allowed(LeadState.ENRICHING, LeadState.SKIPPED)
        assert is_allowed(LeadState.ENRICHING, LeadState.FAILED)

    def test_enriching_is_not_a_self_loop(self):
        """Which is why the handler only claims it when the lead is still NEW: a retried job
        arrives with the lead already ENRICHING."""
        from app.domain.state import LeadState, is_allowed

        assert not is_allowed(LeadState.ENRICHING, LeadState.ENRICHING)
