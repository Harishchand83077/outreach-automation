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
#: Without these the application cannot do its job for anybody. **The mail credentials are no
#: longer here** (ADR-023): there is no deployment-wide sending account, so what is required is
#: the ability to *let somebody connect their own* -- the OAuth client and the key that encrypts
#: what it returns. A deployment missing them has a dashboard on which nothing can ever send.
REQUIRED_KEYS = (
    "GROQ_API_KEY",
    "CREDENTIAL_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
)

#: Not required to start. Each maps to what stops working without it.
FEATURE_KEYS: dict[str, str] = {
    "CALENDAR_LINK": "drafts have no booking link to call to action with",
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
    # Added because check_env reported none of these, so its silence read as "nothing to
    # report" when the truth was "this script has never heard of them". An operator ran it,
    # saw "All required configuration is present", and still had Connect Gmail drawn as
    # unavailable with nothing on screen explaining why.
    # CREDENTIAL_KEY and the GOOGLE_* values moved to REQUIRED_KEYS with ADR-023: without
    # them nobody can connect a mailbox, and without a mailbox nothing can ever send.
    "Phase 10 - sign-up": ["DASHBOARD_URL"],
}

#: Models Groq has retired; silently upgraded so an old .env keeps working.
#: Models Groq has retired. An old `.env` naming one is silently upgraded rather than failing,
#: because the value is a default somebody set months ago, not a decision they are making now.
_DECOMMISSIONED_MODELS = {
    "llama3-70b-8192",
    "llama3-8b-8192",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
}
_CURRENT_MODEL = "openai/gpt-oss-120b"

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
    #: **No SMTP_EMAIL or SMTP_PASSWORD.** ADR-023: the deployment has no mailbox of its own, so
    #: every send authenticates as a credential somebody connected. Host and port stay, because
    #: they describe Google's servers rather than anybody's identity.
    SMTP_TIMEOUT: float = 90.0
    #: True for implicit TLS (port 465). Leave False for STARTTLS on 587.
    SMTP_USE_TLS: bool = False
    #: Require STARTTLS on a plaintext port. Only tests against a local fake server set this
    #: False; never disable it against a real mailbox, as credentials would cross in the clear.
    SMTP_START_TLS: bool = True

    # === IMAP ===
    IMAP_HOST: str = "imap.gmail.com"
    IMAP_PORT: int = 993

    # === Sending safety (hard rule 1) ===
    #: disabled = nothing leaves, drafts only. allowlist = only the org's own allowlist rows.
    #: live = normal. **There is no SEND_ALLOWLIST here** (ADR-023): one list for the whole
    #: deployment meant every workspace rehearsed against the operator's addresses and only
    #: somebody with shell access could edit it. The list is `app.send_allowlist`, per org.
    SEND_MODE: Literal["disabled", "allowlist", "live"] = "disabled"

    # === Who may have an account (ADR-017) ===
    #: closed = only members somebody inserted deliberately (today's behaviour, and the default
    #: for the same reason `orgs.send_mode` is nullable: deploying this must change nothing).
    #: allowlist = anyone at a domain in SIGNUP_ALLOWED_DOMAINS. open = anyone who can sign in.
    #:
    #: `open` deserves a clear head: anybody could then connect their own Gmail and send cold
    #: email through software you host. They send as themselves, with their own quota and their
    #: own reputation -- this is not an open relay -- but it is a decision, not a default.
    SIGNUP_MODE: Literal["closed", "allowlist", "open"] = "closed"
    #: Comma-separated email domains for SIGNUP_MODE=allowlist. Empty means nobody.
    SIGNUP_ALLOWED_DOMAINS: str = ""
    #: A ceiling on workspaces. One small instance and one shared free LLM quota: the failure
    #: mode of open sign-up is not a breach, it is strangers quietly exhausting the free tier
    #: until the people who actually use this get nothing.
    MAX_ORGS: int = 25

    #: Fernet key encrypting stored mailbox credentials (ADR-018). Generated with
    #: `python -m app.scripts.make_key`. Without it, connecting a mailbox is refused -- storing
    #: a refresh token in plain text in a database that is dumped nightly to S3 is not a thing
    #: to do by accident.
    CREDENTIAL_KEY: str = ""

    # === Google OAuth, for "Connect Gmail" (ADR-018) ===
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    #: Where Google sends the user back. Must match the Cloud console exactly, including scheme
    #: and trailing path -- a mismatch here is the commonest OAuth setup failure there is.
    GOOGLE_REDIRECT_URI: str = ""
    #: How many days a refresh token issued by this OAuth client survives. **Seven** for a client
    #: in Testing status, which is where ADR-018 deliberately leaves it: publishing a client that
    #: asks for a restricted scope needs an annual paid security assessment, and hard rule 5 says
    #: no. So a connection lapsing weekly is a product state with a known fix, not an incident.
    #:
    #: `0` means "never expires", which is what a published client gets. Set it the day the
    #: client is published and the warnings stop, with no code change.
    GOOGLE_GRANT_DAYS: int = 7

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
    #: A safety valve against a runaway loop, not a quota. Set far above any legitimate import —
    #: a thousand-lead import creates a thousand jobs and that is ordinary — because starvation
    #: between workspaces is solved by `claim`'s round-robin ordering, not by refusing work. If
    #: a workspace ever reaches this, something is enqueueing in a cycle.
    MAX_QUEUED_JOBS_PER_ORG: int = 20_000

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

    #: Where to send a browser after an OAuth round trip. Its own setting rather than
    #: "the first CORS origin", because a redirect target chosen by position in a list
    #: is one somebody reorders by accident. Empty means the API's own root.
    DASHBOARD_URL: str = ""

    #: This API's own public base, e.g. `https://api.example.com`. Used to build the one-click
    #: unsubscribe URL that goes in `List-Unsubscribe` (ADR-033). Its own setting rather than
    #: derived from `GOOGLE_REDIRECT_URI`, for the same reason as `DASHBOARD_URL`: a public URL
    #: inferred from another value is one that breaks quietly when that value changes.
    #:
    #: **Empty means no one-click header is sent at all**, which is the safe default: a URL
    #: built against a guess would have receiving servers POST opt-outs into the void, and
    #: claiming RFC 8058 compliance we cannot honour is the defect ADR-033 exists to fix.
    PUBLIC_API_URL: str = ""

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

    @field_validator("CREDENTIAL_KEY", mode="before")
    @classmethod
    def _strip_pasted_whitespace(cls, v: object) -> object:
        """Trim whitespace a key picks up on its way through a terminal and an editor.

        This validator used to exist for Google App Passwords, which are *displayed* grouped as
        `abcd efgh ijkl mnop` and fail silently when pasted with the spaces. Those are gone with
        ADR-023 — nobody types a mail password into this system any more.

        It is kept for `CREDENTIAL_KEY` because that value is generated inside a container and
        copied by hand into `.env`, and a stray newline there means every stored credential fails
        to decrypt with an error that points at the credential rather than at the key.
        """
        if isinstance(v, str):
            return "".join(v.split())
        return v

    @field_validator("GROQ_MODEL")
    @classmethod
    def _upgrade_retired_model(cls, v: str) -> str:
        return _CURRENT_MODEL if v.strip() in _DECOMMISSIONED_MODELS else v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def signup_allowed_domains(self) -> frozenset[str]:
        """Normalised sign-up domains. Matching is case-insensitive."""
        return frozenset(
            part.strip().lower().lstrip("@")
            for part in self.SIGNUP_ALLOWED_DOMAINS.split(",")
            if part.strip()
        )

    def allowed_origins(self) -> list[str]:
        """The browser origins allowed to call the api. Never `*` (issue C3).

        Here rather than in the app factory because two readers now need it: the CORS middleware,
        and the MCP endpoint's `Origin` check, which the transport spec requires. A second copy of
        this `split` is the shape of defect this project keeps finding.

        Not a `computed_field`: it would serialise into any dump of the settings object, and the
        list is configuration rather than something to report.
        """
        raw = self.CORS_ALLOWED_ORIGINS or ""
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def google_oauth_configured(self) -> bool:
        """Whether "Connect Gmail" can work at all. All three or none -- a client id with no
        secret produces a failure at the callback, which is the least debuggable moment."""
        return bool(
            self.GOOGLE_CLIENT_ID.strip()
            and self.GOOGLE_CLIENT_SECRET.strip()
            and self.GOOGLE_REDIRECT_URI.strip()
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
