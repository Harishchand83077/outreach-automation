"""
utils/email_utils.py — Async email sending via SMTP (Gmail).

Handles:
- Sending HTML emails via aiosmtplib
- Inbox checking via imaplib (sync, run in executor)
- Reply detection by subject/thread matching
"""

import asyncio
import imaplib
import email as email_lib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, List, Dict

from config.config import config
from logger import get_logger
import os

logger = get_logger("email_utils")


async def send_email(
    to_email: str,
    subject: str,
    body_html: str,
    body_text: Optional[str] = None,
) -> bool:
    """
    Send an email asynchronously via SMTP.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        body_html: HTML body of the email.
        body_text: Optional plain-text fallback.

    Returns:
        bool: True if sent successfully, False otherwise.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.SMTP_EMAIL
    msg["To"] = to_email

    # Attach plain text fallback
    if body_text:
        msg.attach(MIMEText(body_text, "plain"))

    # Attach HTML content
    msg.attach(MIMEText(body_html, "html"))

    try:
        # DRY RUN support: if DRY_RUN_EMAILS=1 then just log and return success
        if os.getenv("DRY_RUN_EMAILS", ""):
            logger.info("[DRY_RUN] Simulating send to: %s | Subject: %s", to_email, subject)
            return True

        try:
            import aiosmtplib
        except Exception:
            raise RuntimeError("aiosmtplib is not installed; install it to send emails")

        use_tls = config.SMTP_USE_TLS
        start_tls = not use_tls
        await aiosmtplib.send(
            msg,
            hostname=config.SMTP_HOST,
            port=config.SMTP_PORT,
            username=config.SMTP_EMAIL,
            password=config.SMTP_PASSWORD,
            use_tls=use_tls,
            start_tls=start_tls,
            timeout=config.SMTP_TIMEOUT,
        )
        logger.info("Email sent to: %s | Subject: %s", to_email, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to_email, str(e))
        return False


def _check_inbox_sync(check_from_emails: Optional[List[str]] = None) -> List[Dict]:
    """
    Synchronous IMAP inbox check.
    Returns list of reply dicts: {from_email, subject, body, message_id}

    This runs in a thread executor to avoid blocking the event loop.
    """
    replies = []

    try:
        mail = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
        mail.login(config.IMAP_EMAIL, config.IMAP_PASSWORD)
        mail.select("INBOX")

        # Search for UNSEEN emails
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

                # Extract body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(errors="replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(errors="replace")

                # Filter by specific senders if provided
                if check_from_emails:
                    if from_email not in [e.lower() for e in check_from_emails]:
                        continue

                replies.append({
                    "from_email": from_email,
                    "subject": subject,
                    "body": body.strip(),
                    "message_id": message_id,
                })

                # Mark as read
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
    """
    Async wrapper around the synchronous IMAP check.
    Runs in a thread pool executor to avoid blocking.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _check_inbox_sync, check_from_emails)
