"""Configuration that refuses to start rather than pretending (issues B1, B3, B6).

This replaces `tests/test_config.py`, which tested the prototype's `config/config.py`. That
module is deleted; the guarantees are re-proved here against `app/core/settings.py`.

Two prototype failures shaped this module, and both were silent:

- **`config/config.py` loaded `.env.example` as defaults and copied its placeholders into the
  environment**, so `your_groq_api_key_here` passed validation and the dashboard cheerfully
  reported the system configured. The operator had no way to tell a configured system from an
  unconfigured one.
- **`DRY_RUN_EMAILS=0` meant *dry run*,** because `bool("0")` is `True`. The documented value for
  "send real emails" silently disabled every send.

So: placeholders fail startup, and the old flag is rejected outright rather than ignored — an
ignored flag in a live environment is exactly how somebody believes a campaign went out.
"""

from __future__ import annotations

import pytest

from app.core.settings import ConfigError, Settings, looks_like_placeholder


def startup_problems(settings: Settings) -> str:
    """The message `validate_required` raises, or an empty string if it does not."""
    try:
        settings.validate_required()
    except ConfigError as exc:
        return str(exc)
    return ""


class TestPlaceholderDetection:
    @pytest.mark.parametrize(
        "value",
        [
            "your_groq_api_key_here",
            "YOUR_API_KEY",
            "changeme",
            "placeholder",
            "xxx",
            "<your key>",
        ],
    )
    def test_obvious_placeholders_are_recognised(self, value):
        assert looks_like_placeholder(value)

    @pytest.mark.parametrize(
        "value",
        ["gsk_realLookingKey123456", "a-real-password", "smtp.gmail.com"],
    )
    def test_real_values_are_not_flagged(self, value):
        """A false positive here refuses to start a correctly configured system."""
        assert not looks_like_placeholder(value)

    def test_an_empty_value_is_not_a_placeholder(self):
        """Empty and placeholder are different problems and read differently in an error."""
        assert not looks_like_placeholder("")


class TestTheOldFlagIsRejected:
    def test_dry_run_emails_in_the_environment_stops_startup(self, monkeypatch):
        """Ignoring it would be worse: someone would set it and believe it had an effect."""
        monkeypatch.setenv("DRY_RUN_EMAILS", "0")
        assert "DRY_RUN_EMAILS" in startup_problems(Settings())

    def test_the_message_names_the_replacement(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN_EMAILS", "1")
        assert "SEND_MODE" in startup_problems(Settings())

    def test_a_clean_environment_does_not_complain_about_it(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN_EMAILS", raising=False)
        assert "DRY_RUN_EMAILS" not in startup_problems(Settings())


class TestSendModeIsNamedNotBoolean:
    def test_the_three_modes_are_the_only_ones_accepted(self):
        for mode in ("disabled", "allowlist", "live"):
            assert Settings(SEND_MODE=mode).SEND_MODE == mode

    def test_an_unknown_mode_is_refused_at_construction(self):
        """No coercion anywhere: this is what B1 was, and a typo must not become 'live'."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(SEND_MODE="yes")

    @pytest.mark.parametrize("value", ["0", "1", "true", "false"])
    def test_a_boolean_looking_value_is_not_a_mode(self, value):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(SEND_MODE=value)


class TestConfigurationIsReportedHonestly:
    def test_a_placeholder_key_is_not_configured(self):
        """The prototype's dashboard said 'configured' for `your_groq_api_key_here`."""
        assert not Settings(GROQ_API_KEY="your_groq_api_key_here").groq_configured

    def test_an_empty_key_is_not_configured(self):
        assert not Settings(GROQ_API_KEY="").groq_configured

    def test_a_real_looking_key_is_configured(self):
        assert Settings(GROQ_API_KEY="gsk_realLookingKey123456").groq_configured


class TestNothingLoadsTheTemplate:
    def test_the_example_file_is_never_loaded_as_defaults(self, monkeypatch, tmp_path):
        """`config/config.py` read `.env.example` and copied its placeholders into os.environ.

        Behavioural rather than textual: a value that exists only in the committed template must
        not appear in a freshly constructed Settings.
        """
        import os

        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        settings = Settings(_env_file=None)
        assert not looks_like_placeholder(settings.GROQ_API_KEY or "x")
        assert "GROQ_API_KEY" not in os.environ or os.environ.get("GROQ_API_KEY") != (
            "your_groq_api_key_here"
        )

    def test_the_template_is_not_in_the_env_file_chain(self):
        """Precise rather than textual: the template may be *named* in an error message —
        "see env.example for the shape" is helpful — but it must never be a source of values.
        """
        sources = Settings.model_config.get("env_file")
        names = [sources] if isinstance(sources, str) else list(sources or [])
        assert all("example" not in str(name) for name in names), (
            f"the committed template is being loaded as defaults: {names}"
        )
