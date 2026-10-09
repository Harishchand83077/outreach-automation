"""Regression tests for B1 and the SEND_MODE chokepoint.

Hard rule 1: no real email is ever sent while developing. Hard rule 9: when SEND_MODE is not
`live`, the sender must refuse any recipient not in SEND_ALLOWLIST, enforced in exactly one place.
"""

import pytest

import email_utils
from email_utils import SendOutcome, is_send_allowed
from tests.helpers import executable_source


@pytest.fixture
def send_env(clean_env, valid_settings_kwargs):
    """Point the app at a fresh Settings whose SMTP host is overridden per test."""

    def _configure(**overrides):
        from config import config as config_module
        from config.config import Settings

        values = {**valid_settings_kwargs, **overrides}
        new = Settings(_env_file=None, **values)
        clean_env.setattr(config_module, "config", new)
        clean_env.setattr(email_utils, "config", new)
        return new

    return _configure


class TestOneChokepoint:
    def test_legacy_flag_is_gone_from_the_sender(self):
        """B1 regression lock: bool("0") is True, so the old flag inverted its own meaning."""
        assert "DRY_RUN_EMAILS" not in executable_source(email_utils)

    def test_the_guard_is_a_single_named_function(self):
        assert callable(is_send_allowed)

    def test_default_mode_blocks(self, send_env):
        send_env()
        assert send_env().SEND_MODE == "disabled"


class TestIsSendAllowed:
    """Pure unit tests of the one decision function."""

    def test_disabled_blocks_every_recipient(self, send_env):
        s = send_env(SEND_MODE="disabled", SEND_ALLOWLIST="me@example.com")
        assert is_send_allowed("me@example.com", s) is False
        assert is_send_allowed("anyone@example.com", s) is False

    def test_allowlist_permits_only_listed(self, send_env):
        s = send_env(SEND_MODE="allowlist", SEND_ALLOWLIST="me@example.com,other@example.com")
        assert is_send_allowed("me@example.com", s) is True
        assert is_send_allowed("other@example.com", s) is True
        assert is_send_allowed("stranger@example.com", s) is False

    @pytest.mark.parametrize(
        "listed,candidate",
        [
            ("Me@Example.com", "me@example.com"),
            ("me@example.com", "ME@EXAMPLE.COM"),
            (" me@example.com , other@example.com ", "other@example.com"),
        ],
    )
    def test_matching_ignores_case_and_whitespace(self, send_env, listed, candidate):
        s = send_env(SEND_MODE="allowlist", SEND_ALLOWLIST=listed)
        assert is_send_allowed(candidate, s) is True

    def test_allowlist_mode_with_empty_list_blocks_everything(self, send_env):
        s = send_env(SEND_MODE="allowlist", SEND_ALLOWLIST="")
        assert is_send_allowed("me@example.com", s) is False

    def test_live_permits_anyone(self, send_env):
        s = send_env(SEND_MODE="live", SEND_ALLOWLIST="")
        assert is_send_allowed("stranger@example.com", s) is True


class TestSendEmailOutcomes:
    """send_email must distinguish 'sent' from 'blocked' from 'failed' so that callers
    cannot record a send that never happened (the second half of B1)."""

    @pytest.mark.asyncio
    async def test_disabled_returns_blocked_without_touching_smtp(self, send_env):
        # Port 1 is closed; if the guard leaked, this would raise or hang rather than return.
        send_env(SEND_MODE="disabled", SMTP_HOST="127.0.0.1", SMTP_PORT=1, SMTP_TIMEOUT=2)
        outcome = await email_utils.send_email(
            to_email="me@example.com", subject="s", body_text="b"
        )
        assert outcome is SendOutcome.BLOCKED

    @pytest.mark.asyncio
    async def test_unlisted_recipient_is_blocked_and_nothing_is_received(self, send_env, fake_smtp):
        send_env(
            SEND_MODE="allowlist",
            SEND_ALLOWLIST="me@example.com",
            SMTP_HOST=fake_smtp.host,
            SMTP_PORT=fake_smtp.port,
            SMTP_USE_TLS=False,
            SMTP_START_TLS=False,
        )
        outcome = await email_utils.send_email(
            to_email="stranger@example.com", subject="s", body_text="b"
        )
        assert outcome is SendOutcome.BLOCKED
        assert fake_smtp.messages == []

    @pytest.mark.asyncio
    async def test_allowlisted_recipient_is_sent(self, send_env, fake_smtp):
        send_env(
            SEND_MODE="allowlist",
            SEND_ALLOWLIST="me@example.com",
            SMTP_HOST=fake_smtp.host,
            SMTP_PORT=fake_smtp.port,
            SMTP_USE_TLS=False,
            SMTP_START_TLS=False,
        )
        outcome = await email_utils.send_email(
            to_email="me@example.com", subject="Hello", body_text="Body here"
        )
        assert outcome is SendOutcome.SENT
        assert len(fake_smtp.messages) == 1
        assert fake_smtp.messages[0]["rcpt"] == ["me@example.com"]
        assert "Hello" in fake_smtp.messages[0]["raw"]

    @pytest.mark.asyncio
    async def test_smtp_error_returns_failed_not_sent(self, send_env):
        send_env(
            SEND_MODE="allowlist",
            SEND_ALLOWLIST="me@example.com",
            SMTP_HOST="127.0.0.1",
            SMTP_PORT=1,
            SMTP_TIMEOUT=2,
        )
        outcome = await email_utils.send_email(
            to_email="me@example.com", subject="s", body_text="b"
        )
        assert outcome is SendOutcome.FAILED


class TestLeadIsNotMarkedSentWhenNothingWasSent:
    """B1's real damage: the lead was marked EMAIL_SENT on a skipped send."""

    @pytest.mark.asyncio
    async def test_blocked_send_does_not_mark_email_sent(self, clean_env):
        import nodes

        writes = []

        async def fake_update_lead(email, **fields):
            writes.append(fields)

        async def fake_send_email(**kwargs):
            return SendOutcome.BLOCKED

        clean_env.setattr(nodes, "update_lead", fake_update_lead)
        clean_env.setattr(nodes, "send_email", fake_send_email)

        result = await nodes.send_email_node(
            {
                "name": "A",
                "email": "stranger@example.com",
                "company": "Acme",
                "email_draft": "Subject: Hi\n\nBody",
            }
        )

        assert result["status"] != "EMAIL_SENT"
        assert result["email_sent"] is False
        assert all(w.get("status") != "EMAIL_SENT" for w in writes)

    @pytest.mark.asyncio
    async def test_successful_send_does_mark_email_sent(self, clean_env):
        import nodes

        writes = []

        async def fake_update_lead(email, **fields):
            writes.append(fields)

        async def fake_send_email(**kwargs):
            return SendOutcome.SENT

        clean_env.setattr(nodes, "update_lead", fake_update_lead)
        clean_env.setattr(nodes, "send_email", fake_send_email)

        result = await nodes.send_email_node(
            {
                "name": "A",
                "email": "me@example.com",
                "company": "Acme",
                "email_draft": "Subject: Hi\n\nBody",
            }
        )

        assert result["status"] == "EMAIL_SENT"
        assert any(w.get("status") == "EMAIL_SENT" for w in writes)
