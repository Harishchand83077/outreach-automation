"""
workflows/nodes.py — All LangGraph node functions for the outreach pipeline.

Nodes:
1. generate_insights      — LLM researches the company
2. human_validate_insights — CLI approval of insights
3. generate_email         — LLM writes personalized email
4. human_validate_email   — CLI approval of email draft
5. send_email_node        — Sends email via SMTP
6. end_node               — Terminal node, updates DB

Each node receives the full LeadState and returns a partial dict
with only the fields it updates.
"""

import asyncio
import os
from typing import Dict, Any

from state import LeadState
from llm_client import llm_call
from email_utils import send_email
from logger import get_logger
from config.config import config
from database import update_lead

logger = get_logger("nodes")

# ─────────────────────────────────────────────
# 1. GENERATE INSIGHTS
# ─────────────────────────────────────────────

async def generate_insights(state: LeadState) -> Dict[str, Any]:
    """
    Uses LLM to research the company and generate structured insights
    about their funding potential and fit.
    """
    name = state["name"]
    company = state["company"]
    feedback = state.get("insights_feedback") or ""
    attempts = state.get("insights_attempts", 0)

    logger.info("[%s] Generating insights (attempt %d)...", name, attempts + 1)

    feedback_section = f"\nPrevious feedback to incorporate: {feedback}" if feedback else ""

    system_prompt = """You are an expert startup and venture capital analyst.
Your job is to analyze a company and generate structured insights to help 
determine if they would be a good funding target or strategic partner.
Be specific, concise, and actionable. Format output in clear sections."""

    user_prompt = f"""Research and analyze this company for potential funding outreach:

Company: {company}
Contact: {name}
{feedback_section}

Generate insights covering:
1. **Company Overview**: What does {company} do? Core product/service.
2. **Market Position**: Industry sector, market size, competitive landscape.
3. **Funding Potential**: Likely stage (seed/Series A/B/C), estimated funding needs.
4. **Strategic Fit**: Why they might want to fund us / partner with us.
5. **Key Pain Points**: Problems they likely face that we can solve.
6. **Outreach Angle**: The most compelling reason to reach out to them.
7. **Risk Factors**: Any concerns or red flags.

Be specific and actionable. Base analysis on the company name and industry."""

    try:
        insights = await llm_call(system_prompt, user_prompt)
        logger.info("[%s] Insights generated (%d chars)", name, len(insights))
        return {
            "insights": insights,
            "insights_attempts": attempts + 1,
            "status": "INSIGHTS_GENERATED",
        }
    except Exception as e:
        logger.error("[%s] Failed to generate insights: %s", name, str(e))
        return {
            "insights": f"Error generating insights: {str(e)}",
            "insights_attempts": attempts + 1,
            "error": str(e),
        }


# ─────────────────────────────────────────────
# 2. HUMAN VALIDATE INSIGHTS
# ─────────────────────────────────────────────

async def human_validate_insights(state: LeadState) -> Dict[str, Any]:
    """
    CLI-based human review of generated insights.
    Returns approved=True or provides feedback for regeneration.
    """
    name = state["name"]
    company = state["company"]
    insights = state.get("insights", "")

    print("\n" + "=" * 70)
    print(f"INSIGHTS REVIEW — {name} @ {company}")
    print("=" * 70)
    print(insights)
    print("=" * 70)
    # Auto-approve when running non-interactively (useful for CI/test)
    if os.getenv("AUTO_APPROVE", ""):
        logger.info("[%s] Auto-approving insights (AUTO_APPROVE set)", name)
        return {
            "approved_insights": True,
            "insights_feedback": None,
            "status": "INSIGHTS_APPROVED",
        }

    # Run blocking input() in a thread executor so we don't block the event loop
    loop = asyncio.get_event_loop()

    decision = await loop.run_in_executor(
        None,
        lambda: input("\nApprove insights? (y/n): ").strip().lower()
    )

    if decision == "y":
        logger.info("[%s] Insights approved by human", name)
        return {
            "approved_insights": True,
            "insights_feedback": None,
            "status": "INSIGHTS_APPROVED",
        }
    else:
        feedback = await loop.run_in_executor(
            None,
            lambda: input("Provide feedback for regeneration: ").strip()
        )
        logger.info("[%s] Insights rejected. Feedback: %s", name, feedback)
        return {
            "approved_insights": False,
            "insights_feedback": feedback,
            "status": "INSIGHTS_REJECTED",
        }


# ─────────────────────────────────────────────
# 3. GENERATE EMAIL
# ─────────────────────────────────────────────

async def generate_email(state: LeadState) -> Dict[str, Any]:
    """
    Uses LLM to write a personalized outreach email based on
    the approved insights.
    """
    name = state["name"]
    company = state["company"]
    email_addr = state["email"]
    insights = state.get("insights", "")
    feedback = state.get("email_feedback") or ""
    attempts = state.get("email_attempts", 0)

    logger.info("[%s] Generating email draft (attempt %d)...", name, attempts + 1)

    feedback_section = f"\nPrevious feedback to incorporate: {feedback}" if feedback else ""

    system_prompt = """You are an expert business development and outreach specialist.
Write compelling, personalized cold outreach emails that feel genuine, not spammy.
The email should be concise (under 200 words), professional, and have a clear CTA.
Write in plain text with minimal formatting — no markdown in the email body."""

    user_prompt = f"""Write a personalized funding/partnership outreach email using these details:

Contact Name: {name}
Company: {company}
Email: {email_addr}
Calendar Link: {config.CALENDAR_LINK}
{feedback_section}

Company Research Insights:
{insights}

Email Requirements:
1. Subject line (write it as "Subject: ...")
2. Personalized opening that references something specific about {company}
3. Brief, compelling value proposition (what we offer)
4. 1-2 sentence description of our product/service and traction
5. Clear CTA with calendar link: {config.CALENDAR_LINK}
6. Professional signature

Format:
Subject: [subject line]

[email body]

Keep it under 200 words. Sound human, not like a template."""

    try:
        email_draft = await llm_call(system_prompt, user_prompt)
        logger.info("[%s] Email draft generated (%d chars)", name, len(email_draft))
        return {
            "email_draft": email_draft,
            "email_attempts": attempts + 1,
            "status": "EMAIL_DRAFTED",
        }
    except Exception as e:
        logger.error("[%s] Failed to generate email: %s", name, str(e))
        return {
            "email_draft": f"Error generating email: {str(e)}",
            "email_attempts": attempts + 1,
            "error": str(e),
        }


# ─────────────────────────────────────────────
# 4. HUMAN VALIDATE EMAIL
# ─────────────────────────────────────────────

async def human_validate_email(state: LeadState) -> Dict[str, Any]:
    """
    CLI-based human review of generated email draft.
    """
    name = state["name"]
    company = state["company"]
    email_draft = state.get("email_draft", "")

    print("\n" + "=" * 70)
    print(f"EMAIL DRAFT REVIEW — {name} @ {company}")
    print("=" * 70)
    print(email_draft)
    print("=" * 70)

    # Auto-approve when running non-interactively
    if os.getenv("AUTO_APPROVE", ""):
        logger.info("[%s] Auto-approving email (AUTO_APPROVE set)", name)
        return {
            "approved_email": True,
            "email_feedback": None,
            "status": "EMAIL_APPROVED",
        }

    loop = asyncio.get_event_loop()

    decision = await loop.run_in_executor(
        None,
        lambda: input("\nApprove email to send? (y/n): ").strip().lower()
    )

    if decision == "y":
        logger.info("[%s] Email approved by human", name)
        return {
            "approved_email": True,
            "email_feedback": None,
            "status": "EMAIL_APPROVED",
        }
    else:
        feedback = await loop.run_in_executor(
            None,
            lambda: input("Provide feedback for regeneration: ").strip()
        )
        logger.info("[%s] Email rejected. Feedback: %s", name, feedback)
        return {
            "approved_email": False,
            "email_feedback": feedback,
            "status": "EMAIL_REJECTED",
        }


# ─────────────────────────────────────────────
# 5. SEND EMAIL NODE
# ─────────────────────────────────────────────

async def send_email_node(state: LeadState) -> Dict[str, Any]:
    """
    Parses the email draft and sends it via SMTP.
    Updates the DB after sending.
    """
    name = state["name"]
    to_email = state["email"]
    company = state["company"]
    email_draft = state.get("email_draft", "")

    # Parse subject line from draft (format: "Subject: ...")
    subject = f"Partnership Opportunity — {company}"
    body = email_draft

    lines = email_draft.strip().split("\n")
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()

    # Build HTML version
    html_body = body.replace("\n", "<br>")
    html_body = f"""
    <html><body>
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        {html_body}
        <br><br>
        <hr style="border: none; border-top: 1px solid #eee;">
        <p style="color: #999; font-size: 12px;">
            Book a time: <a href="{config.CALENDAR_LINK}">{config.CALENDAR_LINK}</a>
        </p>
    </div>
    </html></body>
    """

    logger.info("[%s] Sending email to: %s", name, to_email)

    success = await send_email(
        to_email=to_email,
        subject=subject,
        body_html=html_body,
        body_text=body,
    )

    if success:
        # Persist to DB
        await update_lead(
            email=to_email,
            status="EMAIL_SENT",
            insights=state.get("insights"),
            email_draft=email_draft,
        )
        logger.info("[%s] Email sent and DB updated", name)
        return {
            "email_sent": True,
            "status": "EMAIL_SENT",
        }
    else:
        logger.error("[%s] Email send failed", name)
        return {
            "email_sent": False,
            "status": "EMAIL_FAILED",
            "error": "SMTP send failed",
        }


# ─────────────────────────────────────────────
# 6. END NODE
# ─────────────────────────────────────────────

async def end_node(state: LeadState) -> Dict[str, Any]:
    """
    Terminal node. Logs completion and returns final state.
    The LangGraph workflow ends here.
    Reply monitoring is handled by a SEPARATE background worker.
    """
    name = state["name"]
    status = state.get("status", "UNKNOWN")
    logger.info("[%s] Workflow complete. Final status: %s", name, status)
    return {"status": status}
