"""Classifying LLM failures, because the response decides what to do next.

The prototype retried on bare `Exception` with a fixed 2-10 second backoff (issue E5). That
meant an invalid API key was retried three times — producing the same 401 slower — while a 429
carrying an explicit `Retry-After` was ignored and hammered.

Three outcomes, three different responses:

- **Retryable** (5xx, timeout, connection reset): back off and try the next entry.
- **Rate limited** (429, quota exhausted): cool this entry down for as long as the provider
  says, then try the next entry.
- **Permanent** (401, 403, 400): disable the entry and alert. Retrying cannot help, and
  quietly failing over hides a misconfiguration that someone needs to fix.

Schema-invalid output counts as that model's failure (§11), so a model that cannot follow the
output contract falls out of rotation instead of corrupting data.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum

#: Cool-down when the provider gives no hint. Short enough to recover, long enough not to spin.
DEFAULT_COOLDOWN = timedelta(minutes=1)
MAX_COOLDOWN = timedelta(hours=24)

#: Header names providers use to say when a limit resets. Checked in this order.
RESET_HEADERS = (
    "retry-after",
    "x-ratelimit-reset-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset",
)

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)


class Outcome(StrEnum):
    """Recorded on every `llm_usage` row, so failure patterns are visible per provider."""

    SUCCESS = "success"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    #: The call returned 200 but the body did not match the requested schema.
    INVALID_OUTPUT = "invalid_output"
    PERMANENT_ERROR = "permanent_error"


class LLMError(Exception):
    """Base for a failed call against one entry."""

    outcome: Outcome = Outcome.SERVER_ERROR
    #: Whether another entry should be tried. False only when nothing could help.
    should_failover: bool = True

    def __init__(self, message: str, *, entry_name: str | None = None) -> None:
        self.entry_name = entry_name
        super().__init__(message)


class RetryableError(LLMError):
    """Transient. Try the next entry; this one may recover shortly."""

    outcome = Outcome.SERVER_ERROR


class TimeoutError_(RetryableError):
    outcome = Outcome.TIMEOUT


class RateLimitedError(LLMError):
    """429, or a quota the provider says has run out."""

    outcome = Outcome.RATE_LIMITED

    def __init__(
        self,
        message: str,
        *,
        entry_name: str | None = None,
        retry_after: timedelta | None = None,
        daily_quota: bool = False,
    ) -> None:
        self.retry_after = retry_after
        self.daily_quota = daily_quota
        if daily_quota:
            self.outcome = Outcome.QUOTA_EXHAUSTED
        super().__init__(message, entry_name=entry_name)


class PermanentError(LLMError):
    """A misconfiguration or a bad request. Retrying produces the same answer more slowly."""

    outcome = Outcome.PERMANENT_ERROR


class InvalidOutputError(LLMError):
    """200, but the body did not match the schema we asked for.

    Counted against the model (§11): one that cannot honour the output contract should fall out
    of rotation rather than have its output accepted unvalidated.
    """

    outcome = Outcome.INVALID_OUTPUT


class CapacityExhausted(Exception):
    """Every usable entry is cooled down or out of headroom.

    LLM work pauses and the UI says when it resets. Deliberately *not* a silent downgrade to a
    worse model or a skipped validation step (§11) — the whole point of grounding is lost if the
    system quietly stops checking.
    """

    def __init__(self, message: str, *, resets_at: datetime | None = None) -> None:
        self.resets_at = resets_at
        super().__init__(message)


def parse_duration(value: str) -> timedelta | None:
    """Parse a provider's reset hint.

    Providers are inconsistent: an integer of seconds, an HTTP date, or Groq's `"7.66s"` and
    `"2m59.56s"`. All of them are more trustworthy than our own guess, so all are handled.
    """
    text = (value or "").strip()
    if not text:
        return None

    match = _DURATION.match(text)
    if match:
        amount = float(match.group(1))
        unit = (match.group(2) or "s").lower()
        factor = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
        return timedelta(seconds=amount * factor)

    # Compound forms such as "2m59.56s".
    compound = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", text, re.IGNORECASE)
    if compound:
        seconds = sum(
            float(amount) * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit.lower()]
            for amount, unit in compound
        )
        return timedelta(seconds=seconds)

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = when - datetime.now(tz=UTC)
    return delta if delta > timedelta(0) else timedelta(0)


def retry_after_from_headers(headers: dict[str, str]) -> timedelta | None:
    """The provider's own reset hint, preferred over any backoff we would invent."""
    lowered = {key.lower(): value for key, value in headers.items()}
    for name in RESET_HEADERS:
        if name in lowered:
            parsed = parse_duration(lowered[name])
            if parsed is not None:
                return min(parsed, MAX_COOLDOWN)
    return None


def looks_like_daily_quota(status: int, body: str) -> bool:
    """Distinguish "slow down" from "you are done for today".

    It matters: a per-minute limit clears on its own in seconds, while a daily quota means this
    entry is finished until it resets, and cooling it down for a minute would just re-fail
    repeatedly for hours.
    """
    if status not in (402, 429):
        return False
    haystack = body.lower()
    return any(
        marker in haystack
        for marker in (
            "daily",
            "per day",
            "tokens per day",
            "tpd",
            "quota exceeded",
            "insufficient_quota",
            "credit",
            "billing",
        )
    )


def classify_response(
    status: int, headers: dict[str, str], body: str, *, entry_name: str
) -> LLMError:
    """Turn a non-200 response into the right exception."""
    retry_after = retry_after_from_headers(headers)

    if status in (429, 402):
        return RateLimitedError(
            f"{entry_name}: rate limited ({status})",
            entry_name=entry_name,
            retry_after=retry_after,
            daily_quota=looks_like_daily_quota(status, body),
        )

    if status in (401, 403):
        # A bad key will not fix itself. Failing over hides it; alerting surfaces it.
        return PermanentError(
            f"{entry_name}: authentication rejected ({status}) — check {entry_name}'s API key",
            entry_name=entry_name,
        )

    if status == 400:
        return PermanentError(
            f"{entry_name}: bad request (400) — {body[:200]}", entry_name=entry_name
        )

    if status == 404:
        # Usually a model name that the provider has retired.
        return PermanentError(
            f"{entry_name}: model or endpoint not found (404) — {body[:200]}",
            entry_name=entry_name,
        )

    if status >= 500:
        return RetryableError(f"{entry_name}: provider error ({status})", entry_name=entry_name)

    return RetryableError(
        f"{entry_name}: unexpected status {status} — {body[:200]}", entry_name=entry_name
    )


def cooldown_for(error: LLMError, *, consecutive_failures: int) -> timedelta:
    """How long to rest an entry after a failure.

    The provider's own hint wins. Otherwise exponential backoff on consecutive failures, so a
    persistently broken entry drops out of rotation quickly instead of being retried every call.
    """
    if isinstance(error, RateLimitedError):
        if error.retry_after is not None:
            return min(error.retry_after, MAX_COOLDOWN)
        if error.daily_quota:
            # No hint and a daily quota: rest until the next UTC midnight, the usual reset.
            now = datetime.now(tz=UTC)
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return min(tomorrow - now, MAX_COOLDOWN)

    if isinstance(error, PermanentError):
        # Effectively disabled until someone fixes the configuration.
        return MAX_COOLDOWN

    seconds = min(DEFAULT_COOLDOWN.total_seconds() * (2 ** max(consecutive_failures - 1, 0)), 1800)
    return timedelta(seconds=seconds)
