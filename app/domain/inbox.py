"""Inbox folder assignment — pure, no I/O.

A thread (one lead's conversation) belongs to exactly one folder.  The function
below is the single authority on that mapping, so the router and the frontend can
never disagree about what a folder means.

Folder precedence (highest first):
  archived  → the reviewer dismissed this thread permanently
  snoozed   → the reviewer asked to be reminded later (until > now)
  meeting_booked → terminal win; kept visible so the reviewer can confirm
  not_interested → terminal loss: lead state or reply label signals no fit
  wrong_person   → redirected, may need a referral reply
  out_of_office  → deferred by the existing OOO policy; shows proposed return date
  drafts_waiting → there is a draft the reviewer has not acted on yet
  interested     → positive reply, may need a human reply
  needs_reply    → inbound message, no draft queued

A lead that has never received an inbound message returns None (not in inbox).
"""

from __future__ import annotations

from datetime import datetime

#: Labels that mean the conversation is over and the lead is not interested.
_CLOSED_LABELS = frozenset({"not_interested", "opt_out", "hostile"})

#: Lead states that mean the same thing.
_CLOSED_STATES = frozenset({"NOT_INTERESTED", "OPTED_OUT", "BOUNCED"})

#: Lead states where the lead is waiting for a reply from us.
_NEEDS_REPLY_STATES = frozenset({"REPLIED", "IN_CONVERSATION"})

#: Lead states that have an active draft awaiting approval.
_APPROVAL_STATES = frozenset({"AWAITING_APPROVAL"})

#: These labels need a human reply drafted (or the system already drafted one).
_REPLY_NEEDED_LABELS = frozenset({"question", "objection", "not_now", "has_vendor", "other"})

#: Lead states that have never produced an inbound reply — excluded from the inbox.
_PRE_REPLY_STATES = frozenset(
    {
        "NEW",
        "ENRICHING",
        "RESEARCHED",
        "DRAFTED",
        "AWAITING_APPROVAL",
        "APPROVED",
        "SENT",
        "FOLLOWUP_SENT",
        "NO_RESPONSE",
        "SKIPPED",
        "FAILED",
    }
)


def folder(
    *,
    lead_state: str,
    latest_label: str | None,
    has_pending_draft: bool,
    snoozed_until: datetime | None,
    archived_at: datetime | None,
    now: datetime,
) -> str | None:
    """Return the inbox folder name for a thread, or None if not in the inbox.

    Args:
        lead_state: The current ``leads.state`` value.
        latest_label: The ``classifications.label`` of the most recent inbound
            message, or None if there are none.
        has_pending_draft: True when the lead has a draft in AWAITING_APPROVAL
            that the reviewer has not yet acted on.
        snoozed_until: When the reviewer asked to be reminded, if set.
        archived_at: When the reviewer dismissed this thread, if set.
        now: The current time, injected so this function is testable.

    Returns:
        A folder name string, or None when the thread is not in the inbox.
    """
    if archived_at is not None:
        return "archived"

    if snoozed_until is not None and snoozed_until > now:
        return "snoozed"

    if lead_state == "MEETING_BOOKED":
        return "meeting_booked"

    if lead_state in _CLOSED_STATES:
        return "not_interested"

    label = latest_label or ""

    if label in _CLOSED_LABELS:
        return "not_interested"

    if has_pending_draft:
        return "drafts_waiting"

    if label == "wrong_person_referral":
        return "wrong_person"

    if label == "out_of_office":
        return "out_of_office"

    if label == "interested":
        return "interested"

    if lead_state in _NEEDS_REPLY_STATES or label in _REPLY_NEEDED_LABELS:
        return "needs_reply"

    if lead_state in _PRE_REPLY_STATES:
        return None

    # Catch-all: a state we did not anticipate still surfaces rather than disappears.
    return "needs_reply"
