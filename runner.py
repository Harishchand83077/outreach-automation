"""
workflows/runner.py — Parallel execution engine for all leads.

Key Design:
- Uses asyncio.gather() to run all leads concurrently
- Limits concurrency with asyncio.Semaphore (max 5 by default)
- Each lead gets its own LangGraph workflow instance
- Graph is invoked with ainvoke() — fully async
"""

import asyncio
from typing import List, Dict, Any

from graph import get_graph
from state import LeadState
from database import update_lead
from config.config import config
from logger import get_logger

logger = get_logger("runner")

# Semaphore to limit concurrent leads (prevents API rate limits & resource exhaustion)
_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_LEADS)


async def run_lead_workflow(lead: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run the complete LangGraph workflow for a single lead.

    Args:
        lead: Dict with keys: name, email, company

    Returns:
        Final LeadState dict after workflow completion.
    """
    name = lead["name"]
    email = lead["email"]
    company = lead["company"]

    # Build initial state
    initial_state: LeadState = {
        "name": name,
        "email": email,
        "company": company,
        "insights": None,
        "insights_feedback": None,
        "insights_attempts": 0,
        "email_draft": None,
        "email_feedback": None,
        "email_attempts": 0,
        "email_sent": False,
        "status": "INIT",
        "reply": None,
        "classification": None,
        "meeting_booked": False,
        "pre_meeting_doc": None,
        "human_response": None,
        "error": None,
        # These fields are set by nodes:
        "approved_insights": False,
        "approved_email": False,
    }

    # Acquire semaphore slot before starting
    async with _semaphore:
        logger.info("[%s]  Starting workflow (semaphore acquired)", name)
        try:
            graph = get_graph()
            # ainvoke() is the async version — does NOT block the event loop
            final_state = await graph.ainvoke(initial_state)
            logger.info(
                "[%s]  Workflow finished. Status: %s",
                name,
                final_state.get("status", "UNKNOWN"),
            )
            # Sync DB when workflow reports EMAIL_SENT (dummy graph skips send_email_node)
            if final_state.get("status") == "EMAIL_SENT":
                await update_lead(
                    email=email,
                    status="EMAIL_SENT",
                    insights=final_state.get("insights"),
                    email_draft=final_state.get("email_draft"),
                )
            return final_state
        except Exception as e:
            logger.error("[%s]  Workflow crashed: %s", name, str(e), exc_info=True)
            # Update DB with error status
            await update_lead(email=email, status="ERROR", email_draft=str(e))
            return {**initial_state, "status": "ERROR", "error": str(e)}


async def run_all_leads(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Run LangGraph workflows for ALL leads in parallel.

    Uses asyncio.gather() for concurrent execution.
    Concurrency is limited by the semaphore in run_lead_workflow().

    Args:
        leads: List of lead dicts from DB or Excel.

    Returns:
        List of final states for all leads.
    """
    if not leads:
        logger.warning("No leads to process.")
        return []

    logger.info(
        " Starting parallel execution for %d leads (max concurrent: %d)",
        len(leads),
        config.MAX_CONCURRENT_LEADS,
    )

    # Create async tasks for all leads
    tasks = [run_lead_workflow(lead) for lead in leads]

    # Run all concurrently — asyncio.gather handles scheduling
    results = await asyncio.gather(*tasks, return_exceptions=False)

    logger.info(" All %d lead workflows completed", len(leads))
    return list(results)
