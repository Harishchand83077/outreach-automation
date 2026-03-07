"""
workers/decision_engine.py — Background Worker 2: Decision Engine

Runs asynchronously every N minutes.
Processes leads with status = REPLIED by:

1. Classifying the reply with LLM (positive / question / no_interest)
2. Taking appropriate action based on classification:
   - POSITIVE  → Send confirmation email + generate pre-meeting doc
   - QUESTION  → Route to human via CLI → send human reply
   - NO_INTEREST → Close the lead
"""

import asyncio
from typing import Dict, Any

from database import get_leads_by_status, update_lead
from llm_client import llm_call
from email_utils import send_email
from logger import get_logger
from config.config import config

logger = get_logger("decision_engine")


# ─────────────────────────────────────────────
# REPLY CLASSIFICATION
# ─────────────────────────────────────────────

async def classify_reply(lead: Dict[str, Any]) -> str:
    """
    Uses LLM to classify a reply into one of:
    - positive
    - question
    - no_interest

    Returns the classification string.
    """
    name = lead["name"]
    company = lead["company"]
    reply_text = lead.get("reply", "")
    original_email = lead.get("email_draft", "")

    system_prompt = """You are an expert sales analyst specializing in outreach response classification.
Classify the reply into exactly ONE of these categories:
- positive: They show interest, want to meet, or give a positive response
- question: They have questions, objections, or need more information
- no_interest: They decline, unsubscribe, or show no interest

Respond with ONLY the category word: positive, question, or no_interest"""

    user_prompt = f"""Classify this reply to our outreach email:

Company: {company}
Contact: {name}

Original Email We Sent:
{original_email}

Their Reply:
{reply_text}

Classification (positive / question / no_interest):"""

    try:
        result = await llm_call(system_prompt, user_prompt)
        # Clean up and validate the response
        classification = result.strip().lower()

        # Ensure it's one of the valid options
        if "positive" in classification:
            return "positive"
        elif "question" in classification:
            return "question"
        elif "no_interest" in classification or "no interest" in classification:
            return "no_interest"
        else:
            logger.warning(
                "[%s] Unexpected classification '%s', defaulting to 'question'",
                name, classification
            )
            return "question"
    except Exception as e:
        logger.error("[%s] Classification failed: %s", name, str(e))
        return "question"  # Safe default


# ─────────────────────────────────────────────
# ACTION: POSITIVE REPLY
# ─────────────────────────────────────────────

async def handle_positive(lead: Dict[str, Any]) -> None:
    """
    Handle a positive reply:
    1. Send a meeting confirmation email with calendar link
    2. Generate a pre-meeting knowledge document
    3. Update DB: status=MEETING_BOOKED
    """
    name = lead["name"]
    email = lead["email"]
    company = lead["company"]
    reply_text = lead.get("reply", "")

    logger.info("[%s] POSITIVE reply! Booking meeting...", name)

    # ── 1. Send confirmation email ──
    confirmation_subject = f"Re: Looking forward to connecting, {name}!"
    confirmation_body = f"""Hi {name},

Thank you for your interest! I'd love to connect and explore how we can work together.

Please use the link below to book a time that works best for you:
{config.CALENDAR_LINK}

I look forward to our conversation!

Best regards
"""
    await send_email(
        to_email=email,
        subject=confirmation_subject,
        body_html=confirmation_body.replace("\n", "<br>"),
        body_text=confirmation_body,
    )

    # ── 2. Generate pre-meeting document ──
    logger.info("[%s] Generating pre-meeting document...", name)

    system_prompt = """You are a business development expert preparing for an important investor/partner meeting.
Create a comprehensive pre-meeting briefing document. Be specific and strategic."""

    user_prompt = f"""Create a pre-meeting knowledge document for an upcoming meeting with:

Contact: {name}
Company: {company}

Their reply to our outreach:
{reply_text}

Our company research insights:
{lead.get('insights', 'Not available')}

Generate a pre-meeting document covering:
1. **Meeting Objective**: What we want to achieve
2. **Company Background**: Key facts about {company}
3. **Talking Points**: 5 key points to cover
4. **Questions to Ask Them**: 5 strategic questions
5. **Anticipated Objections & Responses**: Common objections and our responses
6. **Next Steps**: What we're hoping to agree on
7. **Materials to Bring**: Documents, demos, or data to prepare

Format this as a professional meeting brief."""

    try:
        pre_meeting_doc = await llm_call(system_prompt, user_prompt)
    except Exception as e:
        pre_meeting_doc = f"Error generating document: {str(e)}"
        logger.error("[%s] Failed to generate pre-meeting doc: %s", name, str(e))

    # ── 3. Update DB ──
    await update_lead(
        email=email,
        status="MEETING_BOOKED",
        classification="positive",
        meeting_booked=1,
        pre_meeting_doc=pre_meeting_doc,
    )

    logger.info("[%s] Meeting booked, confirmation sent, pre-meeting doc saved", name)
    print(f"\n{'=' * 70}")
    print(f"PRE-MEETING DOCUMENT — {name} @ {company}")
    print("=" * 70)
    print(pre_meeting_doc)
    print("=" * 70)


# ─────────────────────────────────────────────
# ACTION: QUESTION / OBJECTION
# ─────────────────────────────────────────────

async def handle_question(lead: Dict[str, Any]) -> None:
    """
    Handle a question/objection reply:
    1. Show reply to human via CLI
    2. Human writes a response
    3. Send human's response
    4. Update DB: status=WAITING_REPLY
    """
    name = lead["name"]
    email = lead["email"]
    company = lead["company"]
    reply_text = lead.get("reply", "")

    print(f"\n{'=' * 70}")
    print(f"QUESTION/OBJECTION from {name} @ {company}")
    print("=" * 70)
    print("Their reply:")
    print(reply_text)
    print("=" * 70)

    loop = asyncio.get_event_loop()

    print("\nPlease write your response (press Enter twice when done):")
    lines = []
    while True:
        line = await loop.run_in_executor(None, input)
        if line == "" and lines and lines[-1] == "":
            break
        lines.append(line)

    human_response = "\n".join(lines[:-1] if lines and lines[-1] == "" else lines).strip()

    if not human_response:
        logger.warning("[%s] No response provided, skipping.", name)
        return

    # Send the human's reply
    subject = f"Re: Following up — {company}"
    await send_email(
        to_email=email,
        subject=subject,
        body_html=human_response.replace("\n", "<br>"),
        body_text=human_response,
    )

    # Update DB
    await update_lead(
        email=email,
        status="WAITING_REPLY",
        classification="question",
    )

    logger.info("[%s] Human response sent. Continuing to monitor.", name)


# ─────────────────────────────────────────────
# ACTION: NO INTEREST
# ─────────────────────────────────────────────

async def handle_no_interest(lead: Dict[str, Any]) -> None:
    """
    Handle a no-interest reply: mark as CLOSED.
    """
    name = lead["name"]
    email = lead["email"]

    await update_lead(
        email=email,
        status="CLOSED",
        classification="no_interest",
    )

    logger.info("[%s] Lead closed (no interest)", name)


# ─────────────────────────────────────────────
# MAIN DECISION ENGINE PASS
# ─────────────────────────────────────────────

async def process_replied_leads() -> int:
    """
    Single pass: process all leads with status=REPLIED.

    Returns:
        int: Number of leads processed.
    """
    replied_leads = await get_leads_by_status("REPLIED")

    if not replied_leads:
        logger.debug("No leads with REPLIED status. Skipping.")
        return 0

    logger.info("Decision engine processing %d replied lead(s)...", len(replied_leads))
    count = 0

    for lead in replied_leads:
        name = lead["name"]
        logger.info("[%s] Classifying reply...", name)

        try:
            # Step 1: Classify the reply
            classification = await classify_reply(lead)
            logger.info("[%s] Classification: %s", name, classification)

            # Step 2: Take action based on classification
            if classification == "positive":
                await handle_positive(lead)
            elif classification == "question":
                await handle_question(lead)
            elif classification == "no_interest":
                await handle_no_interest(lead)

            count += 1

        except Exception as e:
            logger.error(
                "[%s] Decision engine error: %s", name, str(e), exc_info=True
            )

    return count


async def decision_engine_loop():
    """
    Infinite async loop that periodically processes replied leads.
    Runs at the same interval as the reply monitor.
    """
    interval_seconds = config.REPLY_CHECK_INTERVAL_MINUTES * 60
    logger.info(
        "Decision engine started. Running every %d minutes.",
        config.REPLY_CHECK_INTERVAL_MINUTES,
    )

    # Slight delay offset from reply monitor to avoid race conditions
    await asyncio.sleep(30)

    while True:
        try:
            processed = await process_replied_leads()
            if processed > 0:
                logger.info("Decision engine: processed %d lead(s)", processed)
        except Exception as e:
            logger.error("Decision engine loop error: %s", str(e), exc_info=True)

        await asyncio.sleep(interval_seconds)
