"""Shared test fixtures.

No test in this suite may touch the network. SMTP is faked with aiosmtpd, the LLM is
monkeypatched, and every recipient address is @example.com.
"""

import io
import socket
import sys
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Every environment variable the app reads, so a test never inherits the developer's .env.
APP_ENV_VARS = (
    "GROQ_API_KEY", "GROQ_MODEL",
    "SMTP_HOST", "SMTP_PORT", "SMTP_EMAIL", "SMTP_PASSWORD", "SMTP_TIMEOUT", "SMTP_USE_TLS",
    "IMAP_HOST", "IMAP_PORT", "IMAP_EMAIL", "IMAP_PASSWORD",
    "CALENDAR_LINK", "DB_PATH", "MAX_CONCURRENT_LEADS", "REPLY_CHECK_INTERVAL_MINUTES",
    "LOG_LEVEL", "APPROVAL_MODE", "AUTO_APPROVE", "SEND_MODE", "SEND_ALLOWLIST",
    "DRY_RUN_EMAILS",
)


def executable_source(module) -> str:
    """Return a module's source with comments and docstrings removed.

    Several tests assert that a dangerous string no longer appears in a module — a lock against
    quietly reintroducing it. A plain source grep would also match the comments that *explain*
    why the string was removed, so those are stripped first. Ordinary string literals are kept,
    because that is the form a reintroduced bug would take (`status="MEETING_BOOKED"`).
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    kept: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            continue
        if token.type == tokenize.STRING and token.string.lstrip("rbfuRBFU")[:3] in ('"""', "'''"):
            continue  # docstring or long prose block
        kept.append(token.string)
    return "\n".join(kept)


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
            self.messages.append({
                "from": envelope.mail_from,
                "rcpt": list(envelope.rcpt_tos),
                "raw": envelope.content.decode("utf-8", errors="replace"),
            })
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
