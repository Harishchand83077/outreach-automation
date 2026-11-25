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


#: This system's own booking webhook path. The URL under it carries the workspace's shared
#: secret, and `tests/unit/test_booking_url.py` asserts that the router still mounts it here --
#: `app/domain/` imports nothing from `app/api/`, so the two are kept honest by a test.
WEBHOOK_PATH = "/hooks/booking/"


def opens_own_calendar(url: str) -> bool:
    lowered = (url or "").strip().lower()
    return any(shape in lowered for shape in OWN_CALENDAR_SHAPES)


def contains_booking_webhook(text: str) -> bool:
    """Whether an email's text carries this system's inbound booking webhook URL.

    That URL embeds the workspace's secret: in an email it hands every recipient the credential
    that can mark a lead booked (ADR-074). Checked at approval, so a persona saved before the
    save-time check existed cannot still put it in front of a recipient.
    """
    return WEBHOOK_PATH in (text or "").lower()


def is_the_booking_webhook(url: str) -> bool:
    """Whether this is the *inbound* webhook URL rather than a page a person can book on.

    It is the worst thing this field can hold. The URL is the secret a booking provider POSTs
    to, so a draft containing it hands every recipient -- and every mail server on the way --
    the one credential that can write `MEETING_BOOKED` onto a lead. It also books nothing: the
    endpoint is POST-only, so a recipient who clicks it gets 405.
    """
    return WEBHOOK_PATH in (url or "").strip().lower()


def why_it_books_nothing(url: str) -> str | None:
    """The reason this link cannot be a campaign's booking link, or `None` if it can.

    Both persona schemas call this, so `outreach` and `job_hunt` cannot disagree about what a
    usable link is -- they did, which is how a `job_hunt` campaign stored a link that `Persona`
    would have refused and only found out when the worker compiled it.
    """
    if opens_own_calendar(url):
        return (
            "this link opens the reader's own calendar and books nothing; use a scheduling "
            "page that shows your availability"
        )
    if is_the_booking_webhook(url):
        return (
            "this is the inbound booking webhook, and its URL is a secret: in an email it "
            "hands every recipient the credential that can mark a lead booked. It also books "
            "nothing, because that endpoint only answers POST. Use the public scheduling page "
            "of whichever tool posts to it -- your Cal.com or Calendly link"
        )
    return None
