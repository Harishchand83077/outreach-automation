"""The rules that make a shared deployment safe, tested without a database.

Three separate things are checked here, and the first is the one that matters most: a secret
that reaches a log, a route or a repr is a secret that eventually reaches somebody who should
not have it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.core.settings import Settings
from app.domain.campaign_schedule import schedule_note, scheduled_status
from app.services.credentials import CredentialError, Sealed, seal, unseal
from app.services.mail.google_oauth import SCOPES, authorize_url, xoauth2_string
from app.services.signup import SignupRefused, _org_name_for, may_sign_up

KEY = Fernet.generate_key().decode("ascii")


def settings_with(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "SMTP_EMAIL": "asha@outreach.example",
        "SMTP_PASSWORD": "not-a-real-password",
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


class TestCredentialsNeverLeak:
    def test_a_sealed_value_prints_nothing(self) -> None:
        """A dataclass would print its ciphertext in every traceback and debugger frame."""
        sealed = seal("a-refresh-token", settings_with(CREDENTIAL_KEY=KEY))

        assert repr(sealed) == "Sealed(...)"
        assert "refresh" not in repr(sealed)

    def test_a_secret_round_trips(self) -> None:
        settings = settings_with(CREDENTIAL_KEY=KEY)

        assert unseal(seal("hunter2", settings).token, settings) == "hunter2"

    def test_the_stored_form_is_not_the_plaintext(self) -> None:
        sealed = seal("a-refresh-token", settings_with(CREDENTIAL_KEY=KEY))

        assert b"a-refresh-token" not in sealed.token

    def test_without_a_key_storing_is_refused_rather_than_stored_in_the_clear(self) -> None:
        """The failure that must never be silent. A refresh token in plain text in a database
        that is dumped nightly to S3 is a Gmail account in a backup."""
        with pytest.raises(CredentialError, match="CREDENTIAL_KEY"):
            seal("a-refresh-token", settings_with(CREDENTIAL_KEY=""))

    def test_a_value_written_under_another_key_says_so(self) -> None:
        """The fix is "reconnect the mailbox", and nothing about a generic decryption error
        says that."""
        written = seal("token", settings_with(CREDENTIAL_KEY=KEY))
        other = settings_with(CREDENTIAL_KEY=Fernet.generate_key().decode("ascii"))

        with pytest.raises(CredentialError, match="reconnected"):
            unseal(written.token, other)

    def test_an_empty_secret_is_refused(self) -> None:
        with pytest.raises(CredentialError):
            seal("", settings_with(CREDENTIAL_KEY=KEY))

    def test_sealed_is_frozen_so_a_token_cannot_be_swapped_in_place(self) -> None:
        sealed = Sealed(token=b"x")

        with pytest.raises(AttributeError):
            sealed.token = b"y"  # type: ignore[misc]


class TestWhoMaySignUp:
    def test_closed_is_the_default(self) -> None:
        """Deploying this must change nothing until somebody turns it on deliberately."""
        assert settings_with().SIGNUP_MODE == "closed"

    def test_closed_refuses_everybody(self) -> None:
        with pytest.raises(SignupRefused, match="sign-up is closed"):
            may_sign_up("stranger@example.com", settings_with())

    def test_open_admits_anybody(self) -> None:
        assert may_sign_up("stranger@example.com", settings_with(SIGNUP_MODE="open")) is None

    def test_an_allowlist_admits_its_own_domain(self) -> None:
        settings = settings_with(SIGNUP_MODE="allowlist", SIGNUP_ALLOWED_DOMAINS="arkvane.in")

        assert may_sign_up("asha@arkvane.in", settings) is None

    def test_an_allowlist_refuses_another_domain(self) -> None:
        settings = settings_with(SIGNUP_MODE="allowlist", SIGNUP_ALLOWED_DOMAINS="arkvane.in")

        with pytest.raises(SignupRefused):
            may_sign_up("someone@gmail.com", settings)

    def test_an_empty_allowlist_admits_nobody(self) -> None:
        """Fails closed. The safe reading of "allow these domains: none" is "allow nobody"."""
        with pytest.raises(SignupRefused):
            may_sign_up("asha@arkvane.in", settings_with(SIGNUP_MODE="allowlist"))

    def test_domain_matching_ignores_case(self) -> None:
        settings = settings_with(SIGNUP_MODE="allowlist", SIGNUP_ALLOWED_DOMAINS="Arkvane.IN")

        assert may_sign_up("ASHA@ARKVANE.IN", settings) is None

    def test_a_workspace_is_named_after_the_person(self) -> None:
        assert _org_name_for("asha.rao@arkvane.in") == "Asha Rao"
        assert _org_name_for(None) == "My workspace"


class TestTheCampaignClock:
    MONDAY = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    def test_a_campaign_with_no_schedule_is_never_touched(self) -> None:
        """What lets this ship without changing a single existing campaign."""
        for status in ("draft", "running", "paused", "completed", "archived"):
            assert (
                scheduled_status(status=status, starts_at=None, ends_at=None, now=self.MONDAY)
                == status
            )

    def test_before_the_start_it_is_scheduled(self) -> None:
        assert (
            scheduled_status(
                status="draft",
                starts_at=self.MONDAY + timedelta(days=1),
                ends_at=None,
                now=self.MONDAY,
            )
            == "scheduled"
        )

    def test_inside_the_window_it_runs(self) -> None:
        assert (
            scheduled_status(
                status="scheduled",
                starts_at=self.MONDAY - timedelta(hours=1),
                ends_at=self.MONDAY + timedelta(days=7),
                now=self.MONDAY,
            )
            == "running"
        )

    def test_past_the_end_it_completes(self) -> None:
        assert (
            scheduled_status(
                status="running",
                starts_at=self.MONDAY - timedelta(days=7),
                ends_at=self.MONDAY - timedelta(hours=1),
                now=self.MONDAY,
            )
            == "completed"
        )

    def test_a_window_entirely_in_the_past_completes_without_running_first(self) -> None:
        """The end is checked before the start, or a campaign scheduled last month would
        briefly start sending the moment somebody saved it."""
        assert (
            scheduled_status(
                status="draft",
                starts_at=self.MONDAY - timedelta(days=30),
                ends_at=self.MONDAY - timedelta(days=20),
                now=self.MONDAY,
            )
            == "completed"
        )

    def test_a_paused_campaign_is_never_started_by_its_schedule(self) -> None:
        """Pausing is a human saying stop. A schedule must never override it."""
        assert (
            scheduled_status(
                status="paused",
                starts_at=self.MONDAY - timedelta(days=1),
                ends_at=self.MONDAY + timedelta(days=7),
                now=self.MONDAY,
            )
            == "paused"
        )

    def test_a_finished_campaign_does_not_restart(self) -> None:
        for status in ("completed", "archived"):
            assert (
                scheduled_status(
                    status=status,
                    starts_at=self.MONDAY - timedelta(days=1),
                    ends_at=self.MONDAY + timedelta(days=7),
                    now=self.MONDAY,
                )
                == status
            )

    def test_the_note_is_dates_not_timestamps(self) -> None:
        note = schedule_note(
            status="scheduled", starts_at=self.MONDAY, ends_at=self.MONDAY + timedelta(days=5)
        )

        assert note is not None
        assert "2026-11-02" in note
        assert "+00:00" not in note

    def test_no_schedule_has_no_note(self) -> None:
        assert schedule_note(status="running", starts_at=None, ends_at=None) is None


class TestTheOAuthRequest:
    def test_it_asks_for_a_refresh_token_explicitly(self) -> None:
        """`access_type=offline` with `prompt=consent` is what actually returns a refresh
        token. Without the second, a *reconnect* comes back with an access token and nothing to
        renew it -- invisible for an hour, then permanent."""
        url = authorize_url(
            "a-state",
            settings_with(
                GOOGLE_CLIENT_ID="cid",
                GOOGLE_CLIENT_SECRET="secret",
                GOOGLE_REDIRECT_URI="https://api.example.com/cb",
            ),
        )

        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "state=a-state" in url

    def test_the_scope_covers_smtp_and_imap(self) -> None:
        """`https://mail.google.com/` is what XOAUTH2 against SMTP and IMAP needs; there is no
        narrower scope that authorises those protocols."""
        assert "https://mail.google.com/" in SCOPES

    def test_it_refuses_when_the_server_is_not_configured(self) -> None:
        from app.services.mail.google_oauth import OAuthError

        with pytest.raises(OAuthError, match="not configured"):
            authorize_url("a-state", settings_with())

    def test_the_sasl_string_has_the_shape_the_rfc_wants(self) -> None:
        """`user=<email>^Aauth=Bearer <token>^A^A`, unencoded -- the SMTP and IMAP clients
        base64 it themselves, and doing it here would double-encode."""
        assert xoauth2_string("asha@arkvane.in", "tok") == (
            "user=asha@arkvane.in\x01auth=Bearer tok\x01\x01"
        )
