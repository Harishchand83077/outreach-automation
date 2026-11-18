"""A booking link must book something (F1, slice S10.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.booking import WEBHOOK_PATH, is_the_booking_webhook, opens_own_calendar
from app.domain.kinds import CandidateProfile
from app.domain.persona import Persona

OWN = [
    "https://calendar.google.com/calendar/u/0/r?pli=1",
    "https://calendar.google.com/calendar/u/1/r",
    "https://calendar.google.com/calendar/r",
    "https://outlook.live.com/calendar/0/view/month",
    "https://outlook.office.com/calendar/view/workweek",
]
REAL = [
    "https://cal.com/asha/20min",
    "https://calendly.com/asha/intro",
    "https://calendar.app.google/abc123",
    "https://zcal.co/asha",
    "https://arkvane.in/book",
]


@pytest.mark.parametrize("url", OWN)
def test_a_readers_own_calendar_is_refused_at_save(url):
    """The live campaign had exactly this, and every earlier check only asked if it was set."""
    with pytest.raises(ValidationError) as caught:
        Persona(sender_name="Asha", booking_url=url)

    assert "books nothing" in str(caught.value)


@pytest.mark.parametrize("url", REAL)
def test_a_real_scheduling_page_is_accepted(url):
    assert Persona(sender_name="Asha", booking_url=url).booking_url is not None


def test_no_booking_url_is_still_allowed():
    """`cta_text` alone has been valid since Phase 2; this must not change that."""
    assert Persona(sender_name="Asha", cta_text="Worth a call?").booking_url is None


@pytest.mark.parametrize("url", OWN)
def test_the_shared_helper_agrees(url):
    """The gate and the validator read one list, so they cannot drift apart."""
    assert opens_own_calendar(url) is True


@pytest.mark.parametrize("url", REAL)
def test_the_shared_helper_passes_real_pages(url):
    assert opens_own_calendar(url) is False


# --------------------------------------------------------------- the webhook, pasted as a link
#
# **Written because it happened.** The owner generated a booking webhook secret, pasted the URL
# into a `job_hunt` campaign's `booking_url`, and the drafter put it in an email that went out as
# "Please pick a time that works for you". Two separate failures in one field:
#
# 1. The URL *is* the shared secret that authenticates bookings into the workspace. Every
#    recipient, and every mail provider on the way, now holds it. It is the only thing standing
#    between a stranger and writing `MEETING_BOOKED` onto a lead.
# 2. It books nothing. `/hooks/booking/{token}` is POST-only, so a recipient clicking it gets
#    405 Method Not Allowed -- the same class of defect as the reader's-own-calendar link above,
#    and caught by none of the checks written for that one.

WEBHOOK = [
    "https://api.arkvane.in/hooks/booking/REDACTED.REDACTED",
    "https://api.example.com/hooks/booking/abc.def",
    "HTTPS://API.EXAMPLE.COM/HOOKS/BOOKING/abc.def",
]


@pytest.mark.parametrize("url", WEBHOOK)
def test_the_booking_webhook_is_refused_as_a_link(url):
    with pytest.raises(ValidationError) as caught:
        Persona(sender_name="Asha", booking_url=url)

    assert "secret" in str(caught.value)


@pytest.mark.parametrize("url", WEBHOOK)
def test_a_candidate_profile_refuses_it_too(url):
    """The campaign this happened to was `job_hunt`, whose persona is a `CandidateProfile`."""
    with pytest.raises(ValidationError) as caught:
        CandidateProfile(full_name="Asha", booking_url=url)

    assert "secret" in str(caught.value)


@pytest.mark.parametrize("url", OWN)
def test_a_candidate_profile_refuses_the_readers_own_calendar(url):
    """`CandidateProfile` had no validator at all, so this was refused only when `as_persona()`
    compiled it at drafting time -- a `ValidationError` inside a worker job, which reads as a
    crash rather than as a field to correct."""
    with pytest.raises(ValidationError) as caught:
        CandidateProfile(full_name="Asha", booking_url=url)

    assert "books nothing" in str(caught.value)


@pytest.mark.parametrize("url", REAL)
def test_a_candidate_profile_accepts_a_real_scheduling_page(url):
    assert CandidateProfile(full_name="Asha", booking_url=url).booking_url is not None


@pytest.mark.parametrize("url", WEBHOOK)
def test_the_shared_helper_agrees_about_the_webhook(url):
    assert is_the_booking_webhook(url) is True


@pytest.mark.parametrize("url", REAL + OWN)
def test_the_shared_helper_passes_everything_else(url):
    assert is_the_booking_webhook(url) is False


def test_the_router_and_the_domain_agree_on_the_path():
    """The domain cannot import the router -- `app/domain/` depends on nothing -- so the two
    constants are kept honest by a test rather than by an import. If the webhook ever moves,
    this fails and the refusal is updated with it instead of silently passing the new shape."""
    from app.api.routers.booking import PATH

    assert PATH.startswith(WEBHOOK_PATH)
