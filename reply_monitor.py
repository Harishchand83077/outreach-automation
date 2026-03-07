"""
workers/reply_monitor.py — Background Worker 1: Reply Monitor

Runs asynchronously every N minutes.
Checks the email inbox for new replies from leads.
When a reply is found, updates the lead's status in the DB.

This is intentionally SEPARATE from the LangGraph workflow —
the workflow ends after sending email, and this picks up from there.
"""

import asyncio
from typing import List

from database import get_leads_by_status, update_lead
from email_utils import check_inbox
from logger import get_logger
from config.config import config

logger = get_logger("reply_monitor")


async def check_and_store_replies() -> int:
    """
    Single pass: check inbox and store any new replies.

    Returns:
        int: Number of new replies found and stored.
    """
    # Get all leads awaiting replies
    sent_leads = await get_leads_by_status("EMAIL_SENT")

    if not sent_leads:
        logger.debug("No leads in EMAIL_SENT status. Skipping inbox check.")
        return 0

    # Build list of expected sender emails
    lead_emails = [lead["email"].lower() for lead in sent_leads]
    email_to_lead = {lead["email"].lower(): lead for lead in sent_leads}

    logger.info(
        "Checking inbox for replies from %d leads...", len(lead_emails)
    )

    # Check inbox for replies from known lead emails
    replies = await check_inbox(check_from_emails=lead_emails)

    if not replies:
        logger.info("No new replies found.")
        return 0

    count = 0
    for reply in replies:
        from_email = reply["from_email"].lower()
        lead = email_to_lead.get(from_email)

        if not lead:
            logger.debug("Reply from unknown sender: %s (skipping)", from_email)
            continue

        name = lead["name"]
        body = reply["body"]

        logger.info(
            "[%s] Reply received from %s | Subject: %s",
            name, from_email, reply["subject"]
        )

        # Update the lead in DB with reply content
        await update_lead(
            email=from_email,
            status="REPLIED",
            reply=body,
        )

        logger.info("[%s] DB updated: status=REPLIED", name)
        count += 1

    return count


async def reply_monitor_loop():
    """
    Infinite async loop that periodically checks for new replies.
    Runs every REPLY_CHECK_INTERVAL_MINUTES minutes.

    Designed to run as a background asyncio task — does not block.
    """
    interval_seconds = config.REPLY_CHECK_INTERVAL_MINUTES * 60
    logger.info(
        "Reply monitor started. Checking every %d minutes.",
        config.REPLY_CHECK_INTERVAL_MINUTES,
    )

    while True:
        try:
            found = await check_and_store_replies()
            if found > 0:
                logger.info("Reply monitor: %d new replies stored", found)
        except Exception as e:
            logger.error("Reply monitor error: %s", str(e), exc_info=True)

        # Wait before next check — non-blocking
        logger.debug("Reply monitor sleeping for %ds...", interval_seconds)
        await asyncio.sleep(interval_seconds)
