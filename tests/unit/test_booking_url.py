"""A booking link must book something (F1, slice S10.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.booking import opens_own_calendar
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
