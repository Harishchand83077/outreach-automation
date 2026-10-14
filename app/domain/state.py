"""The lead state machine. Pure logic: no I/O, no database, no clock of its own.

`ARCHITECTURE_TARGET.md` §6.1 is the authority for the states and the legal transitions. The
rule that matters: **only one function may change `leads.state`**, it does so inside a
transaction that has the row locked, and it writes an `events` row in that same transaction. The
database-facing half of that lives in `app.db.state`; the decision about whether a move is
allowed lives here, so it can be exhaustively tested without a database.

The prototype had no state machine at all: any module could call
`update_lead(email=..., status="ANYTHING")` with a free-text string, and `WAITING_REPLY` was a
dead end that no loop watched (issues A6, E3).
"""

from __future__ import annotations

from enum import StrEnum


class LeadState(StrEnum):
    """Every state a lead can be in. Stored as text, constrained by this enum."""

    NEW = "NEW"
    ENRICHING = "ENRICHING"
    RESEARCHED = "RESEARCHED"
    DRAFTED = "DRAFTED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SENT = "SENT"
    FOLLOWUP_SENT = "FOLLOWUP_SENT"
    REPLIED = "REPLIED"
    IN_CONVERSATION = "IN_CONVERSATION"
    # Terminal
    MEETING_BOOKED = "MEETING_BOOKED"
    NOT_INTERESTED = "NOT_INTERESTED"
    OPTED_OUT = "OPTED_OUT"
    BOUNCED = "BOUNCED"
    NO_RESPONSE = "NO_RESPONSE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


#: States that end a lead's journey. `PAUSED` is deliberately not a state — it is a flag on the
#: org, campaign, mailbox or lead, so pausing never loses where a lead had got to.
TERMINAL: frozenset[LeadState] = frozenset(
    {
        LeadState.MEETING_BOOKED,
        LeadState.NOT_INTERESTED,
        LeadState.OPTED_OUT,
        LeadState.BOUNCED,
        LeadState.NO_RESPONSE,
        LeadState.SKIPPED,
        LeadState.FAILED,
    }
)

#: Terminal states a human may reopen. `OPTED_OUT` and `BOUNCED` never reopen, by design:
#: someone who asked to be left alone stays left alone.
RECOVERABLE: frozenset[LeadState] = frozenset(
    {
        LeadState.FAILED,
        LeadState.SKIPPED,
        LeadState.NOT_INTERESTED,
        LeadState.NO_RESPONSE,
    }
)

#: States where we are waiting on the lead, not on ourselves.
WAITING: frozenset[LeadState] = frozenset(
    {
        LeadState.SENT,
        LeadState.FOLLOWUP_SENT,
        LeadState.IN_CONVERSATION,
    }
)

#: States that need a person before anything else can happen.
NEEDS_HUMAN: frozenset[LeadState] = frozenset(
    {
        LeadState.AWAITING_APPROVAL,
        LeadState.REPLIED,
    }
)

#: The legal transition table. Anything absent here is a bug, not an edge case.
_ALLOWED: dict[LeadState, frozenset[LeadState]] = {
    LeadState.NEW: frozenset(
        {
            LeadState.ENRICHING,
            # A `template`-mode campaign has nothing to research: the operator wrote the email
            # and the lead's own columns fill it in. Routing it through ENRICHING and RESEARCHED
            # to keep the path uniform would mean recording research that never happened, and
            # the audit trail is the one thing that must not be decorative.
            LeadState.DRAFTED,
            LeadState.SKIPPED,
            LeadState.FAILED,
        }
    ),
    LeadState.ENRICHING: frozenset(
        {
            LeadState.RESEARCHED,
            LeadState.SKIPPED,
            LeadState.FAILED,
        }
    ),
    LeadState.RESEARCHED: frozenset({LeadState.DRAFTED, LeadState.FAILED}),
    LeadState.DRAFTED: frozenset(
        {
            LeadState.AWAITING_APPROVAL,
            LeadState.FAILED,
            LeadState.SKIPPED,
        }
    ),
    LeadState.AWAITING_APPROVAL: frozenset(
        {
            LeadState.APPROVED,
            LeadState.DRAFTED,  # rejected with feedback — regenerate
            LeadState.SKIPPED,
        }
    ),
    LeadState.APPROVED: frozenset(
        {
            LeadState.SENT,
            LeadState.SKIPPED,  # recipient not allow-listed, or suppressed before sending
            LeadState.FAILED,
        }
    ),
    LeadState.SENT: frozenset(
        {
            LeadState.FOLLOWUP_SENT,
            LeadState.REPLIED,
            LeadState.NO_RESPONSE,
            LeadState.BOUNCED,
            # A reply that is an unambiguous refusal closes the lead directly. Routing it
            # through REPLIED would briefly mark it as needing a human, and there is nothing
            # for a person to decide about "we already have a vendor for this".
            LeadState.NOT_INTERESTED,
        }
    ),
    LeadState.FOLLOWUP_SENT: frozenset(
        {
            LeadState.REPLIED,
            LeadState.NO_RESPONSE,
            LeadState.BOUNCED,
            LeadState.NOT_INTERESTED,
        }
    ),
    LeadState.REPLIED: frozenset(
        {
            LeadState.IN_CONVERSATION,  # our approved reply went out
            LeadState.MEETING_BOOKED,  # human action or a booking webhook only
            LeadState.NOT_INTERESTED,
            LeadState.OPTED_OUT,
            LeadState.BOUNCED,
        }
    ),
    LeadState.IN_CONVERSATION: frozenset(
        {
            LeadState.REPLIED,  # they wrote back again — multi-turn
            LeadState.MEETING_BOOKED,
            LeadState.NOT_INTERESTED,
            LeadState.OPTED_OUT,
            LeadState.NO_RESPONSE,
        }
    ),
    # A late reply reopens a lead we had given up on.
    LeadState.NO_RESPONSE: frozenset({LeadState.REPLIED}),
    # Human-recoverable terminals go back to the start of the pipeline.
    LeadState.FAILED: frozenset({LeadState.NEW, LeadState.SKIPPED}),
    LeadState.SKIPPED: frozenset({LeadState.NEW}),
    LeadState.NOT_INTERESTED: frozenset({LeadState.REPLIED}),
    # Never reopen.
    LeadState.MEETING_BOOKED: frozenset(),
    LeadState.OPTED_OUT: frozenset(),
    LeadState.BOUNCED: frozenset(),
}

#: An opt-out or a bounce can arrive while a lead is in any non-final state, and must win
#: immediately regardless of the table above. Suppression is not negotiable.
_ALWAYS_REACHABLE: frozenset[LeadState] = frozenset(
    {
        LeadState.OPTED_OUT,
        LeadState.BOUNCED,
    }
)

#: Only a person — or a booking webhook — may declare a meeting booked. It is never inferred
#: from a positive reply, which is exactly what the prototype did (issue B4).
HUMAN_ONLY: frozenset[LeadState] = frozenset({LeadState.MEETING_BOOKED})


class IllegalTransition(Exception):
    """Raised when a caller tries to move a lead somewhere the machine does not allow."""

    def __init__(self, lead_id: object, from_state: LeadState, to_state: LeadState) -> None:
        self.lead_id = lead_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"lead {lead_id}: {from_state} -> {to_state} is not a legal transition")


def is_allowed(from_state: LeadState, to_state: LeadState) -> bool:
    """True if a lead may move from `from_state` to `to_state`.

    Self-transitions are refused: re-entering the same state would write a misleading audit
    event and usually means a caller lost track of where the lead was.
    """
    if from_state == to_state:
        return False
    if to_state in _ALWAYS_REACHABLE and from_state not in (
        LeadState.OPTED_OUT,
        LeadState.BOUNCED,
        LeadState.MEETING_BOOKED,
    ):
        return True
    return to_state in _ALLOWED.get(from_state, frozenset())


def assert_allowed(lead_id: object, from_state: LeadState, to_state: LeadState) -> None:
    """Raise `IllegalTransition` unless the move is legal."""
    if not is_allowed(from_state, to_state):
        raise IllegalTransition(lead_id, from_state, to_state)


def is_terminal(state: LeadState) -> bool:
    return state in TERMINAL


def needs_human(state: LeadState) -> bool:
    return state in NEEDS_HUMAN


def reachable_from(state: LeadState) -> frozenset[LeadState]:
    """Every state reachable in one legal step. Used by the API to offer valid actions."""
    direct = _ALLOWED.get(state, frozenset())
    if state in (LeadState.OPTED_OUT, LeadState.BOUNCED, LeadState.MEETING_BOOKED):
        return direct
    return direct | _ALWAYS_REACHABLE
