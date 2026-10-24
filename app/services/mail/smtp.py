"""The only code in the system that talks to an SMTP server.

`ARCHITECTURE_TARGET.md` §9.6. `SEND_MODE` is checked here, at the boundary, in addition to the
eligibility checks the send loop performs. That duplication is deliberate: a future caller that
forgets the loop's checks still cannot reach a stranger's inbox, because the last thing between
this process and the network asks the question again.

The prototype gated sending on `DRY_RUN_EMAILS`, where the documented "send real emails" value
of `0` silently suppressed everything while still marking leads as sent (issue B1).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.core.settings import Settings, config
from app.core.settings import config as default_config
from app.services.mail.google_oauth import xoauth2_string
from app.services.mail.message import BuiltMessage

if TYPE_CHECKING:
    from app.services.mail.connection import MailLogin


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


#: The three modes. Named so it cannot be confused with a decision on `SEND_MODE` itself.
ALLOWED_SEND_MODES: tuple[str, ...] = ("disabled", "allowlist", "live")


def effective_send_mode(settings: Settings | None = None, override: str | None = None) -> str:
    """The mode that actually applies: the operator's choice, or the environment's if unset.

    ADR-010. The mode came only from the environment until now, which made it safe and also made
    it unusable: pausing an in-flight campaign meant an SSH session and a container restart, and
    the person who most needs to stop a campaign is not always the person who can do that.
    `orgs.send_mode` is the authority when set; the environment supplies the value when it is not.

    Two properties are kept rather than traded away:

    - **It still resolves in one place.** This function and `allowlist_permits` below are the
      only code that decides anything from a send mode, and a structural test over the whole
      tree asserts it. Reading the value from a column instead of the environment did not spread
      the decision out.
    - **Raising it is gated where it is written, not here.** Only an `owner` may set it, `live`
      is refused outside production, and the change writes an audit row naming who did it. That
      belongs at the API boundary: a guard that answers differently depending on who is asking
      is a guard nobody can reason about.

    An unknown override is treated as `disabled`. The column has a check constraint, so this is
    unreachable by design — which is exactly why it must fail closed if it ever happens.
    """
    settings = settings or config
    if override is None:
        return settings.SEND_MODE
    if override not in ALLOWED_SEND_MODES:
        return "disabled"
    return override


def allowlist_permits(
    to_email: str, settings: Settings | None = None, *, override: str | None = None
) -> bool:
    """The one send decision, as in the Phase 0 patch but now at the SMTP boundary.

    - `disabled` (the default): nothing may be sent, to anybody.
    - `allowlist`: only addresses in `SEND_ALLOWLIST`, compared case-insensitively.
    - `live`: anybody. Set by the operator, in production, deliberately.
    """
    settings = settings or config
    recipient = (to_email or "").strip().lower()
    if not recipient:
        return False
    mode = effective_send_mode(settings, override)
    if mode == "live":
        return True
    if mode == "allowlist":
        return recipient in settings.send_allowlist
    return False


logger = logging.getLogger(__name__)


class SmtpSender:
    """Sends one message. Holds no eligibility logic beyond the `SEND_MODE` boundary check."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or default_config

    @property
    def settings(self) -> Settings:
        """The configuration this sender was built with.

        Read by the send loop to resolve a mailbox's credentials against the same
        configuration the send itself will use -- a test that points the sender at a fake
        SMTP server must not have the login resolved against the real one."""
        return self._settings

    async def send(
        self,
        built: BuiltMessage,
        *,
        to_email: str,
        send_mode: str | None = None,
        login: MailLogin | None = None,
    ) -> SendReport:
        """Deliver one message, or explain why it was not delivered.

        Never raises for an expected failure. The send loop needs an outcome it can record
        against the outbox row, and an exception escaping here would leave that row in
        `sending` with nothing to resolve it.

        `login` says **as whom**. `None` means the environment's single account, which is what
        every caller meant before mailboxes had their own credentials and is what a single-tenant
        deployment still means. It does not affect **whether** we may send: that is
        `allowlist_permits` below, unchanged, in the one place hard rule 1 allows it to be.
        """
        settings = self._settings

        if not allowlist_permits(to_email, settings, override=send_mode):
            return SendReport(
                outcome=SmtpOutcome.BLOCKED,
                detail=(
                    f"send mode {effective_send_mode(settings, send_mode)!r} "
                    f"does not permit this recipient"
                ),
            )

        try:
            import aiosmtplib
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise RuntimeError("aiosmtplib is not installed") from exc

        login = login or MailLogin(username=settings.SMTP_EMAIL, password=settings.SMTP_PASSWORD)

        try:
            if login.uses_oauth:
                response = await self._send_xoauth2(aiosmtplib, built, login, settings)
            else:
                response = await aiosmtplib.send(
                    built.message,
                    hostname=settings.SMTP_HOST,
                    port=settings.SMTP_PORT,
                    username=login.username,
                    password=login.password or "",
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
    async def _send_xoauth2(
        aiosmtplib: Any, built: BuiltMessage, login: MailLogin, settings: Settings
    ) -> Any:
        """Send authenticating with an OAuth access token instead of a password.

        `aiosmtplib.send` has no XOAUTH2 option, so the conversation is driven by hand: connect,
        STARTTLS, `AUTH XOAUTH2 <base64>`, then hand the message to the same client. Everything
        after authentication is identical to the password path, which is the point -- the
        message, the envelope and the error handling are all unchanged.

        A 334 reply to the AUTH command means Google is offering to explain the refusal rather
        than accepting it; the client must send an empty line to get the real error. Without
        that the connection hangs until the timeout and the failure reads as a network problem.
        """
        assert login.access_token is not None
        client = aiosmtplib.SMTP(
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            use_tls=settings.SMTP_USE_TLS,
            start_tls=False,
            timeout=settings.SMTP_TIMEOUT,
        )
        await client.connect()
        try:
            if settings.SMTP_START_TLS and not settings.SMTP_USE_TLS:
                await client.starttls()
            encoded = base64.b64encode(
                xoauth2_string(login.username, login.access_token).encode("utf-8")
            ).decode("ascii")
            code, message = await client.execute_command(
                b"AUTH", b"XOAUTH2", encoded.encode("ascii")
            )
            if code == 334:
                # Google wants an empty response before it will say what was wrong.
                code, message = await client.execute_command(b"")
            if code not in (235, 250):
                raise aiosmtplib.SMTPAuthenticationError(code, message)
            return await client.send_message(built.message)
        finally:
            # Always, including after an auth failure: a half-open SMTP session left behind on
            # every cycle is how a provider starts refusing connections.
            try:
                await client.quit()
            except Exception as exc:  # pragma: no cover - best effort teardown
                # Debug, not warning: the send either worked or was already classified, and a
                # noisy line about closing a socket would train people to ignore this logger.
                logger.debug("closing the SMTP session failed", extra={"error": type(exc).__name__})

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
