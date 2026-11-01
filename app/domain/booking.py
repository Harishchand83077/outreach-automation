"""Whether a booking link actually books anything.

Shared by `Persona` validation and by the go-live gate, so the two cannot disagree about what a
usable link is.
"""

from __future__ import annotations

#: Link shapes that open the *reader's* calendar rather than the sender's availability.
OWN_CALENDAR_SHAPES: tuple[str, ...] = (
    "calendar.google.com/calendar/u/",
    "calendar.google.com/calendar/r",
    "outlook.live.com/calendar/0/view",
    "outlook.office.com/calendar/view",
)


def opens_own_calendar(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return any(shape in lowered for shape in OWN_CALENDAR_SHAPES)
