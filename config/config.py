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

    # === Workflow ===
    APPROVAL_MODE: Literal["cli", "api"] = "cli"
    AUTO_APPROVE: bool = False
    CALENDAR_LINK: str = ""
    DB_PATH: str = "leads.db"
    MAX_CONCURRENT_LEADS: int = 5
    REPLY_CHECK_INTERVAL_MINUTES: int = 5
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("AUTO_APPROVE", "SMTP_USE_TLS", mode="before")
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
