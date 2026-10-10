"""Structured logging.

`ARCHITECTURE_TARGET.md` §18, issue E7: the prototype logged to a Rich console, which is
pleasant to watch and impossible to search. On a VM, logs are read through
`docker compose logs` and `grep` six hours after the thing went wrong, by somebody who knows
only a request id. So every line is one JSON object on one line, carrying the ids that let a
lead, a job or a request be followed end to end.

Three decisions worth stating:

- **Context travels in `contextvars`, not in arguments.** A request id that has to be passed
  down to be logged is a request id that will be missing from the one line that mattered. The
  api binds it once per request, the worker binds a job id once per job, and every line emitted
  underneath carries it without the emitting code knowing it exists.

- **Secret values are scrubbed from the rendered line.** Hard rule 2 says never log a secret,
  and the primary defence is not logging one; this is the second. Any configured secret long
  enough to be real is replaced with `***` wherever it appears in a message, an argument or an
  exception's text — which is exactly where one arrives unintentionally, inside a driver's
  connection error. `DATABASE_URL`'s password is scrubbed on its own, because the URL itself is
  harmless and worth seeing.

- **Human-readable in development, JSON everywhere else.** Reading JSON by eye while writing a
  feature is a needless tax, and a developer machine has no log aggregator to feed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.settings import Settings, config

#: Ids bound for the duration of a request or a job, and attached to every line emitted inside
#: it. All optional: a line from startup belongs to no request.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)
lead_id_var: ContextVar[str | None] = ContextVar("lead_id", default=None)
org_id_var: ContextVar[str | None] = ContextVar("org_id", default=None)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "job_id": job_id_var,
    "lead_id": lead_id_var,
    "org_id": org_id_var,
}

#: Attributes `logging` puts on every record. Anything else came from `extra=` and is ours.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno lineno
    module msecs message msg name pathname process processName relativeCreated stack_info
    taskName thread threadName""".split()
)

#: A secret shorter than this is either empty, a placeholder, or too short to redact without
#: mangling ordinary text — `SEND_MODE=live` should not become `SEND_MODE=***`.
MIN_SECRET_LENGTH = 8

REDACTED = "***"


def current_context() -> dict[str, str]:
    """The ids bound right now. Used by the formatter, and by alerts."""
    return {name: value for name, var in _CONTEXT_VARS.items() if (value := var.get())}


@contextlib.contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    """Bind ids for the duration of a block, restoring whatever was bound before.

    Unknown names are ignored rather than raising: a caller adding context should never be able
    to break the thing it is trying to make observable.
    """
    tokens = []
    for name, value in values.items():
        var = _CONTEXT_VARS.get(name)
        if var is not None:
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def database_url_secret(url: str) -> str | None:
    """The password inside a SQLAlchemy/libpq URL, if it has one.

    Separated out because the URL is worth logging and its password is not, and because a
    connection error from the driver quotes the whole URL back at you.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return parts.password or None


def redacted_database_url(url: str) -> str:
    """`url` with its password replaced, for the one line that reports what we connected to."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.password or parts.hostname is None:
        return url
    user = f"{parts.username}:{REDACTED}@" if parts.username else f"{REDACTED}@"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(parts._replace(netloc=f"{user}{parts.hostname}{port}"))


def secret_values(settings: Settings) -> frozenset[str]:
    """Every configured value that must never appear in a log line.

    Collected by name from the settings object, so a secret added to `Settings` later is covered
    by naming it rather than by remembering this function exists.
    """
    names = (
        "GROQ_API_KEY",
        "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "TAVILY_API_KEY",
        "SMTP_PASSWORD",
        "IMAP_PASSWORD",
        "INTERNAL_TICK_SECRET",
        "SUPABASE_ANON_KEY",
        "ALERT_WEBHOOK_URL",
    )
    found = {str(getattr(settings, name, "") or "").strip() for name in names}
    if password := database_url_secret(settings.DATABASE_URL):
        found.add(password)
    return frozenset(value for value in found if len(value) >= MIN_SECRET_LENGTH)


class Redactor:
    """Replaces known secret values in rendered text.

    Longest first, so a secret that contains another is not half-replaced.
    """

    def __init__(self, secrets: frozenset[str]) -> None:
        self._secrets = sorted(secrets, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    `extra=` fields are merged in at the top level, which is what makes them greppable; a key
    that collides with a built-in field is prefixed rather than silently overwriting it, because
    losing the level or the timestamp to a careless `extra={"level": ...}` would be worse.
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__()
        self.redactor = redactor or Redactor(frozenset())

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context())

        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[f"extra_{key}" if key in payload else key] = value

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # `default=str` so a UUID, a datetime or a model never turns a log line into a crash.
        return self.redactor(json.dumps(payload, default=str, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    """Development output: readable, with the bound ids appended so they are still visible."""

    def __init__(self, redactor: Redactor | None = None) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-7s %(name)s %(message)s")
        self.redactor = redactor or Redactor(frozenset())

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if context := current_context():
            line += " " + " ".join(f"{key}={value}" for key, value in sorted(context.items()))
        return self.redactor(line)


#: Libraries whose INFO output is noise at our volume. Warnings and above still come through.
_QUIET = {
    "asyncio": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "urllib3": logging.WARNING,
    "uvicorn.access": logging.WARNING,
}

_configured = False


def configure_logging(settings: Settings | None = None, *, force: bool = False) -> None:
    """Install the formatter on the root logger. Called once by the api and by the worker.

    Idempotent, because both the api factory and `uvicorn` reach for logging configuration and
    two handlers mean every line twice.
    """
    global _configured
    if _configured and not force:
        return

    settings = settings or config
    redactor = Redactor(secret_values(settings))
    formatter: logging.Formatter
    if settings.APP_ENV == "development":
        formatter = TextFormatter(redactor)
    else:
        formatter = JsonFormatter(redactor)

    # stdout, not stderr: on this deployment the container's stdout *is* the log, and splitting
    # a single stream of events across two by severity makes ordering unrecoverable.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL)

    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(level)

    _configured = True
