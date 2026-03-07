"""
workflows/graph.py — LangGraph workflow definition for per-lead outreach.

Architecture:
  START
    → generate_insights
    → human_validate_insights
        ├─ rejected → generate_insights (loop)
        └─ approved → generate_email
    → generate_email
    → human_validate_email
        ├─ rejected → generate_email (loop)
        └─ approved → send_email
    → send_email
    → end_node
  END

IMPORTANT: The workflow ends after sending the email.
Reply monitoring is done by a SEPARATE background worker — NOT here.
"""

try:
    from langgraph.graph import StateGraph, END  # type: ignore
    _HAS_LANGGRAPH = True
except Exception:
    StateGraph = None
    END = None
    _HAS_LANGGRAPH = False

from state import LeadState
from logger import get_logger

logger = get_logger("graph")


# ─────────────────────────────────────────────
# CONDITIONAL EDGE FUNCTIONS
# ─────────────────────────────────────────────

def route_insights(state: LeadState) -> str:
    """
    Route after human_validate_insights.
    - If approved → proceed to generate_email
    - If rejected → loop back to generate_insights
    """
    approved = state.get("approved_insights", False)
    if approved:
        return "generate_email"
    else:
        return "generate_insights"


def route_email(state: LeadState) -> str:
    """
    Route after human_validate_email.
    - If approved → proceed to send_email_node
    - If rejected → loop back to generate_email
    """
    approved = state.get("approved_email", False)
    if approved:
        return "send_email"
    else:
        return "generate_email"


# ─────────────────────────────────────────────
# GRAPH CONSTRUCTION
# ─────────────────────────────────────────────

def build_outreach_graph():
    """Build and return the compiled LangGraph outreach graph.
    Requires langgraph and dependencies; fails with clear error if not installed.
    """
    if not _HAS_LANGGRAPH or StateGraph is None:
        raise RuntimeError(
            "LangGraph is required for the full workflow. Install with:\n"
            "  pip install -r requirements.txt"
        )

    from nodes import (
        generate_insights,
        human_validate_insights,
        generate_email,
        human_validate_email,
        send_email_node,
        end_node,
    )

    workflow = StateGraph(LeadState)
    workflow.add_node("generate_insights", generate_insights)
    workflow.add_node("human_validate_insights", human_validate_insights)
    workflow.add_node("generate_email", generate_email)
    workflow.add_node("human_validate_email", human_validate_email)
    workflow.add_node("send_email", send_email_node)
    workflow.add_node("end_node", end_node)
    workflow.set_entry_point("generate_insights")
    workflow.add_edge("generate_insights", "human_validate_insights")
    workflow.add_edge("generate_email", "human_validate_email")
    workflow.add_edge("send_email", "end_node")
    workflow.add_edge("end_node", END)
    workflow.add_conditional_edges("human_validate_insights", route_insights, {
        "generate_email": "generate_email",
        "generate_insights": "generate_insights",
    })
    workflow.add_conditional_edges("human_validate_email", route_email, {
        "send_email": "send_email",
        "generate_email": "generate_email",
    })
    compiled = workflow.compile()
    logger.info("LangGraph outreach workflow compiled successfully")
    return compiled


# Singleton compiled graph (shared across all parallel lead runs)
_graph = None


def get_graph() -> StateGraph:
    """Return the singleton compiled graph."""
    global _graph
    if _graph is None:
        _graph = build_outreach_graph()
    return _graph
