"""Opportunity status machine (S19.2a, ADR-069). Pure: no I/O, no database.

Eight states: saved → preparing → ready → applied → in_conversation →
interviewing → offer → closed.  ``closed`` is the only terminal state and
carries a reason from ``CLOSED_REASONS``.

**One function owns all transitions** — ``assert_allowed`` checks the graph,
and ``app/db/opportunity.py`` locks the row, calls it, writes the event and
updates the status in one transaction.  Nothing else may write
``opportunities.status``.  A structural test in
``tests/unit/test_opportunity_domain.py`` asserts that.
"""

from __future__ import annotations

from enum import StrEnum


class OpportunityStatus(StrEnum):
    SAVED = "saved"
    PREPARING = "preparing"
    READY = "ready"
    APPLIED = "applied"
    IN_CONVERSATION = "in_conversation"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    CLOSED = "closed"


#: States that end an opportunity's journey.
TERMINAL: frozenset[OpportunityStatus] = frozenset({OpportunityStatus.CLOSED})

#: Valid reasons when closing.  Stored as text, constrained by a check constraint.
CLOSED_REASONS: tuple[str, ...] = (
    "rejected",
    "withdrawn",
    "job_closed",
    "skipped",
    "no_response",
    "offer_accepted",
    "offer_declined",
)

#: Allowed forward moves between non-terminal states, in addition to any → closed.
#: A move not in this graph (and not → closed) is illegal.
_FORWARD: dict[OpportunityStatus, frozenset[OpportunityStatus]] = {
    OpportunityStatus.SAVED: frozenset({
        OpportunityStatus.PREPARING,
        OpportunityStatus.READY,
        OpportunityStatus.APPLIED,
        OpportunityStatus.IN_CONVERSATION,
    }),
    OpportunityStatus.PREPARING: frozenset({
        OpportunityStatus.READY,
        OpportunityStatus.APPLIED,
        OpportunityStatus.IN_CONVERSATION,
    }),
    OpportunityStatus.READY: frozenset({
        OpportunityStatus.APPLIED,
        OpportunityStatus.IN_CONVERSATION,
    }),
    OpportunityStatus.APPLIED: frozenset({
        OpportunityStatus.IN_CONVERSATION,
        OpportunityStatus.INTERVIEWING,
    }),
    OpportunityStatus.IN_CONVERSATION: frozenset({
        OpportunityStatus.INTERVIEWING,
    }),
    OpportunityStatus.INTERVIEWING: frozenset({
        OpportunityStatus.OFFER,
    }),
    OpportunityStatus.OFFER: frozenset(),
    OpportunityStatus.CLOSED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(
        self,
        from_status: OpportunityStatus,
        to_status: OpportunityStatus,
    ) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Cannot move opportunity from {from_status!r} to {to_status!r}"
        )


def assert_allowed(
    from_status: OpportunityStatus,
    to_status: OpportunityStatus,
) -> None:
    """Raise ``IllegalTransition`` if the move is not in the state graph.

    Every non-terminal status may move to ``closed``.  Terminal states may not
    move anywhere.  All other moves must be in ``_FORWARD``.
    """
    if from_status in TERMINAL:
        raise IllegalTransition(from_status, to_status)
    if to_status == OpportunityStatus.CLOSED:
        return  # any non-terminal may close
    allowed = _FORWARD.get(from_status, frozenset())
    if to_status not in allowed:
        raise IllegalTransition(from_status, to_status)
