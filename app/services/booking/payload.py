"""Reading a booking payload without assuming whose it is (F15, ADR-045).

A pure module: no database, no network, no clock beyond what is handed in.

**Every booking tool names these four things differently**, and the v3 plan's single acceptance
criterion -- a signed webhook sets `MEETING_BOOKED` -- would be met by a parser that worked for
exactly one of them. So the fields are looked for under the names the common tools use, nested or
flat, and **a payload we cannot read raises rather than returning an empty booking**. That is the
job-post intake's expensive lesson repeated deliberately: an unknown source with a plausible
payload once returned *zero postings*, which is indistinguishable from an empty board, and here
the equivalent would be "nobody booked a meeting" about a meeting somebody is going to attend.

What is **not** read is everything else. A booking payload carries the invitee's own answers to
the booking form, the provider's internal ids and often the organiser's calendar. None of it is
used, so none of it is stored -- a column nobody reads is a column nobody is protecting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.db.models import BOOKING_EVENT_KINDS

#: What a provider's event name has to contain to mean each kind. Substring rather than exact,
#: because the same tool writes `BOOKING_CANCELLED`, `invitee.canceled` and `booking.cancelled`
#: across its versions -- and `cancelled` is checked before `booked`, since
#: `BOOKING_CANCELLED` contains neither spelling of the other but a future name might.
_KIND_HINTS: tuple[tuple[str, str], ...] = (
    ("cancel", "cancelled"),
    ("reschedul", "rescheduled"),
    ("book", "booked"),
    ("creat", "booked"),
    ("schedul", "booked"),
)

MAX_BODY_BYTES = 256 * 1024


class BookingUnreadable(Exception):
    """The payload is not a booking, with a sentence naming what was missing."""


@dataclass(frozen=True)
class Booking:
    external_id: str
    kind: str
    provider: str
    invitee_email: str | None
    invitee_name: str | None
    scheduled_at: datetime | None


def _first(data: Any, *paths: str) -> Any:
    """The first present value at any of these dotted paths. Lists take their first element."""
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if isinstance(current, list) and current:
                current = current[0]
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if isinstance(current, list) and current:
            current = current[0]
        if current not in (None, "", [], {}):
            return current
    return None


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned[:limit] or None


def _moment(value: Any) -> datetime | None:
    """A start time, from ISO 8601 or epoch milliseconds.

    Lever's feed taught this one: the same field is ISO in two tools and epoch milliseconds in a
    third, and a parser written against an assumed shape fails for exactly one source -- which
    reads like a bad configuration rather than a bad assumption.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        seconds = float(value)
        # Milliseconds if the number is far past any plausible second-based timestamp.
        if seconds > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def kind_of(event_name: str) -> str:
    """Which of `BOOKING_EVENT_KINDS` an event name means. Defaults to `booked`.

    Defaulting rather than refusing, deliberately: a tool that invents a new name for "a meeting
    was booked" should not have its meetings lost. The cost is that an unrecognised *cancellation*
    reads as a booking -- which is why `cancel` is matched first and why a cancellation does not
    move the lead anyway (ADR-045).
    """
    lowered = (event_name or "").lower()
    for hint, kind in _KIND_HINTS:
        if hint in lowered:
            return kind
    return BOOKING_EVENT_KINDS[0]


def read(data: Any, *, provider_hint: str = "") -> Booking:
    """One booking, from whatever shape the tool sent. Raises `BookingUnreadable`."""
    if not isinstance(data, dict):
        raise BookingUnreadable("the webhook body is not a JSON object")

    external_id = _text(
        _first(
            data,
            "payload.uid",
            "payload.id",
            "payload.booking.uid",
            "payload.bookingId",
            "data.uuid",
            "data.uri",
            "data.id",
            "uid",
            "id",
            "event.uid",
            "booking.uid",
        ),
        200,
    )
    if not external_id:
        raise BookingUnreadable(
            "the webhook body carries no booking id, so a retry could not be told from a second "
            "meeting. Check that the webhook sends the full booking payload."
        )

    email = _text(
        _first(
            data,
            "payload.attendees.email",
            "payload.attendees.0.email",
            "payload.invitee.email",
            "payload.responses.email.value",
            "payload.email",
            "data.email",
            "data.invitee.email",
            "attendees.email",
            "invitee.email",
            "email",
        ),
        320,
    )
    name = _text(
        _first(
            data,
            "payload.attendees.name",
            "payload.invitee.name",
            "payload.responses.name.value",
            "payload.name",
            "data.name",
            "data.invitee.name",
            "attendees.name",
            "invitee.name",
            "name",
        ),
        300,
    )
    event_name = _text(
        _first(data, "triggerEvent", "event", "event_type", "type", "payload.type"), 120
    )
    provider = _text(_first(data, "provider", "source"), 40) or provider_hint or "unknown"
    scheduled = _moment(
        _first(
            data,
            "payload.startTime",
            "payload.start_time",
            "payload.booking.startTime",
            "data.scheduled_event.start_time",
            "data.start_time",
            "startTime",
            "start_time",
        )
    )

    return Booking(
        external_id=external_id,
        kind=kind_of(event_name or ""),
        provider=provider.lower(),
        invitee_email=(email or "").strip().lower() or None,
        invitee_name=name,
        scheduled_at=scheduled,
    )
