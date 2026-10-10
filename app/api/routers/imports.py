"""Importing a sheet of leads: look before you commit.

`ARCHITECTURE_TARGET.md` §7A and §14. Two endpoints, and the split between them is the point.
`preview` writes nothing and returns the header mapping plus a verdict for every row; `commit`
re-validates the same file from scratch and inserts what survives.

**`commit` never trusts the preview.** It re-reads and re-checks rather than accepting a verdict
list from the caller, because a preview can be stale — someone may have opted out in the minutes
a person spent looking at it — or simply forged. Anything that decides who receives email is
checked against the database at the moment of writing, not at the moment of looking.

The upload itself is handled as bytes in memory and the client's filename is used only to pick a
parser. The prototype wrote the client-supplied filename to disk (issue C2), which is how
`../../x.xlsx` becomes somebody else's problem.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.deps import CurrentUserDep, SessionDep
from app.db.models import Campaign
from app.db.queue import JobKind, enqueue
from app.services.importer import service as importer
from app.services.importer.service import ImportPreview
from app.services.importer.sheet import SheetError

router = APIRouter(tags=["imports"])

#: §7A. Large enough for any realistic lead sheet, small enough that a hostile upload cannot
#: exhaust memory. Checked after reading, because the declared Content-Length is the client's
#: claim rather than a fact.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _serialise(preview: ImportPreview) -> dict[str, Any]:
    return {
        "filename": preview.filename,
        "file_sha256": preview.file_sha256,
        "truncated": preview.truncated,
        "can_commit": preview.can_commit,
        "counts": preview.counts,
        "duplicate_domains": preview.duplicate_domains,
        "mapping": {
            "mapped": {str(index): name for index, name in preview.mapping.mapped.items()},
            "unmapped": {str(index): name for index, name in preview.mapping.unmapped.items()},
            "is_importable": preview.mapping.is_importable,
        },
        "rows": [
            {
                "row_number": row.row_number,
                "verdict": row.verdict.value,
                "reasons": row.reasons,
                "flags": row.flags,
                "email": row.email_norm,
                "full_name": row.full_name,
                "company_name": row.company_name,
                "job_title": row.job_title,
            }
            for row in preview.rows
        ],
    }


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The file is empty."
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    return data


async def _owned_campaign(
    session: SessionDep, user: CurrentUserDep, campaign_id: uuid.UUID
) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    return campaign


@router.post("/campaigns/{campaign_id}/imports/preview")
async def preview_import(
    campaign_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Parse and validate. Writes nothing."""
    await _owned_campaign(session, user, campaign_id)
    data = await _read_upload(file)

    try:
        result = await importer.preview(
            session,
            user.ctx,
            data=data,
            filename=file.filename or "upload.csv",
            campaign_id=campaign_id,
        )
    except SheetError as exc:
        # The file could not be read at all, as opposed to individual bad rows, which come back
        # with verdicts rather than raising.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return _serialise(result)


@router.post("/campaigns/{campaign_id}/imports/commit")
async def commit_import(
    campaign_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Re-validate from the file and insert what survives, with research queued for each lead."""
    await _owned_campaign(session, user, campaign_id)
    data = await _read_upload(file)

    try:
        leads, result = await importer.commit(
            session,
            user.ctx,
            data=data,
            filename=file.filename or "upload.csv",
            campaign_id=campaign_id,
        )
        # Queued in the same transaction as the insert, so a lead never exists without the work
        # that moves it forward, and a rollback takes both.
        for lead in leads:
            await enqueue(
                session,
                org_id=user.org_id,
                kind=JobKind.RESEARCH_LEAD,
                lead_id=lead.id,
                payload={"lead_id": str(lead.id)},
                dedupe_key=f"research:{lead.id}",
            )
        await session.commit()
    except SheetError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return {"imported": len(leads), "preview": _serialise(result)}
