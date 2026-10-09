"""Email sending (SMTP) and inbox reading (IMAP).

**All outbound mail passes through `send_email`, and `send_email` asks `is_send_allowed` first.**
That one function is the only thing standing between a development run and a stranger's inbox, so
it is kept deliberately small and is covered by `tests/test_send_guard.py`. Anything else that
reaches SMTP is a bug.

The prototype gated sending on `DRY_RUN_EMAILS` instead, which inverted its own meaning:
`bool("0") is True`, so the value `.env.example` documented as "send real emails" silently
suppressed every send *while the lead was still marked EMAIL_SENT* (issue B1). That flag is gone.
"""

import asyncio
import email as email_lib
import imaplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Dict, List, Optional

from config.config import Settings, config
from logger import get_logger

logger = get_logger("email_utils")


class SendOutcome(str, Enum):
    """Why a send did or did not happen.

    Callers must distinguish these: the prototype's boolean let a suppressed send be recorded as
    `EMAIL_SENT`, so the dashboard reported a campaign that never left the building.
    """

    SENT = "sent"
    BLOCKED = "blocked"  # refused by SEND_MODE / SEND_ALLOWLIST — not an error
    FAILED = "failed"  # SMTP rejected it or the connection broke


def is_send_allowed(to_email: str, settings: Optional[Settings] = None) -> bool:
    """The single send decision. `True` only if this recipient may legitimately be emailed now.

    - `disabled` (the default): nothing may be sent, to anybody.
    - `allowlist`: only addresses in `SEND_ALLOWLIST`, compared case-insensitively.
    - `live`: anybody. Set only by the operator, only in production.
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


async def send_email(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
) -> SendOutcome:
    """Send one email, if and only if `is_send_allowed` permits it.

    Returns a `SendOutcome` rather than a bool so that a blocked send can never be mistaken for a
    successful one.
    """
    if not is_send_allowed(to_email, config):
        logger.warning(
            "Send refused by SEND_MODE=%s: recipient not permitted | Subject: %s",
            config.SEND_MODE,
            subject,
        )
        return SendOutcome.BLOCKED

    msg = MIMEMultipart("alternative") if body_html else MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_EMAIL
    msg["To"] = to_email
    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    try:
        import aiosmtplib
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise RuntimeError("aiosmtplib is not installed; install it to send emails") from exc

    try:
        await aiosmtplib.send(
            msg,
            hostname=config.SMTP_HOST,
            port=config.SMTP_PORT,
            username=config.SMTP_EMAIL,
            password=config.SMTP_PASSWORD,
            use_tls=config.SMTP_USE_TLS,
            start_tls=config.SMTP_START_TLS if not config.SMTP_USE_TLS else False,
            timeout=config.SMTP_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return SendOutcome.FAILED

    logger.info("Email sent to: %s | Subject: %s", to_email, subject)
    return SendOutcome.SENT


def _check_inbox_sync(check_from_emails: Optional[List[str]] = None) -> List[Dict]:
    """Synchronous IMAP inbox check. Runs in a thread executor.

    Known defects, deliberately left for the reply-engine phase (issues B10, B11): `fetch(RFC822)`
    marks mail `\\Seen`, matching is by sender address only, and quoted text is not stripped.
    """
    replies = []

    try:
        mail = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
        mail.login(config.IMAP_EMAIL, config.IMAP_PASSWORD)
        mail.select("INBOX")

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            return replies

        message_ids = messages[0].split()
        logger.info("Found %d unread messages", len(message_ids))

        for msg_id in message_ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)

                from_email = email_lib.utils.parseaddr(msg.get("From", ""))[1].lower()
                subject = msg.get("Subject", "")
                message_id = msg.get("Message-ID", "")

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")

                if check_from_emails:
                    if from_email not in [e.lower() for e in check_from_emails]:
                        continue

                replies.append(
                    {
                        "from_email": from_email,
                        "subject": subject,
                        "body": body.strip(),
                        "message_id": message_id,
                    }
                )

                mail.store(msg_id, "+FLAGS", "\\Seen")

            except Exception as e:
                logger.warning("Failed to parse message %s: %s", msg_id, str(e))
                continue

        mail.logout()

    except imaplib.IMAP4.error as e:
        logger.error("IMAP connection error: %s", str(e))
    except Exception as e:
        logger.error("Inbox check failed: %s", str(e))

    return replies


async def check_inbox(check_from_emails: Optional[List[str]] = None) -> List[Dict]:
    """Async wrapper around the synchronous IMAP check."""
    return await asyncio.get_running_loop().run_in_executor(
        None, _check_inbox_sync, check_from_emails
    )
