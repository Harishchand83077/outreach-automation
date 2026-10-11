"""Configuration, validated at startup.

Values come from the environment and from `.env` only. `.env.example` is documentation; it is
never a source of values, because the prototype loaded it as defaults and copied its placeholders
into the environment, so `your_groq_api_key_here` passed validation and the dashboard reported
itself correctly configured (issue B3).

Startup fails if a required value is empty *or* still looks like a placeholder.
"""

import os
from typing import Literal

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Required for the full workflow. Reported together so one run fixes them all.
REQUIRED_KEYS = ("GROQ_API_KEY", "SMTP_EMAIL", "SMTP_PASSWORD")

#: Not required to start. Each maps to what stops working without it.
FEATURE_KEYS: dict[str, str] = {
    "IMAP_EMAIL": "replies are not monitored",
    "IMAP_PASSWORD": "replies are not monitored",
    "CALENDAR_LINK": "drafts have no booking link to call to action with",
    "SEND_ALLOWLIST": "SEND_MODE=allowlist can reach nobody",
}

#: Configuration for phases that have not landed yet. Declared and validated so a value can be
#: provisioned early and checked, grouped by the phase whose code will first read it.
FUTURE_KEYS: dict[str, list[str]] = {
    "Phase 1 - database": ["DATABASE_URL"],
    "Phase 3 - LLM router": [
        "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
    ],
    "Phase 3 - research": ["TAVILY_API_KEY"],
    "Phase 5/6 - worker": ["INTERNAL_TICK_SECRET"],
    "Phase 7 - auth": [
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_JWKS_URL",
        "CORS_ALLOWED_ORIGINS",
    ],
    "Phase 9 - ops": ["ALERT_WEBHOOK_URL"],
}

#: Models Groq has retired; silently upgraded so an old .env keeps working.
_DECOMMISSIONED_MODELS = {"llama3-70b-8192", "llama3-8b-8192"}
_CURRENT_MODEL = "llama-3.3-70b-versatile"

_PLACEHOLDER_PREFIXES = (
    "your_",
    "your-",
    "yourkey",
    "changeme",
    "change_me",
    "change-me",
    "replace_me",
    "replace-me",
    "replaceme",
    "placeholder",
    "insert_",
    "add_your",
    "dummy",
    "<",
    "xxx",
    "todo",
    "tbd",
)
_PLACEHOLDER_EXACT = {"none", "null", "n/a", "na", "-", "secret", "password", "apikey", "api_key"}


class ConfigError(RuntimeError):
    """Raised at startup when configuration is incomplete. Never contains a value."""


def looks_like_placeholder(value: str) -> bool:
    """True if `value` is obviously copied from `.env.example` rather than being real.

    Errs toward returning True: refusing to start is cheap, emailing strangers with a broken
    config is not.
    """
    v = (value or "").strip().lower()
    if not v:
        return False
    return v.startswith(_PLACEHOLDER_PREFIXES) or v in _PLACEHOLDER_EXACT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === LLM ===
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = _CURRENT_MODEL

    # === SMTP ===
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_EMAIL: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_TIMEOUT: float = 90.0
    #: True for implicit TLS (port 465). Leave False for STARTTLS on 587.
    SMTP_USE_TLS: bool = False
    #: Require STARTTLS on a plaintext port. Only tests against a local fake server set this
    #: False; never disable it against a real mailbox, as credentials would cross in the clear.
    SMTP_START_TLS: bool = True

    # === IMAP ===
    IMAP_HOST: str = "imap.gmail.com"
    IMAP_PORT: int = 993
    IMAP_EMAIL: str = ""
    IMAP_PASSWORD: str = ""

    # === Sending safety (hard rule 1) ===
    #: disabled = nothing leaves, drafts only. allowlist = only SEND_ALLOWLIST. live = normal.
    SEND_MODE: Literal["disabled", "allowlist", "live"] = "disabled"
    #: Comma-separated addresses that may receive mail when SEND_MODE=allowlist.
    SEND_ALLOWLIST: str = ""

    # === Database (ADR-001: our own Postgres, not Supabase) ===
    #: Async SQLAlchemy URL. `docker compose -f deploy/compose.yml up -d` serves this default.
    #: Port 5433, never the default 5432, which another project's Postgres may already hold.
    DATABASE_URL: str = "postgresql+asyncpg://outreach:outreach_local_dev@127.0.0.1:5433/outreach"
    #: Keep pools small — the api and the worker share one database.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 2
    DB_ECHO: bool = False
    #: Migrations refuse to run unless the connected database is named this (ADR-005).
    DB_EXPECTED_NAME: str = "outreach"

    # === Job queue and worker ===
    #: How long a claimed job stays leased before the reaper may requeue it.
    JOB_LEASE_SECONDS: int = 120
    #: Attempts before a job is marked dead and surfaced to a human.
    JOB_MAX_ATTEMPTS: int = 5
    #: Idle poll interval. The runner backs off to this when there is no work.
    JOB_POLL_SECONDS: float = 2.0

    # === Auth (Supabase is used for AUTHENTICATION ONLY — ADR-001) ===
    # Declared now so the values are validated and visible to check_env. The code that consumes
    # them lands in Phase 7; the anon key is public by design and used by the browser, while the
    # api verifies each JWT locally against the JWKS.
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_JWKS_URL: str = ""
    #: Comma-separated browser origins allowed to call the api. Replaces the prototype's
    #: `allow_origins=["*"]` with credentials enabled (issue C3).
    CORS_ALLOWED_ORIGINS: str = ""

    # === LLM router (Phase 3) ===
    # Entries whose key is unset are skipped by the router, so adding a provider is just a
    # matter of setting its key. One account per provider — never rotate accounts to evade a
    # free-tier quota (hard rule 8).
    #: Ordered JSON list of provider entries, overriding the built-in chain. Each entry names
    #: the *environment variable* holding its key, never the key, so this value is safe to log.
    #: Left empty, app.services.llm.entries.DEFAULT_CHAIN is used and entries whose key is
    #: absent are skipped — so adding a provider is just setting its key.
    LLM_CHAIN: str = ""
    CEREBRAS_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    #: Google AI Studio. Outside the EEA/UK this tier may train on prompts, so the router marks
    #: it allow_pii=false: public website text only, never a lead's name, address or reply.
    GEMINI_API_KEY: str = ""
    #: The paid fallback stays off unless both of these are set deliberately.
    PAID_LLM_ENABLED: bool = False
    PAID_LLM_DAILY_USD_CAP: float = 0.0

    # === Research (Phase 3) ===
    TAVILY_API_KEY: str = ""
    TAVILY_MONTHLY_CREDIT_BUDGET: int = 1000

    # === Worker and operations (Phases 5, 6, 9) ===
    #: Shared secret for POST /internal/tick, which runs one scheduler cycle.
    INTERNAL_TICK_SECRET: str = ""
    #: Optional webhook for alerts: SMTP auth failure, LLM chain exhausted, dead jobs, a bounce
    #: spike. Without it, alerts are visible only in the dashboard.
    ALERT_WEBHOOK_URL: str = ""
    #: Only needed if the mail provider does not copy SMTP-sent mail to the Sent folder, so a
    #: crashed send can still be found by Message-ID (ADR-002).
    BCC_SELF: bool = False

    # === Retention (§17, MASTER_PROMPT Q7) ===
    #: How long a lead is kept after reaching a terminal state. Q7's answer is twelve months.
    #: The sweep erases through the same path a data-subject request takes, which means the
    #: hashed suppression survives — a lead that opted out must not become contactable again
    #: because the system tidied up.
    LEAD_RETENTION_MONTHS: int = 12
    #: How long the bulk page text behind a research finding is kept. The findings themselves
    #: stay; this is the large, least reusable part.
    RESEARCH_TEXT_RETENTION_DAYS: int = 30
    #: Set false to stop the sweep entirely — for a migration, or while investigating something
    #: that would otherwise be erased out from under you.
    RETENTION_ENABLED: bool = True

    # === Workflow ===
    APPROVAL_MODE: Literal["cli", "api"] = "cli"
    AUTO_APPROVE: bool = False
    CALENDAR_LINK: str = ""
    DB_PATH: str = "leads.db"
    MAX_CONCURRENT_LEADS: int = 5
    REPLY_CHECK_INTERVAL_MINUTES: int = 5
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    #: Which deployment this is. Production hides the interactive docs, because an unauthenticated
    #: schema of every endpoint is a map for anyone who finds the URL.
    APP_ENV: Literal["development", "staging", "production"] = "development"

    # RETENTION_ENABLED is deliberately absent from this list. For the flags below, an unset
    # value meaning "off" is the safe reading. For retention it is not: `RETENTION_ENABLED=`
    # would quietly stop the system forgetting anything, which is the kind of silent failure
    # this project refuses elsewhere. An empty value there fails startup instead.
    @field_validator("AUTO_APPROVE", "SMTP_USE_TLS", "PAID_LLM_ENABLED", "BCC_SELF", mode="before")
    @classmethod
    def _tolerate_empty_bool(cls, v: object) -> object:
        """An unset-but-present flag (`AUTO_APPROVE=`) means off, not a validation error."""
        if isinstance(v, str) and not v.strip():
            return False
        return v

    @field_validator("GROQ_MODEL")
    @classmethod
    def _upgrade_retired_model(cls, v: str) -> str:
        return _CURRENT_MODEL if v.strip() in _DECOMMISSIONED_MODELS else v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def send_allowlist(self) -> frozenset[str]:
        """Normalised recipient allowlist. Matching is case-insensitive."""
        return frozenset(
            part.strip().lower() for part in self.SEND_ALLOWLIST.split(",") if part.strip()
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def groq_configured(self) -> bool:
        """Honest answer for /api/config/check — a placeholder is not configured."""
        return bool(self.GROQ_API_KEY.strip()) and not looks_like_placeholder(self.GROQ_API_KEY)

    def validate_required(self) -> None:
        """Raise ConfigError unless every required value is present and real.

        Reports variable *names* only — never values, so this is safe to log.
        """
        problems: list[str] = []

        for key in REQUIRED_KEYS:
            value = getattr(self, key, "") or ""
            if not value.strip():
                problems.append(f"{key} is not set")
            elif looks_like_placeholder(value):
                problems.append(f"{key} still holds a placeholder from .env.example")

        if self.SEND_MODE == "allowlist" and not self.send_allowlist:
            problems.append("SEND_MODE=allowlist but SEND_ALLOWLIST is empty, so nothing can send")

        # The prototype's flag inverted its own meaning: bool("0") is True, so DRY_RUN_EMAILS=0 —
        # which .env.example documented as "send real emails" — silently disabled every send.
        # It is gone. Refuse to start rather than ignore it, because an operator who believes it
        # still controls sending would be wrong in the dangerous direction.
        if "DRY_RUN_EMAILS" in os.environ:
            problems.append(
                "DRY_RUN_EMAILS is set but no longer exists; remove it and use "
                "SEND_MODE=disabled|allowlist|live instead"
            )

        if problems:
            raise ConfigError(
                "Configuration is incomplete:\n  - "
                + "\n  - ".join(problems)
                + "\nSet these in .env in the project root (see env.example for the shape)."
            )


config = Settings()
