"""The only code in the system that talks to an SMTP server.

`ARCHITECTURE_TARGET.md` §9.6. `SEND_MODE` is checked here, at the boundary, in addition to the
eligibility checks the send loop performs. That duplication is deliberate: a future caller that
forgets the loop's checks still cannot reach a stranger's inbox, because the last thing between
this process and the network asks the question again.

The prototype gated sending on `DRY_RUN_EMAILS`, where the documented "send real emails" value
of `0` silently suppressed everything while still marking leads as sent (issue B1).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.core.settings import Settings, config
from app.core.settings import config as default_config
from app.services.mail.message import BuiltMessage


class SmtpOutcome(StrEnum):
    """Why a send did or did not happen.

    Three outcomes, not a boolean: the prototype collapsed them, so a suppressed send was
    recorded as a success (issue B1).
    """

    SENT = "sent"
    #: Refused by SEND_MODE or the allowlist. Not an error.
    BLOCKED = "blocked"
    #: The server refused the recipient outright — a hard bounce at RCPT time.
    REJECTED = "rejected"
    #: Transport failed, or the server deferred. Retryable.
    FAILED = "failed"
    #: Authentication failed. The mailbox must be paused, not retried (§9.8).
    AUTH_FAILED = "auth_failed"


@dataclass
class SendReport:
    outcome: SmtpOutcome
    detail: str = ""
    #: The server's reply, stored on the message row for the audit trail.
    response: str = ""

    @property
    def sent(self) -> bool:
        return self.outcome is SmtpOutcome.SENT

    @property
    def should_pause_mailbox(self) -> bool:
        """Repeated auth failure means the credentials are wrong; retrying is pointless and
        looks like an attack to the provider (§9.8)."""
        return self.outcome is SmtpOutcome.AUTH_FAILED


def allowlist_permits(to_email: str, settings: Settings | None = None) -> bool:
    """The one send decision, as in the Phase 0 patch but now at the SMTP boundary.

    - `disabled` (the default): nothing may be sent, to anybody.
    - `allowlist`: only addresses in `SEND_ALLOWLIST`, compared case-insensitively.
    - `live`: anybody. Set by the operator, in production, deliberately.
    """
    settings = settings or config
    recipient = (to_email or "").strip().lower()
    if not recipient:
        return False
    if settings.SEND_MODE == "live":
        return True
    if settings.SEND_MODE == "allowlist":
        return recipient in settings.send_allowlist
    return False


class SmtpSender:
    """Sends one message. Holds no eligibility logic beyond the `SEND_MODE` boundary check."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or default_config

    async def send(self, built: BuiltMessage, *, to_email: str) -> SendReport:
        """Deliver one message, or explain why it was not delivered.

        Never raises for an expected failure. The send loop needs an outcome it can record
        against the outbox row, and an exception escaping here would leave that row in
        `sending` with nothing to resolve it.
        """
        settings = self._settings

        if not allowlist_permits(to_email, settings):
            return SendReport(
                outcome=SmtpOutcome.BLOCKED,
                detail=(f"SEND_MODE={settings.SEND_MODE} does not permit this recipient"),
            )

        try:
            import aiosmtplib
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise RuntimeError("aiosmtplib is not installed") from exc

        try:
            response = await aiosmtplib.send(
                built.message,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_EMAIL,
                password=settings.SMTP_PASSWORD,
                use_tls=settings.SMTP_USE_TLS,
                start_tls=settings.SMTP_START_TLS if not settings.SMTP_USE_TLS else False,
                timeout=settings.SMTP_TIMEOUT,
            )
        except Exception as exc:
            return self._classify(exc)

        # aiosmtplib returns (errors_by_recipient, message). A non-empty mapping means the
        # server accepted the session but refused this address.
        errors = response[0] if isinstance(response, tuple) else {}
        if errors:
            return SendReport(
                outcome=SmtpOutcome.REJECTED,
                detail=f"the server refused the recipient: {errors}",
                response=str(errors)[:1000],
            )

        return SendReport(
            outcome=SmtpOutcome.SENT,
            response=str(response[1] if isinstance(response, tuple) else response)[:1000],
        )

    @staticmethod
    def _classify(exc: Exception) -> SendReport:
        """Turn an exception into an outcome the loop can act on.

        Auth failure is separated out because the response is different in kind: back off and
        retry for a transport error, but stop and alert for a credential one. Retrying a wrong
        password looks like a brute-force attempt and gets the mailbox locked.
        """
        name = type(exc).__name__
        text = str(exc)

        if "Auth" in name or "authentication" in text.lower():
            return SendReport(
                outcome=SmtpOutcome.AUTH_FAILED,
                detail=f"{name}: {text[:300]}",
                response=text[:1000],
            )

        # A 5xx on the recipient is a hard bounce discovered at send time.
        if name in ("SMTPRecipientsRefused", "SMTPNotSupported") or text.startswith("5"):
            return SendReport(
                outcome=SmtpOutcome.REJECTED,
                detail=f"{name}: {text[:300]}",
                response=text[:1000],
            )

        return SendReport(
            outcome=SmtpOutcome.FAILED, detail=f"{name}: {text[:300]}", response=text[:1000]
        )
