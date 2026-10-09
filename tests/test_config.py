"""Regression tests for B3, B6, B18 and the removal of DRY_RUN_EMAILS (B1).

The prototype loaded .env.example as a source of defaults and copied its placeholders into
os.environ, so validate() passed with `your_groq_api_key_here` and the API reported itself
correctly configured. Startup must now refuse a placeholder.
"""

import pytest
from pydantic import ValidationError

from config.config import ConfigError, Settings, looks_like_placeholder


class TestPlaceholderRejection:
    """B3 — a placeholder must never be accepted as a credential."""

    @pytest.mark.parametrize(
        "value",
        [
            "your_groq_api_key_here",
            "your_app_password",
            "your_email@gmail.com",
            "YOUR_API_KEY",
            "changeme",
            "<your-key>",
            "replace_me",
            "xxx",
            "TODO",
        ],
    )
    def test_known_placeholders_are_detected(self, value):
        assert looks_like_placeholder(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "gsk_realLookingKey123456",
            "sender@example.com",
            "a-genuine-app-password",
        ],
    )
    def test_real_values_are_not_flagged(self, value):
        assert looks_like_placeholder(value) is False

    def test_placeholder_api_key_fails_validation(self, settings_factory, valid_settings_kwargs):
        s = settings_factory(**{**valid_settings_kwargs, "GROQ_API_KEY": "your_groq_api_key_here"})
        with pytest.raises(ConfigError) as exc:
            s.validate_required()
        assert "GROQ_API_KEY" in str(exc.value)
        # The message must never echo the value itself.
        assert "your_groq_api_key_here" not in str(exc.value)

    def test_placeholder_password_fails_validation(self, settings_factory, valid_settings_kwargs):
        s = settings_factory(**{**valid_settings_kwargs, "SMTP_PASSWORD": "your_app_password"})
        with pytest.raises(ConfigError, match="SMTP_PASSWORD"):
            s.validate_required()

    def test_empty_required_value_fails_validation(self, settings_factory, valid_settings_kwargs):
        s = settings_factory(**{**valid_settings_kwargs, "SMTP_EMAIL": ""})
        with pytest.raises(ConfigError, match="SMTP_EMAIL"):
            s.validate_required()

    def test_all_three_missing_are_reported_together(self, settings_factory):
        s = settings_factory()
        with pytest.raises(ConfigError) as exc:
            s.validate_required()
        for name in ("GROQ_API_KEY", "SMTP_EMAIL", "SMTP_PASSWORD"):
            assert name in str(exc.value)

    def test_real_values_pass_validation(self, settings_factory, valid_settings_kwargs):
        settings_factory(**valid_settings_kwargs).validate_required()

    def test_is_configured_reports_honestly(self, settings_factory, valid_settings_kwargs):
        """/api/config/check used to report groq_configured=true for a placeholder."""
        placeholder = settings_factory(
            **{**valid_settings_kwargs, "GROQ_API_KEY": "your_groq_api_key_here"}
        )
        assert placeholder.groq_configured is False
        assert settings_factory(**valid_settings_kwargs).groq_configured is True


class TestEnvExampleIsNotLoaded:
    """B3 — .env.example must not be a source of values."""

    def test_defaults_are_empty_not_placeholders(self, settings_factory):
        s = settings_factory()
        assert s.GROQ_API_KEY == ""
        assert s.SMTP_EMAIL == ""
        assert s.SMTP_PASSWORD == ""

    def test_env_example_is_never_loaded_as_a_source_of_values(self):
        """Assert the mechanism, not the spelling: the file is still named in error messages."""
        import config.config as cfg
        from tests.helpers import executable_source

        assert "load_dotenv" not in executable_source(cfg)
        assert cfg.Settings.model_config["env_file"] == ".env"


class TestDeclaredSettings:
    """B6 — APPROVAL_MODE was monkeypatched onto the config object at runtime."""

    def test_approval_mode_is_declared(self, settings_factory):
        assert settings_factory().APPROVAL_MODE == "cli"

    def test_approval_mode_rejects_unknown_value(self, settings_factory):
        with pytest.raises(ValidationError):
            settings_factory(APPROVAL_MODE="whatever")

    def test_auto_approve_is_declared_and_defaults_off(self, settings_factory):
        assert settings_factory().AUTO_APPROVE is False

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0", False),
            ("", False),
            ("false", False),
            ("no", False),
            ("1", True),
            ("true", True),
            ("yes", True),
        ],
    )
    def test_auto_approve_parses_loosely(self, settings_factory, raw, expected):
        assert settings_factory(AUTO_APPROVE=raw).AUTO_APPROVE is expected


class TestNumericSettings:
    """B18 — int(os.getenv(...)) at class-body scope crashed at import on a bad value."""

    def test_bad_int_raises_a_validation_error_not_a_bare_valueerror(self, settings_factory):
        with pytest.raises(ValidationError):
            settings_factory(SMTP_PORT="not-a-port")

    def test_good_int_is_coerced(self, settings_factory):
        assert settings_factory(SMTP_PORT="465").SMTP_PORT == 465


class TestLegacyDryRunFlagIsRefused:
    """B1 — DRY_RUN_EMAILS=0 meant *dry run*, the opposite of what .env.example documented.

    The flag is deleted. If it is still set, startup must say so loudly rather than ignore it,
    because an operator who believes it controls sending would otherwise be silently wrong.
    """

    def test_dry_run_emails_in_env_is_rejected(self, clean_env, valid_settings_kwargs):
        clean_env.setenv("DRY_RUN_EMAILS", "0")
        s = Settings(_env_file=None, **valid_settings_kwargs)
        with pytest.raises(ConfigError) as exc:
            s.validate_required()
        assert "DRY_RUN_EMAILS" in str(exc.value)
        assert "SEND_MODE" in str(exc.value)
