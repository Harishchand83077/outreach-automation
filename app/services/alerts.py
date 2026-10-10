"""Raising an alert, and optionally pushing it somewhere a human looks.

`ARCHITECTURE_TARGET.md` §18. The table is the record and the webhook is the nudge, in that
order: `ALERT_WEBHOOK_URL` is optional, allowed to fail, and an alert that exists only inside a
failed HTTP request is an alert nobody will ever see.

Two rules about what goes in one:

- **Never an email body, and never a lead's reply.** An alert says "three sends are stuck in
  `unknown`", not what they said. Alert destinations are chat rooms and third-party webhooks, and
  a lead did not consent to their reply being posted into one (§18, issue D6).
- **Never a secret.** The message is written by us, not interpolated from an exception, for the
  same reason the api does not return exception text to a browser: a driver error is as likely to
  quote a connection string as anything useful.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import current_context
from app.core.settings import Settings, config
from app.db import ops
from app.db.models import Alert

logger = logging.getLogger(__name__)

#: Generous, but bounded: an alert is not worth holding a worker cycle open for.
WEBHOOK_TIMEOUT = 5.0


class Notifier(Protocol):
    """Anything that can push an alert outward. Tests pass a recorder."""

    async def __call__(self, alert: Alert) -> None: ...  # pragma: no cover - protocol


class WebhookNotifier:
    """Posts a JSON body to `ALERT_WEBHOOK_URL`. A failure is logged, never raised.

    The payload is deliberately flat and generic — `severity`, `kind`, `message` — so it can be
    pointed at a Slack-compatible endpoint, a Discord one or a shell script behind `nc` without
    the code needing to know which.
    """

    def __init__(
        self, settings: Settings | None = None, *, client: httpx.AsyncClient | None = None
    ):
        self.settings = settings or config
        self._client = client

    @property
    def configured(self) -> bool:
        return bool((self.settings.ALERT_WEBHOOK_URL or "").strip())

    async def __call__(self, alert: Alert) -> None:
        if not self.configured:
            return
        payload = {
            "severity": alert.severity,
            "kind": alert.kind,
            "message": alert.message,
            "at": alert.created_at.isoformat() if alert.created_at else None,
            "detail": alert.detail or {},
            "env": self.settings.APP_ENV,
        }
        try:
            if self._client is not None:
                response = await self._client.post(
                    self.settings.ALERT_WEBHOOK_URL, json=payload, timeout=WEBHOOK_TIMEOUT
                )
            else:
                async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
                    response = await client.post(self.settings.ALERT_WEBHOOK_URL, json=payload)
            if response.status_code >= 400:
                # The URL is not logged: it carries the token that makes it work.
                logger.warning(
                    "alert webhook rejected the alert",
                    extra={"status": response.status_code, "kind": alert.kind},
                )
        except Exception as exc:
            logger.warning(
                "alert webhook unreachable",
                extra={"error": type(exc).__name__, "kind": alert.kind},
            )


async def raise_alert(
    session: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    kind: str,
    message: str,
    severity: str = "warning",
    detail: dict[str, Any] | None = None,
    cooldown: timedelta | None = ops.ALERT_COOLDOWN,
    now: datetime | None = None,
    notifier: Notifier | None = None,
) -> Alert | None:
    """Record an alert and push it once. Returns None when suppressed as a repeat.

    The log line is emitted whether or not the row was written, because the cool-down is about
    not pestering a human — the logs should still show that the condition is ongoing.
    """
    context = current_context()
    alert = await ops.raise_alert(
        session,
        org_id=org_id,
        kind=kind,
        message=message,
        severity=severity,
        detail={**(detail or {}), **context} if context else detail,
        cooldown=cooldown,
        now=now,
    )

    log = logger.error if severity == "critical" else logger.warning
    log(
        "alert: %s",
        message,
        extra={"alert_kind": kind, "severity": severity, "suppressed": alert is None},
    )

    if alert is None:
        return None

    push = notifier if notifier is not None else WebhookNotifier()
    try:
        await push(alert)
    except Exception as exc:
        # The row is already written, which is the part that matters. Letting a failed
        # notification propagate would abort the worker cycle that noticed the problem — so the
        # system would stop working *because* it tried to tell somebody it was in trouble.
        logger.warning(
            "could not push the alert", extra={"error": type(exc).__name__, "alert_kind": kind}
        )
    return alert
