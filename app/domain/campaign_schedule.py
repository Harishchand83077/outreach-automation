"""When a campaign starts and when it stops, decided without a database.

A campaign could be set up but not started, or started and never stopped. Both had to be done by
hand, at the moment they were due — which means somebody remembering on a Monday morning, and
means a campaign that should have stopped on Friday carrying on through the weekend because
nobody was looking.

**The decision is to move `status`, not to add a time window to every query that reads it.**
Three places ask whether a campaign is running -- the send loop's candidate query, the lifecycle
pass, and `repo.due_followups` -- and adding `starts_at <= now < ends_at` to each would be three
copies of one rule, which is precisely the shape of defect this project produces over and over.
Instead one writer moves the status, and every reader goes on asking the question it already
asked.

The cost is up to a minute of lag, because the `Scheduler` runs once a minute. For something
whose unit is a calendar day, that is not a cost.

This module is pure: it takes a status, a window and a clock, and returns the status that should
hold. No I/O, so every edge of it is unit-testable without a database (`app/domain/` has no I/O).
"""

from __future__ import annotations

from datetime import datetime

#: Statuses this module is allowed to move a campaign *out of*. Deliberately short.
#:
#: `paused` is absent, and that is the important one: pausing is a human saying stop, and a
#: schedule must never override it. A paused campaign whose start time arrives stays paused.
#: `archived` and `completed` are absent because both are endings -- a campaign that has
#: finished does not restart because somebody left a `starts_at` on it.
MOVEABLE = frozenset({"draft", "scheduled", "running"})


def scheduled_status(
    *,
    status: str,
    starts_at: datetime | None,
    ends_at: datetime | None,
    now: datetime,
) -> str:
    """The status this campaign should have, given its window and the time.

    Returns `status` unchanged when nothing should move, so a caller can compare and write only
    when it differs -- which keeps the audit trail free of a row every minute saying nothing
    happened.

    The rules, in the order they are decided:

    1. A campaign nobody scheduled is never touched. No window means the old behaviour exactly,
       which is what lets this ship without changing a single existing campaign.
    2. A status outside `MOVEABLE` is never touched -- see the note there about `paused`.
    3. Past `ends_at` is `completed`. Checked **before** the start, so a window already in the
       past completes rather than briefly running.
    4. Before `starts_at` is `scheduled`; a campaign waiting for Monday says so rather than
       sitting in `draft`, which reads as "not finished being set up".
    5. Inside the window is `running`.
    """
    if starts_at is None and ends_at is None:
        return status
    if status not in MOVEABLE:
        return status

    if ends_at is not None and now >= ends_at:
        return "completed"
    if starts_at is not None and now < starts_at:
        return "scheduled"
    return "running"


def schedule_note(
    *,
    status: str,
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> str | None:
    """A sentence for the dashboard, or `None` when there is no schedule to describe.

    Dates only, never times: the window is a calendar decision and showing `09:00:00+00:00`
    invites somebody to reason about a timezone that is not the one their sending window is in.
    """
    if starts_at is None and ends_at is None:
        return None
    start = starts_at.date().isoformat() if starts_at else None
    end = ends_at.date().isoformat() if ends_at else None

    if status == "completed" and end:
        return f"Finished on {end}."
    if status == "scheduled" and start:
        return f"Starts on {start}." + (f" Ends on {end}." if end else "")
    if start and end:
        return f"Running from {start} until {end}."
    if end:
        return f"Ends on {end}."
    return f"Started on {start}."
