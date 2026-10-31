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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import ValidationError

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, require_role
from app.db.models import Campaign
from app.db.queue import JobKind, QueueFull, enqueue
from app.domain.campaign import CampaignSettings
from app.services.importer import service as importer
from app.services.importer.service import ImportPreview
from app.services.importer.sheet import SheetError
from app.services.importer.undo import undo_import

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


#: One day of work queued ahead of what can be sent, so there is always a drafted queue to
#: approve from without a thousand leads racing the free tier on day one.
HEAD_START_DAYS = 1


def _due_at(offset: int, campaign: Campaign, *, template_mode: bool) -> datetime | None:
    """When the nth lead's first job becomes runnable.

    `None` -- runnable now -- for a template campaign: rendering is local and instant, costs no
    quota, and a thousand drafts in seconds is the point of that mode.

    For a research campaign the pace is the campaign's own daily cap, because that is the real
    ceiling on how many of these leads can ever be emailed.
    """
    if template_mode:
        return None
    try:
        settings = CampaignSettings.model_validate(campaign.settings or {})
    except ValidationError:
        settings = CampaignSettings()
    per_day = max(settings.daily_cap, 1)
    day = max(offset // per_day - HEAD_START_DAYS, 0)
    if day == 0:
        return None
    return datetime.now(tz=UTC) + timedelta(days=day)


@router.post("/campaigns/{campaign_id}/imports/commit")
async def commit_import(
    campaign_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Re-validate from the file and insert what survives, with the next job queued per lead.

    Which job depends on the campaign's mode. A `research` campaign researches first; a
    `template` campaign goes straight to drafting, because there is nothing to research -- the
    email is already written and the sheet fills in the blanks. Queuing research for it would
    spend a page fetch and a model call to produce findings nothing reads.
    """
    campaign = await _owned_campaign(session, user, campaign_id)
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
        #
        # **Staggered rather than all runnable at once.** A thousand leads used to mean a
        # thousand jobs due immediately. Per-kind concurrency stops the *worker* melting, but
        # nothing paced against the free tier's **daily** limits -- the chain's quota would go in
        # minutes, every later job would fail into a cool-down, and the dashboard would show a
        # stalled pipeline with no visible cause.
        #
        # There is no value in researching a thousand leads today when at most `daily_cap` can
        # be emailed. `run_after` spreads them over the days they could actually be sent, with
        # one day of head start so there is always a drafted queue to approve from.
        template_mode = campaign.mode == "template"
        kind = JobKind.DRAFT_LEAD if template_mode else JobKind.RESEARCH_LEAD
        prefix = "draft" if template_mode else "research"
        for offset, lead in enumerate(leads):
            await enqueue(
                session,
                org_id=user.org_id,
                kind=kind,
                lead_id=lead.id,
                payload={"lead_id": str(lead.id)},
                dedupe_key=f"{prefix}:{lead.id}",
                run_after=_due_at(offset, campaign, template_mode=template_mode),
            )
        await session.commit()
    except SheetError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except QueueFull as exc:
        # The whole import is rolled back rather than left half-queued: leads with no research
        # job would sit `NEW` for ever, looking imported and doing nothing. Answered as a 429
        # with the reason, not allowed to escape as a 500 — FastAPI handles a bare exception in
        # `ServerErrorMiddleware`, which sits *outside* `CORSMiddleware`, so the dashboard would
        # report the backend unreachable while it was answering perfectly. That trap has already
        # cost this project one long investigation.
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    # The batch id is what makes the import undoable, so it is returned rather than only
    # stored: a dashboard that has to go looking for "the most recent batch" would undo the
    # wrong one for anybody with two tabs open.
    batch_id = str(leads[0].import_batch_id) if leads else None
    return {"imported": len(leads), "batch_id": batch_id, "preview": _serialise(result)}


@router.delete("/campaigns/{campaign_id}/imports/{batch_id}")
async def undo_an_import(
    campaign_id: uuid.UUID,
    batch_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Remove the leads an upload created, while none of them has been emailed.

    **Not the erasure path, and the difference matters.** `DELETE /leads/{id}` exists for a
    data-subject request and deliberately leaves a hashed suppression behind, so that somebody
    who opted out and was then erased cannot be mailed again by the next import of the same
    sheet. Undoing a *mistaken upload* through that path would permanently block every address
    in it -- re-uploading the corrected sheet would skip exactly the people it was meant to
    reach, and nothing on screen would say why.

    Nobody here asked not to be contacted. A sheet was uploaded by mistake, and the correct
    state of the world is that it never happened, so **nothing is suppressed**.

    Admin-only: it deletes rows. A lead that has already been emailed is kept whatever the
    operator intended, and the response says how many and why -- the record of what we sent to
    whom is the one thing in this system that must not be editable.
    """
    await _owned_campaign(session, user, campaign_id)

    report = await undo_import(session, user.ctx, campaign_id=campaign_id, batch_id=batch_id)
    await session.commit()

    return {
        "removed": report.removed,
        "kept": report.kept,
        "message": report.summary(),
    }
