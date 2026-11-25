"""The booking webhook URL is a secret and must not reach an email (ADR-074)."""

from app.domain.booking import contains_booking_webhook


def test_the_webhook_url_is_recognised_anywhere_in_the_text() -> None:
    text = "Pick a time: https://api.example.com/hooks/booking/abc.def thanks"

    assert contains_booking_webhook(text) is True


def test_case_does_not_hide_it() -> None:
    assert contains_booking_webhook("HTTPS://API.EXAMPLE.COM/HOOKS/BOOKING/x") is True


def test_a_real_booking_page_is_not_flagged() -> None:
    assert contains_booking_webhook("Book at https://cal.example.com/me/30min") is False
    assert contains_booking_webhook("") is False
