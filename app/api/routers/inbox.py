"""Inbox — one unified view of inbound threads across all campaigns.

A thread is a lead that has received at least one inbound message.  Every thread
belongs to exactly one folder (see ``app/domain/inbox``).  Snooze and archive are
flags stored in ``inbox_flags``; they never touch ``leads.state``.

The two approval actions (Approve, Reject) go through the existing review routes
(``POST /drafts/{id}/approve`` and ``POST /drafts/{id}/reject``) — the inbox does
not duplicate that logic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, status
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep, SessionDep
from app.api.pagination import Cursor, clamp_limit
from app.db.models import Classification, Draft, InboxFlag, Lead, Message
from app.domain import inbox as inbox_domain

router = APIRouter(tags=["inbox"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INBOX_STATES = frozenset(
    {
        "REPLIED",
        "IN_CONVERSATION",
        "MEETING_BOOKED",
        "NOT_INTERESTED",
        "OPTED_OUT",
        "BOUNCED",
        "AWAITING_APPROVAL",
    }
)


def _flag_dict(flag: InboxFlag | None) -> dict[str, Any]:
    if flag is None:
        return {"snoozed_until": None, "archived_at": None}
    return {
        "snoozed_until": flag.snoozed_until.isoformat() if flag.snoozed_until else None,
        "archived_at": flag.archived_at.isoformat() if flag.archived_at else None,
    }


def _thread(
    lead: Lead,
    latest_label: str | None,
    latest_draft: Draft | None,
    flag: InboxFlag | None,
    now: datetime,
) -> dict[str, Any]:
    has_pending_draft = latest_draft is not None and latest_draft.status == "pending_review"
    folder = inbox_domain.folder(
        lead_state=lead.state,
        latest_label=latest_label,
        has_pending_draft=has_pending_draft,
        snoozed_until=flag.snoozed_until if flag else None,
        archived_at=flag.archived_at if flag else None,
        now=now,
    )
    return {
        "lead_id": str(lead.id),
        "lead_name": lead.full_name,
        "email": lead.email_norm,
        "company": lead.company_name,
        "lead_state": lead.state,
        "folder": folder,
        "latest_label": latest_label,
        "has_pending_draft": has_pending_draft,
        "pending_draft_id": str(latest_draft.id) if has_pending_draft and latest_draft else None,
        **_flag_dict(flag),
        "last_activity_at": lead.state_changed_at.isoformat(),
    }


async def _get_lead(
    lead_id: uuid.UUID,
    session: AsyncSession,
    org_id: uuid.UUID,
) -> Lead:
    row = await session.get(Lead, lead_id)
    if row is None or row.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="thread not found")
    return row


async def _latest_label(lead_id: uuid.UUID, session: AsyncSession) -> str | None:
    result = await session.execute(
        select(Classification.label)
        .where(Classification.lead_id == lead_id)
        .order_by(desc(Classification.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_pending_draft(lead_id: uuid.UUID, session: AsyncSession) -> Draft | None:
    result = await session.execute(
        select(Draft)
        .where(Draft.lead_id == lead_id, Draft.status == "pending_review")
        .order_by(desc(Draft.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_flag(
    lead_id: uuid.UUID, org_id: uuid.UUID, session: AsyncSession
) -> InboxFlag | None:
    result = await session.execute(
        select(InboxFlag).where(InboxFlag.org_id == org_id, InboxFlag.lead_id == lead_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/inbox/threads")
async def list_threads(
    user: CurrentUserDep,
    session: SessionDep,
    folder: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> dict[str, Any]:
    """All inbound threads for this workspace, grouped into folders.

    ``folder`` filters to a single folder; omit it to get all threads.  Threads
    that have never received an inbound message do not appear here.
    """
    size = clamp_limit(limit)
    position = Cursor.decode(cursor)
    now = datetime.now(tz=UTC)

    # Leads that have at least one inbound message OR are in a reply state
    leads_with_replies = (
        select(Lead.id)
        .join(Message, Message.lead_id == Lead.id)
        .where(
            Lead.org_id == user.org_id,
            Message.direction == "in",
        )
        .union(
            select(Lead.id).where(
                Lead.org_id == user.org_id,
                Lead.state.in_(list(_INBOX_STATES)),
            )
        )
        .subquery()
    )

    stmt = (
        select(Lead)
        .where(Lead.id.in_(select(leads_with_replies.c.id)))
        .order_by(desc(Lead.state_changed_at), desc(Lead.id))
    )
    if position:
        stmt = stmt.where(
            or_(
                Lead.state_changed_at < position.created_at,
                and_(Lead.state_changed_at == position.created_at, Lead.id < position.row_id),
            )
        )

    rows = (await session.execute(stmt.limit(size + 1))).scalars().all()
    has_more = len(rows) > size
    rows = rows[:size]

    # Build threads, filter by folder if requested
    threads = []
    for lead in rows:
        label = await _latest_label(lead.id, session)
        draft = await _latest_pending_draft(lead.id, session)
        flag = await _get_flag(lead.id, user.org_id, session)
        t = _thread(lead, label, draft, flag, now)
        if folder is None or t["folder"] == folder:
            threads.append(t)

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = Cursor(created_at=last.state_changed_at, row_id=last.id).encode()

    return {"threads": threads, "next_cursor": next_cursor}


@router.get("/inbox/threads/{lead_id}")
async def get_thread(
    lead_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> dict[str, Any]:
    """One thread with its full message list and pending draft."""
    now = datetime.now(tz=UTC)
    lead = await _get_lead(lead_id, session, user.org_id)
    label = await _latest_label(lead_id, session)
    draft = await _latest_pending_draft(lead_id, session)
    flag = await _get_flag(lead_id, user.org_id, session)

    # All messages for this thread (newest last — conversation order)
    msgs_result = await session.execute(
        select(Message).where(Message.lead_id == lead_id).order_by(Message.created_at)
    )
    messages = [
        {
            "id": str(m.id),
            "direction": m.direction,
            "subject": m.subject,
            "body_text": m.body_text,
            "from_addr": m.from_addr,
            "to_addr": m.to_addr,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
            "received_at": m.received_at.isoformat() if m.received_at else None,
            "status": m.status,
        }
        for m in msgs_result.scalars()
    ]

    thread = _thread(lead, label, draft, flag, now)
    thread["messages"] = messages

    if draft:
        thread["pending_draft"] = {
            "id": str(draft.id),
            "subject": draft.subject,
            "body": draft.body,
            "kind": draft.kind,
            "version": draft.version,
        }

    return thread


@router.post("/inbox/threads/{lead_id}/snooze", status_code=status.HTTP_200_OK)
async def snooze_thread(
    lead_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    until: Annotated[datetime, Body(embed=True)],
) -> dict[str, Any]:
    """Snooze a thread until a future time.  Clears the archived_at flag."""
    await _get_lead(lead_id, session, user.org_id)
    flag = await _get_flag(lead_id, user.org_id, session)

    if flag is None:
        flag = InboxFlag(
            org_id=user.org_id,
            lead_id=lead_id,
            snoozed_until=until,
            updated_by=user.email,
        )
        session.add(flag)
    else:
        flag.snoozed_until = until
        flag.archived_at = None
        flag.updated_by = user.email

    await session.commit()
    return {"snoozed_until": until.isoformat(), "archived_at": None}


@router.post("/inbox/threads/{lead_id}/archive", status_code=status.HTTP_200_OK)
async def archive_thread(
    lead_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> dict[str, Any]:
    """Archive a thread.  Clears any active snooze."""
    await _get_lead(lead_id, session, user.org_id)
    now = datetime.now(tz=UTC)
    flag = await _get_flag(lead_id, user.org_id, session)

    if flag is None:
        flag = InboxFlag(
            org_id=user.org_id,
            lead_id=lead_id,
            archived_at=now,
            updated_by=user.email,
        )
        session.add(flag)
    else:
        flag.archived_at = now
        flag.snoozed_until = None
        flag.updated_by = user.email

    await session.commit()
    return {"archived_at": now.isoformat(), "snoozed_until": None}


@router.delete("/inbox/threads/{lead_id}/archive", status_code=status.HTTP_200_OK)
async def unarchive_thread(
    lead_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> dict[str, Any]:
    """Move a thread out of archived back to its natural folder."""
    await _get_lead(lead_id, session, user.org_id)
    flag = await _get_flag(lead_id, user.org_id, session)

    if flag is not None:
        flag.archived_at = None
        flag.updated_by = user.email
        await session.commit()

    return {"archived_at": None}
