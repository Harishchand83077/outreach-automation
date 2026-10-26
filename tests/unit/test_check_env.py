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

from app.core.settings import FEATURE_KEYS, FUTURE_KEYS, REQUIRED_KEYS, Settings
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
        # Everything `REQUIRED_KEYS` names, so "configured" means what the script means by it.
        # ADR-023 replaced the mail credentials here with the OAuth client that lets each
        # person connect their own.
        "CREDENTIAL_KEY": "a_real_looking_fernet_key_1234567890=",
        "GOOGLE_CLIENT_ID": "1234567890-ci.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "a_real_looking_client_secret_123",
        "GOOGLE_REDIRECT_URI": "https://api.example.com/api/v1/connect/google/callback",
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
            # The mail passwords were here until ADR-023 removed them. These are the secrets a
            # deployment actually holds now: the key that encrypts every stored refresh token,
            # and the OAuth client secret that mints them.
            ("CREDENTIAL_KEY", "credential-key-abcdefghijklmnop"),
            ("GOOGLE_CLIENT_SECRET", "google-client-secret-abcdefgh"),
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


class TestEveryGatingVariableIsReported:
    """The script must know about every variable that decides whether a feature works.

    **Written because it did not.** `check_env` builds its whole report from `REQUIRED_KEYS`,
    `FEATURE_KEYS` and `FUTURE_KEYS`, and the Gmail-OAuth work added `CREDENTIAL_KEY`,
    `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` and `DASHBOARD_URL`
    without adding them to any of the three. So the one command whose job is answering "what is
    configured?" printed **"All required configuration is present."** on a server where Connect
    Gmail was drawn as unavailable and sign-up was shut.

    That is worse than an omission. A report that does not mention a variable is indistinguishable
    from a variable that is fine, so its silence was read as an answer. Confirming it took a
    hand-written `python -c` against the settings object inside the container.

    The same shape as every other defect here: a correct producer (`Settings`, which declares the
    fields) and a correct consumer (`check_env`, which prints the groups), with nothing checking
    that the two agreed. So this asserts the agreement instead of a list somebody has to remember
    to extend.
    """

    def _gating_fields(self) -> dict[str, set[str]]:
        """Every settings field read by a `*_configured` property, keyed by that property.

        Read out of the source with `ast` rather than from a hand-kept list, so a property added
        later is covered without anybody remembering this test exists -- which is the only way a
        guard against forgetting can itself not be forgotten.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(Settings))
        fields = set(Settings.model_fields)
        found: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_configured"):
                continue
            found[node.name] = {
                child.attr
                for child in ast.walk(node)
                if isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
                and child.attr in fields
            }
        return found

    def test_the_reader_finds_the_properties(self) -> None:
        """Guards the test below from passing vacuously.

        If the ast walk found nothing -- a renamed property, a changed decorator, a moved class --
        every assertion underneath would compare against an empty set and pass while proving
        nothing. A test that cannot fail is worse than no test, because it is counted as coverage.
        """
        gating = self._gating_fields()

        assert "google_oauth_configured" in gating
        assert gating["google_oauth_configured"] == {
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REDIRECT_URI",
        }
        assert "groq_configured" in gating

    def test_every_gating_field_appears_in_a_reported_group(self) -> None:
        reported = (
            set(REQUIRED_KEYS)
            | set(FEATURE_KEYS)
            | {key for names in FUTURE_KEYS.values() for key in names}
        )

        for prop, fields in self._gating_fields().items():
            missing = fields - reported
            assert not missing, (
                f"{prop} decides whether a feature works from {sorted(missing)}, but check_env "
                "reports none of them -- so the report stays silent about the exact values an "
                "operator is trying to confirm, and reads as 'nothing to report'. Add them to "
                "FUTURE_KEYS in app/core/settings.py."
            )

    def test_the_oauth_names_reach_the_printed_report(self) -> None:
        """The end-to-end version: the names an operator is looking for are actually on screen."""
        output = run(configured())

        for name in (
            "CREDENTIAL_KEY",
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_REDIRECT_URI",
            "DASHBOARD_URL",
        ):
            assert name in output, f"{name} is never mentioned by check_env"

    def test_signup_mode_is_reported_because_closed_looks_like_unconfigured(self) -> None:
        assert "SIGNUP_MODE" in run(configured())
        assert "nobody new can sign up" in run(configured())
        assert "OPEN" in run(configured(SIGNUP_MODE="open"))

    def test_the_report_still_prints_no_values(self) -> None:
        """The property that matters most, re-asserted over the newly reported variables."""
        secret = "ultra-secret-client-value-9876543210"
        output = run(
            configured(
                GOOGLE_CLIENT_ID="1234-abc.apps.googleusercontent.com",
                GOOGLE_CLIENT_SECRET=secret,
                CREDENTIAL_KEY=secret,
            )
        )

        assert secret not in output
        assert "googleusercontent" not in output
