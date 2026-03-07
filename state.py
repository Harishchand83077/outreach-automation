"""
workflows/state.py — TypedDict state for the LangGraph per-lead workflow.

Each field corresponds to data collected or generated throughout
the lead processing pipeline.
"""

from typing import Optional
from typing_extensions import TypedDict


class LeadState(TypedDict):
    """
    State object passed between LangGraph nodes for each lead.

    All fields are optional so nodes can safely read/write incrementally.
    """

    # === Lead Identity ===
    name: str
    email: str
    company: str

    # === Research Phase ===
    insights: Optional[str]           # Generated company insights
    insights_feedback: Optional[str]  # Human feedback if rejected
    insights_attempts: int            # Number of insight regeneration attempts
    approved_insights: bool            # Human approved insights (for routing)

    # === Email Phase ===
    email_draft: Optional[str]        # Generated email draft
    email_feedback: Optional[str]     # Human feedback if rejected
    email_attempts: int               # Number of email regeneration attempts
    approved_email: bool               # Human approved email (for routing)

    # === Outreach Phase ===
    email_sent: bool                  # Whether email was sent
    status: str                       # Current lead status

    # === Reply Phase ===
    reply: Optional[str]              # Received reply text
    classification: Optional[str]    # positive / question / no_interest

    # === Post-Reply ===
    meeting_booked: bool
    pre_meeting_doc: Optional[str]
    human_response: Optional[str]     # Human-written response for questions

    # === Error tracking ===
    error: Optional[str]
