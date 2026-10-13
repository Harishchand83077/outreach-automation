r"""The one gate every outbound email passes through (hard rule 1, §9.6, issue B1).

This file replaces `tests/test_send_guard.py`, which tested the prototype's `email_utils`. The
prototype module is deleted; the guarantee is not, so it is re-proved here against
`app/services/mail/smtp.py`.

The original failure is worth remembering, because it is why the guard is shaped like this.
`DRY_RUN_EMAILS=0` was the documented value for "send real emails", and `bool("0")` is `True` in
Python — so following the documentation silently disabled every send while the pipeline went on
marking leads `EMAIL_SENT`. The operator would believe a campaign had gone out when nothing had
left the building.

`SEND_MODE` replaces it with three named states and no boolean coercion anywhere. The check lives
in exactly one function; anything else reaching SMTP is a bug, and the last test here asserts
that structurally rather than trusting it.
"""

from __future__ import annotations

import pytest

from app.core.settings import Settings
from app.services.mail.smtp import (
    SmtpOutcome,
    SmtpSender,
    allowlist_permits,
    effective_send_mode,
)
from tests.helpers import executable_source


def settings_with(**kwargs) -> Settings:
    return Settings(**kwargs)


class TestTheDefaultIsSilence:
    def test_disabled_is_the_default(self):
        """Nothing is ever sent because somebody forgot to configure a mode."""
        assert Settings().SEND_MODE == "disabled"

    @pytest.mark.parametrize(
        "recipient",
        ["anyone@example.com", "owner@example.com", "", "ADMIN@EXAMPLE.COM"],
    )
    def test_disabled_blocks_every_recipient(self, recipient):
        assert not allowlist_permits(recipient, settings_with(SEND_MODE="disabled"))

    def test_disabled_blocks_even_an_allowlisted_address(self):
        """`disabled` means disabled. The allowlist is not an exemption from it."""
        config = settings_with(SEND_MODE="disabled", SEND_ALLOWLIST="me@example.com")
        assert not allowlist_permits("me@example.com", config)


class TestAllowlistMode:
    def test_a_listed_address_is_permitted(self):
        config = settings_with(SEND_MODE="allowlist", SEND_ALLOWLIST="me@example.com")
        assert allowlist_permits("me@example.com", config)

    def test_an_unlisted_address_is_refused(self):
        config = settings_with(SEND_MODE="allowlist", SEND_ALLOWLIST="me@example.com")
        assert not allowlist_permits("someone.else@example.com", config)

    def test_matching_ignores_case_and_padding(self):
        """A rehearsal must not send to a stranger because of a stray space in an env var."""
        config = settings_with(
            SEND_MODE="allowlist", SEND_ALLOWLIST=" Me@Example.com , b@example.com "
        )
        assert allowlist_permits("me@example.com", config)
        assert allowlist_permits("B@EXAMPLE.COM", config)

    def test_an_empty_allowlist_permits_nobody(self):
        """Not 'everybody'. An unset list is the safe direction of the two."""
        config = settings_with(SEND_MODE="allowlist", SEND_ALLOWLIST="")
        assert not allowlist_permits("me@example.com", config)

    def test_a_substring_is_not_a_match(self):
        """`me@example.com` must not permit `me@example.com.evil.test`."""
        config = settings_with(SEND_MODE="allowlist", SEND_ALLOWLIST="me@example.com")
        assert not allowlist_permits("me@example.com.evil.test", config)
        assert not allowlist_permits("notme@example.com", config)


class TestLiveMode:
    def test_live_permits_any_recipient(self):
        """Only the owner sets this, only in production."""
        assert allowlist_permits("stranger@example.com", settings_with(SEND_MODE="live"))


class TestTheSenderRefusesBeforeConnecting:
    @pytest.mark.asyncio
    async def test_a_blocked_recipient_never_reaches_smtp(self):
        """The refusal happens before any socket is opened, so a fake server sees nothing."""

        class Built:
            message_id = "<x@outreach.example>"
            subject = "hello"
            raw = b"raw"

        sender = SmtpSender(settings_with(SEND_MODE="disabled"))
        report = await sender.send(Built(), to_email="stranger@example.com")

        assert report.outcome is SmtpOutcome.BLOCKED
        assert not report.sent
        # The refusal has to say *why*, and name the effective mode. It no longer says
        # "SEND_MODE" literally, because since ADR-010 the mode may come from the environment
        # or from the org's own setting, and a message naming the wrong source is worse than
        # one naming neither.
        assert "send mode" in (report.detail or "")
        assert "disabled" in (report.detail or "")

    @pytest.mark.asyncio
    async def test_a_blocked_send_is_not_an_error(self):
        """A blocked send is a policy decision, so it must not pause the mailbox."""

        class Built:
            message_id = "<x@outreach.example>"
            subject = "hello"
            raw = b"raw"

        report = await SmtpSender(settings_with(SEND_MODE="disabled")).send(
            Built(), to_email="stranger@example.com"
        )
        assert not report.should_pause_mailbox


class TestTheGuardIsInOnePlace:
    def test_only_the_smtp_adapter_decides_on_send_mode(self):
        """Hard rule 1: the check lives in exactly one function.

        A second copy is how the two drift apart, and the drift is only ever discovered by
        sending something that should not have been sent.

        What counts as a second copy is a *decision* — comparing the mode — not a mention.
        Logging the active mode at startup is useful and harmless; so is naming it in a comment
        that explains why a lead was parked.
        """
        import pathlib
        import re

        allowed = {
            "app/core/settings.py",  # where the value is defined
            "app/services/mail/smtp.py",  # where it is enforced
            "app/scripts/check_env.py",  # prints names only, decides nothing
        }
        decides = re.compile(r"SEND_MODE\s*(==|!=|in|not in)")

        offenders = []
        for path in pathlib.Path("app").rglob("*.py"):
            if path.as_posix() in allowed:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if decides.search(code):
                    offenders.append(f"{path.as_posix()}:{number}")
        assert offenders == [], f"SEND_MODE is decided on outside the one gate: {offenders}"

    def test_the_send_loop_does_not_reimplement_the_check(self):
        import app.worker.send_loop as module

        source = executable_source(module)
        assert "SEND_ALLOWLIST" not in source


class TestTheOrgOverride:
    """ADR-010: the org's own send mode, when it has one, is what applies.

    The decision still resolves in one place — `effective_send_mode` and `allowlist_permits`
    live in the same module as the gate, and the structural test above still passes. What
    changed is where the *value* comes from, not where it is decided.

    Raising the mode is gated at the API, not here, which is why these tests are only about
    resolution. A guard that answers differently depending on who is asking is a guard nobody
    can reason about.
    """

    def test_no_override_uses_the_environment(self):
        """The migration added a nullable column, so every existing org lands here unchanged."""
        assert effective_send_mode(settings_with(SEND_MODE="allowlist"), None) == "allowlist"

    def test_an_override_wins_over_the_environment(self):
        assert effective_send_mode(settings_with(SEND_MODE="live"), "disabled") == "disabled"
        assert effective_send_mode(settings_with(SEND_MODE="disabled"), "live") == "live"

    def test_an_unknown_override_fails_closed(self):
        """Unreachable — the column has a check constraint — which is why it must fail closed."""
        for rubbish in ("LIVE ", "yes", "", "enabled", "true"):
            assert effective_send_mode(settings_with(SEND_MODE="live"), rubbish) == "disabled"

    def test_disabling_blocks_an_allowlisted_recipient(self):
        """The kill switch has to beat the allowlist, or it is not a kill switch."""
        config = settings_with(SEND_MODE="live", SEND_ALLOWLIST="me@example.com")

        assert not allowlist_permits("me@example.com", config, override="disabled")

    def test_restricting_to_the_allowlist_still_checks_it(self):
        config = settings_with(SEND_MODE="live", SEND_ALLOWLIST="me@example.com")

        assert allowlist_permits("me@example.com", config, override="allowlist")
        assert not allowlist_permits("stranger@example.com", config, override="allowlist")

    def test_an_empty_recipient_is_refused_whatever_the_mode(self):
        assert not allowlist_permits("", settings_with(SEND_MODE="live"), override="live")

    @pytest.mark.asyncio
    async def test_the_sender_honours_the_override_before_connecting(self):
        """End of the path: an override of `disabled` must stop a send that the environment
        would have permitted, without a socket being opened."""

        class Built:
            message_id = "<x@outreach.example>"
            subject = "hello"
            raw = b"raw"

        sender = SmtpSender(settings_with(SEND_MODE="live", SMTP_HOST="127.0.0.1", SMTP_PORT=1))
        report = await sender.send(Built(), to_email="anyone@example.com", send_mode="disabled")

        assert report.outcome is SmtpOutcome.BLOCKED
        assert "disabled" in (report.detail or "")
