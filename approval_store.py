"""
approval_store.py — In-memory store for human-in-the-loop approvals when APPROVAL_MODE=api.

When the workflow reaches human_validate_insights or human_validate_email and APPROVAL_MODE=api,
the node registers a pending approval and awaits approval_store.wait_approval().
The API (called by the frontend) calls approve_insights() / reject_insights() or approve_email() / reject_email(),
which sets the result and unblocks the waiting node.
"""

import asyncio
from typing import Optional, Dict, Any

from logger import get_logger

logger = get_logger("approval_store")

# Key: (email_lower, "insights" | "email") -> {"event": asyncio.Event, "result": None | dict, "payload": dict}
_pending: Dict[tuple, Dict[str, Any]] = {}
_lock = asyncio.Lock()


def _key(email: str, approval_type: str) -> tuple:
    return (email.strip().lower(), approval_type)


async def wait_approval(
    email: str,
    approval_type: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Register a pending approval and wait for the API to resolve it.

    Args:
        email: Lead email (used as id).
        approval_type: "insights" or "email".
        payload: Optional dict to expose via get_pending_async (e.g. name, company, insights or email_draft).
        timeout: Optional max seconds to wait (None = wait forever).

    Returns:
        {"approved": bool, "feedback": str | None}
    """
    k = _key(email, approval_type)
    async with _lock:
        if k in _pending:
            raise RuntimeError(f"Already pending approval for {email} ({approval_type})")
        _pending[k] = {"event": asyncio.Event(), "result": None, "payload": payload or {}}
    try:
        if timeout:
            await asyncio.wait_for(_pending[k]["event"].wait(), timeout=timeout)
        else:
            await _pending[k]["event"].wait()
        return _pending[k]["result"] or {"approved": False, "feedback": None}
    except asyncio.TimeoutError:
        logger.warning("[%s] Approval timeout (%s)", email, approval_type)
        return {"approved": False, "feedback": "Approval timeout"}
    finally:
        async with _lock:
            _pending.pop(k, None)


def _resolve(
    email: str,
    approval_type: str,
    approved: bool,
    feedback: Optional[str] = None,
    *,
    edited_insights: Optional[str] = None,
    edited_email_draft: Optional[str] = None,
    response_text: Optional[str] = None,
) -> bool:
    """Set the result and unblock the waiter. Returns True if there was a pending approval."""
    k = _key(email, approval_type)
    if k not in _pending:
        return False
    result = {"approved": approved, "feedback": feedback or None}
    if edited_insights is not None:
        result["edited_insights"] = edited_insights
    if edited_email_draft is not None:
        result["edited_email_draft"] = edited_email_draft
    if response_text is not None:
        result["response_text"] = response_text
    _pending[k]["result"] = result
    _pending[k]["event"].set()
    return True


def approve_insights(email: str, edited_insights: Optional[str] = None) -> bool:
    return _resolve(email, "insights", True, None, edited_insights=edited_insights)


def reject_insights(email: str, feedback: str = "") -> bool:
    return _resolve(email, "insights", False, feedback)


def approve_email(email: str, edited_email_draft: Optional[str] = None) -> bool:
    return _resolve(email, "email", True, None, edited_email_draft=edited_email_draft)


def reject_email(email: str, feedback: str = "") -> bool:
    return _resolve(email, "email", False, feedback)


def approve_question_reply(email: str, response_text: str = "") -> bool:
    """Submit human response for a QUESTION-classified reply. Unblocks decision engine."""
    return _resolve(email, "question_reply", True, None, response_text=response_text)


async def get_pending_async() -> list:
    """Return list of {email, type, ...payload} for all pending approvals (for API)."""
    async with _lock:
        return [
            {"email": k[0], "type": k[1], **v.get("payload", {})}
            for k, v in list(_pending.items())
        ]
