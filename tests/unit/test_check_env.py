"""`check_env`: the one command an operator runs when they are unsure what is configured.

Two properties matter, and the second was a real defect found during the first deploy.

**It never prints a value.** Hard rule 2. The whole point of this script is that it can be run
on a server and pasted into a chat window, so the test asserts the absence of the secrets it was
given rather than the presence of anything.

**Its warnings must be true where they fire.** The ADR-005 warning about port 5432 is correct on
a developer machine, where another project's Postgres may already hold that port. Inside the
Compose network it is nonsense: every container has its own address space, `postgres:5432` is
both correct and unavoidable, and the warning fired on a perfectly good production config — at
the exact moment somebody is deciding whether to trust what they are reading.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from app.core.settings import Settings
from app.scripts import check_env


def run(settings: Settings) -> str:
    """Run the report against an explicit Settings, capturing stdout."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        check_env.main(settings)
    return buffer.getvalue()


def configured(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_a_real_looking_key_1234567890",
        "SMTP_EMAIL": "sender@example.com",
        "SMTP_PASSWORD": "not-a-real-password-either",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TestTheDatabaseWarning:
    def test_it_fires_for_the_default_port_on_a_developer_machine(self) -> None:
        settings = configured(
            DATABASE_URL="postgresql+asyncpg://outreach:pw@127.0.0.1:5432/outreach"
        )
        assert "5433" in run(settings)

    def test_it_is_silent_for_this_project_s_own_port(self) -> None:
        settings = configured(
            DATABASE_URL="postgresql+asyncpg://outreach:pw@127.0.0.1:5433/outreach"
        )
        assert "5433 is the Postgres default" not in run(settings)
        assert "another project's database" not in run(settings)

    def test_it_is_silent_inside_the_compose_network(self) -> None:
        """`postgres:5432` is correct there, and a false alarm during a deploy is worse than
        no alarm: it teaches the reader to discount the next one."""
        settings = configured(
            DATABASE_URL="postgresql+asyncpg://outreach:pw@postgres:5432/outreach"
        )
        assert "another project's database" not in run(settings)

    def test_it_still_reports_where_the_database_is(self) -> None:
        settings = configured(
            DATABASE_URL="postgresql+asyncpg://outreach:pw@postgres:5432/outreach"
        )
        assert "postgres:5432/outreach" in run(settings)


class TestItNeverPrintsASecret:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("GROQ_API_KEY", "gsk_secret_value_abcdefghijklmnop"),
            ("SMTP_PASSWORD", "app-password-abcd-efgh-ijkl"),
            ("IMAP_PASSWORD", "imap-password-abcd-efgh-ijkl"),
            ("SUPABASE_ANON_KEY", "anon_key_abcdefghijklmnopqrstuv"),
        ],
    )
    def test_the_value_is_absent_from_the_output(self, name: str, value: str) -> None:
        output = run(configured(**{name: value}))
        assert value not in output
        assert name in output, "the variable's NAME is what the operator needs"

    def test_the_database_password_is_not_printed(self) -> None:
        settings = configured(
            DATABASE_URL="postgresql+asyncpg://outreach:hunter2_is_secret@postgres:5432/outreach"
        )
        assert "hunter2_is_secret" not in run(settings)


class TestTheSendModeReport:
    def test_disabled_says_nothing_will_be_sent(self) -> None:
        assert "nothing will be sent" in run(configured(SEND_MODE="disabled"))

    def test_live_says_so_plainly(self) -> None:
        """An operator skimming this output must not be able to miss that sending is live."""
        assert "LIVE" in run(configured(SEND_MODE="live"))
