"""Shared test fixtures.

No test in this suite may touch the network. SMTP is faked with aiosmtpd, the LLM is
monkeypatched, and every recipient address is @example.com.
"""

import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Every environment variable the app reads, so a test never inherits the developer's .env.
APP_ENV_VARS = (
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_EMAIL",
    "SMTP_PASSWORD",
    "SMTP_TIMEOUT",
    "SMTP_USE_TLS",
    "IMAP_HOST",
    "IMAP_PORT",
    "IMAP_EMAIL",
    "IMAP_PASSWORD",
    "CALENDAR_LINK",
    "DB_PATH",
    "MAX_CONCURRENT_LEADS",
    "REPLY_CHECK_INTERVAL_MINUTES",
    "LOG_LEVEL",
    "APPROVAL_MODE",
    "AUTO_APPROVE",
    "SEND_MODE",
    "SEND_ALLOWLIST",
    "DRY_RUN_EMAILS",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every app environment variable and stop pydantic reading a real .env."""
    for name in APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def settings_factory(clean_env):
    """Build a Settings instance from explicit values, ignoring .env and .env.example."""
    from config.config import Settings

    def _make(**overrides):
        return Settings(_env_file=None, **overrides)

    return _make


@pytest.fixture
def valid_settings_kwargs():
    """The minimum set of real-looking required values."""
    return {
        "GROQ_API_KEY": "gsk_testkeytestkeytestkey",
        "SMTP_EMAIL": "sender@example.com",
        "SMTP_PASSWORD": "not-a-real-password",
    }


@pytest.fixture
async def fake_smtp():
    """An in-process SMTP server. Yields a handler whose .messages lists what it received."""
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    def accept_any_login(server, session, envelope, mechanism, auth_data):
        """send_email always authenticates, so the fake server must advertise AUTH."""
        return AuthResult(success=True)

    class Collector:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(
                {
                    "from": envelope.mail_from,
                    "rcpt": list(envelope.rcpt_tos),
                    "raw": envelope.content.decode("utf-8", errors="replace"),
                }
            )
            return "250 OK"

    # aiosmtpd's readiness probe dials the configured port, so port=0 is not usable here —
    # reserve a concrete free port first.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    handler = Collector()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        authenticator=accept_any_login,
        auth_require_tls=False,  # local fake server only; production requires STARTTLS
    )
    controller.start()
    try:
        handler.host = controller.hostname
        handler.port = controller.port
        yield handler
    finally:
        controller.stop()
