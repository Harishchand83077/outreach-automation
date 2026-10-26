"""The seven-day countdown on a Gmail connection.

Pure arithmetic against an injected clock, which is the point of `expiry.grant_status` being a
function rather than a method on a row: the dashboard's sentence and the alert's threshold are
the same calculation, and a second copy of it is how this project's signature defect starts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.settings import Settings
from app.db.models import MailCredential
from app.services.mail.expiry import WARN_AT_DAYS, grant_status

GRANTED = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def settings_with(**kw: object) -> Settings:
    base: dict[str, object] = {"_env_file": None, "GROQ_API_KEY": "gsk_test"}
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


def credential(**kw: object) -> MailCredential:
    fields: dict[str, object] = {
        "kind": "google_oauth",
        "account_email": "asha@example.com",
        "secret": b"ciphertext",
        "granted_at": GRANTED,
    }
    return MailCredential(**{**fields, **kw})  # type: ignore[arg-type]


class TestTheCountdown:
    @pytest.mark.parametrize(
        ("hours_since_grant", "expected"),
        [
            (0, 7),
            (24, 6),
            (24 * 5, 2),
            (24 * 6, 1),
            # Eleven hours left still reports 1. Rounding down would say 0 for something that
            # works, and a countdown that reaches zero while the thing still works is one
            # nobody believes the next time it says zero.
            (24 * 6 + 13, 1),
            (24 * 7, 0),
            (24 * 9, 0),
        ],
    )
    def test_days_left_rounds_up(self, hours_since_grant, expected):
        status = grant_status(
            credential(), now=GRANTED + timedelta(hours=hours_since_grant), settings=settings_with()
        )
        assert status.days_left == expected

    def test_the_expiry_is_seven_days_after_the_grant(self):
        status = grant_status(credential(), now=GRANTED, settings=settings_with())
        assert status.expires_at == GRANTED + timedelta(days=7)

    def test_created_at_is_the_fallback_for_a_row_written_before_the_column_existed(self):
        row = credential(granted_at=None)
        row.created_at = GRANTED
        assert grant_status(row, now=GRANTED, settings=settings_with()).days_left == 7

    def test_a_reconnection_restarts_it(self):
        """`granted_at` moves on consent, which is the whole reason it is not `created_at`."""
        row = credential(granted_at=GRANTED)
        row.created_at = GRANTED - timedelta(days=30)
        assert grant_status(row, now=GRANTED, settings=settings_with()).days_left == 7


class TestWhenThereIsNothingToCountDown:
    def test_an_app_password_does_not_expire(self):
        row = credential(kind="app_password")
        status = grant_status(row, now=GRANTED, settings=settings_with())
        assert status.expires_at is None
        assert status.days_left is None
        assert status.notice is None

    def test_no_credential_at_all(self):
        assert grant_status(None, now=GRANTED, settings=settings_with()).days_left is None

    def test_a_published_client_turns_the_countdown_off(self):
        """ADR-018's exit. Publishing the client is a configuration change, not a code one."""
        status = grant_status(
            credential(), now=GRANTED, settings=settings_with(GOOGLE_GRANT_DAYS=0)
        )
        assert status.expires_at is None
        assert status.notice is None


class TestWhatThePersonIsTold:
    def test_nothing_is_said_while_there_is_plenty_of_time(self):
        """Copy that cries wolf for six days is copy nobody reads on the seventh."""
        status = grant_status(
            credential(), now=GRANTED + timedelta(days=2), settings=settings_with()
        )
        assert status.notice is None

    def test_the_day_before_says_so(self):
        status = grant_status(
            credential(), now=GRANTED + timedelta(days=6), settings=settings_with()
        )
        assert status.notice is not None
        assert "1 day" in status.notice
        assert not status.expired

    def test_an_expired_grant_says_it_has_expired(self):
        status = grant_status(
            credential(), now=GRANTED + timedelta(days=8), settings=settings_with()
        )
        assert status.expired
        assert status.notice is not None
        assert "expired" in status.notice

    def test_the_thresholds_are_the_last_two_days(self):
        """Locked deliberately: the owner asked for day 6 and day 7 of a seven-day life."""
        assert WARN_AT_DAYS == (1, 0)
