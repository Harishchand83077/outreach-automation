"""Structured logging: the shape of a line, the ids on it, and what must never be in it.

The redaction tests are the ones that matter. Hard rule 2 says never log a secret, and the first
defence is not logging one — but secrets arrive in log lines by accident, inside the text of an
exception raised by a driver or an HTTP client that was handed a URL with a password in it. So
these tests pass a secret through the paths it would actually travel: a message, a format
argument, an `extra` field and an exception's text.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import (
    JsonFormatter,
    Redactor,
    TextFormatter,
    configure_logging,
    current_context,
    database_url_secret,
    log_context,
    redacted_database_url,
    secret_values,
)
from app.core.settings import Settings


def record(
    message: str = "hello", *args: object, level: int = logging.INFO, **extra: object
) -> logging.LogRecord:
    made = logging.LogRecord(
        name="app.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(made, key, value)
    return made


def rendered(formatter: JsonFormatter, made: logging.LogRecord) -> dict[str, object]:
    return json.loads(formatter.format(made))


class TestJsonShape:
    def test_one_json_object_per_line(self) -> None:
        line = JsonFormatter().format(record("worker starting"))
        assert "\n" not in line
        assert json.loads(line)["message"] == "worker starting"

    def test_carries_level_logger_and_timestamp(self) -> None:
        payload = rendered(JsonFormatter(), record("x", level=logging.WARNING))
        assert payload["level"] == "WARNING"
        assert payload["logger"] == "app.test"
        assert payload["ts"].endswith("+00:00")

    def test_extra_fields_are_top_level_so_they_can_be_grepped(self) -> None:
        payload = rendered(JsonFormatter(), record("sent", lead_count=3, campaign="spring"))
        assert payload["lead_count"] == 3
        assert payload["campaign"] == "spring"

    def test_an_extra_cannot_overwrite_a_built_in_field(self) -> None:
        # Losing the real level to a careless extra={"level": ...} would be worse than an
        # awkward key name.
        made = record("x", level=logging.ERROR)
        made.level = "debug"  # what extra={"level": "debug"} does to a record
        payload = rendered(JsonFormatter(), made)
        assert payload["level"] == "ERROR"
        assert payload["extra_level"] == "debug"

    def test_an_unserialisable_value_does_not_crash_the_line(self) -> None:
        payload = rendered(JsonFormatter(), record("x", when=object()))
        assert "object" in str(payload["when"])

    def test_an_exception_is_rendered_into_the_line(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            made = record("failed")
            made.exc_info = sys.exc_info()
        payload = rendered(JsonFormatter(), made)
        assert "ValueError" in str(payload["error"])


class TestContext:
    def test_bound_ids_appear_on_every_line_without_being_passed(self) -> None:
        with log_context(request_id="abc123", org_id="org-1"):
            payload = rendered(JsonFormatter(), record("handled"))
        assert payload["request_id"] == "abc123"
        assert payload["org_id"] == "org-1"

    def test_context_is_restored_afterwards(self) -> None:
        with log_context(request_id="abc123"):
            pass
        assert "request_id" not in current_context()

    def test_nested_context_restores_the_outer_value(self) -> None:
        with log_context(request_id="outer"):
            with log_context(request_id="inner"):
                assert current_context()["request_id"] == "inner"
            assert current_context()["request_id"] == "outer"

    def test_a_none_value_is_not_logged_as_the_string_none(self) -> None:
        with log_context(lead_id=None):
            payload = rendered(JsonFormatter(), record("x"))
        assert "lead_id" not in payload

    def test_an_unknown_name_is_ignored_rather_than_raising(self) -> None:
        # Adding context must never be able to break the thing it is making observable.
        with log_context(nonsense="x"):
            assert "nonsense" not in current_context()

    def test_text_formatter_shows_the_ids_too(self) -> None:
        with log_context(request_id="abc123"):
            line = TextFormatter().format(record("handled"))
        assert "request_id=abc123" in line


class TestRedaction:
    SECRET = "gsk_a_very_real_looking_key_0123456789"

    @pytest.fixture
    def formatter(self) -> JsonFormatter:
        return JsonFormatter(Redactor(frozenset({self.SECRET})))

    def test_a_secret_in_the_message_is_replaced(self, formatter: JsonFormatter) -> None:
        payload = rendered(formatter, record(f"calling with {self.SECRET}"))
        assert self.SECRET not in json.dumps(payload)
        assert "***" in str(payload["message"])

    def test_a_secret_in_a_format_argument_is_replaced(self, formatter: JsonFormatter) -> None:
        payload = rendered(formatter, record("key=%s", self.SECRET))
        assert self.SECRET not in json.dumps(payload)

    def test_a_secret_in_an_extra_field_is_replaced(self, formatter: JsonFormatter) -> None:
        payload = rendered(formatter, record("x", headers={"Authorization": self.SECRET}))
        assert self.SECRET not in json.dumps(payload)

    def test_a_secret_inside_an_exception_is_replaced(self, formatter: JsonFormatter) -> None:
        # The realistic path: a client raises an error quoting the request it was given.
        import sys

        try:
            raise RuntimeError(f"401 from provider, key {self.SECRET}")
        except RuntimeError:
            made = record("llm call failed")
            made.exc_info = sys.exc_info()
        assert self.SECRET not in formatter.format(made)

    def test_the_text_formatter_redacts_as_well(self) -> None:
        line = TextFormatter(Redactor(frozenset({self.SECRET}))).format(
            record(f"key {self.SECRET}")
        )
        assert self.SECRET not in line

    def test_a_longer_secret_containing_a_shorter_one_is_fully_replaced(self) -> None:
        short, long = "password", "password_extended_value"
        redact = Redactor(frozenset({short, long}))
        assert redact(f"using {long}") == "using ***"

    def test_short_values_are_not_treated_as_secrets(self) -> None:
        # Otherwise `SEND_MODE=live` becomes `SEND_MODE=***` and the logs stop being readable.
        settings = Settings(_env_file=None, GROQ_API_KEY="live", SMTP_PASSWORD="x")
        collected = secret_values(settings)
        assert "live" not in collected
        assert "x" not in collected

    def test_a_placeholder_is_still_redacted(self) -> None:
        # It is not a real secret, but neither is it worth a special case: redacting it costs
        # nothing and the alternative is a rule about which secrets are real.
        settings = Settings(_env_file=None, GROQ_API_KEY="your_groq_api_key_here")
        assert "your_groq_api_key_here" in secret_values(settings)

    def test_configured_secrets_are_collected_by_name(self) -> None:
        settings = Settings(
            _env_file=None,
            GROQ_API_KEY="groq_key_long_enough",
            SMTP_PASSWORD="smtp_password_long_enough",
            SUPABASE_ANON_KEY="anon_key_long_enough",
        )
        collected = secret_values(settings)
        assert "groq_key_long_enough" in collected
        assert "smtp_password_long_enough" in collected
        assert "anon_key_long_enough" in collected

    def test_the_database_password_is_a_secret_but_the_url_is_not(self) -> None:
        url = "postgresql+asyncpg://outreach:s3cret_password@127.0.0.1:5433/outreach"
        assert database_url_secret(url) == "s3cret_password"
        assert redacted_database_url(url) == (
            "postgresql+asyncpg://outreach:***@127.0.0.1:5433/outreach"
        )

    def test_a_url_without_a_password_is_left_alone(self) -> None:
        url = "postgresql+asyncpg://outreach@127.0.0.1:5433/outreach"
        assert redacted_database_url(url) == url
        assert database_url_secret(url) is None


class TestConfigure:
    def test_development_gets_readable_output_and_production_gets_json(self) -> None:
        try:
            configure_logging(Settings(_env_file=None, APP_ENV="development"), force=True)
            assert isinstance(logging.getLogger().handlers[0].formatter, TextFormatter)

            configure_logging(Settings(_env_file=None, APP_ENV="production"), force=True)
            assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
        finally:
            logging.getLogger().handlers.clear()

    def test_configuring_twice_does_not_double_every_line(self) -> None:
        try:
            configure_logging(Settings(_env_file=None, APP_ENV="production"), force=True)
            configure_logging(Settings(_env_file=None, APP_ENV="production"), force=True)
            assert len(logging.getLogger().handlers) == 1
        finally:
            logging.getLogger().handlers.clear()
