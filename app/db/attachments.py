"""Reading and writing the one file a campaign may attach.

Everything here is org-scoped through `OrgContext` (ADR-004). The blob is only ever loaded by
`load_for_send`; every other function answers questions about the file without moving it, which
is the point of keeping it in a table of its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Attachment, Campaign, Draft
from app.db.repo import OrgContext
from app.domain.attachment import CheckedFile, should_attach
from app.domain.campaign import CampaignSettings


@dataclass(frozen=True)
class AttachmentInfo:
    """What a campaign's attachment is, without its contents."""

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploaded_by: str | None
    created_at: datetime

    def as_json(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "uploaded_by": self.uploaded_by,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class OutgoingFile:
    """An attachment resolved for one specific message, ready for `build_message`."""

    filename: str
    content_type: str
    content: bytes


async def describe(
    session: AsyncSession, ctx: OrgContext, *, campaign_id: uuid.UUID
) -> AttachmentInfo | None:
    """The campaign's attachment, or `None`. Never loads `content`."""
    row = (
        await session.execute(
            select(
                Attachment.id,
                Attachment.filename,
                Attachment.content_type,
                Attachment.size_bytes,
                Attachment.sha256,
                Attachment.uploaded_by,
                Attachment.created_at,
            ).where(
                Attachment.org_id == ctx.org_id,
                Attachment.campaign_id == campaign_id,
            )
        )
    ).first()
    if row is None:
        return None
    return AttachmentInfo(
        id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
    )


async def put(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID,
    checked: CheckedFile,
    uploaded_by: str | None,
) -> AttachmentInfo:
    """Store the campaign's attachment, replacing whatever was there.

    Replacing rather than versioning is deliberate. A second row would mean deciding which one a
    send uses, and the honest answer -- "the newest" -- is what one row already says. The old
    file is not kept: it was the operator's own upload, nothing points at it, and keeping stale
    copies of documents somebody replaced is a privacy liability with no reader.

    **What this cannot do is change an email already sent.** A message that has left carries the
    file it carried; swapping the row does not reach into anybody's inbox.
    """
    existing = await session.scalar(
        select(Attachment).where(
            Attachment.org_id == ctx.org_id, Attachment.campaign_id == campaign_id
        )
    )
    if existing is None:
        existing = Attachment(org_id=ctx.org_id, campaign_id=campaign_id)
        session.add(existing)

    existing.filename = checked.filename
    existing.content_type = checked.content_type
    existing.size_bytes = checked.size_bytes
    existing.sha256 = checked.sha256
    existing.content = checked.content
    existing.uploaded_by = uploaded_by
    await session.flush()

    return AttachmentInfo(
        id=existing.id,
        filename=existing.filename,
        content_type=existing.content_type,
        size_bytes=existing.size_bytes,
        sha256=existing.sha256,
        uploaded_by=existing.uploaded_by,
        created_at=existing.created_at,
    )


async def remove(session: AsyncSession, ctx: OrgContext, *, campaign_id: uuid.UUID) -> bool:
    """Delete the campaign's attachment. Returns whether there was one."""
    result = await session.execute(
        delete(Attachment).where(
            Attachment.org_id == ctx.org_id, Attachment.campaign_id == campaign_id
        )
    )
    # `rowcount` is on the cursor result a DELETE returns, which the generic `Result` type does
    # not advertise; the cast is to mypy rather than to the driver.
    return bool(cast("CursorResult[Any]", result).rowcount)


async def load_for_send(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    campaign: Campaign,
    draft: Draft,
    settings: CampaignSettings,
) -> OutgoingFile | None:
    """The file this message carries, if any. The only place `content` is read.

    Resolved at send time rather than at approval, so that replacing the file updates every
    email not yet sent -- which is what an operator who has just fixed a typo in their deck
    expects. The ones already gone keep what they had, because that is what happened.

    Three ways to get nothing, in order of how often they happen: the campaign has no file, the
    settings say this kind of email does not carry it, or a reviewer unticked the box on this
    particular draft. `should_attach` holds that last decision so the NULL-means-default rule
    lives in one place.
    """
    default = {
        "initial": settings.attach_initial,
        "followup": settings.attach_followups,
        # A reply carries the file only when somebody ticked the box on that draft.
        "reply": False,
    }.get(draft.kind, False)

    if not should_attach(draft_override=draft.attach, campaign_default=default):
        return None

    row = await session.scalar(
        select(Attachment).where(Attachment.org_id == org_id, Attachment.campaign_id == campaign.id)
    )
    if row is None:
        # Not an error. An operator who deletes the attachment while drafts are waiting has
        # decided those emails go without it, and refusing to send them would be the system
        # breaking a campaign over an absent nicety.
        return None

    return OutgoingFile(filename=row.filename, content_type=row.content_type, content=row.content)
