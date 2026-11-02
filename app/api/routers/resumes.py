"""Tailored resumes and the application tracker (F20, ADR-043).

**This router is on the dashboard door only**, which is what makes the acceptance criterion true
rather than intended: a resume version is the person's own career and an application is their own
claim to have applied. An `ext:suggest` token -- the one living in a browser extension on a
laptop -- is refused at every door but `/ext/v1` (ADR-039), and nothing on `/ext/v1` names these
tables. An extension that could mark an application sent would have the tracker assert the person
did something they did not do, which is the one thing a tracker must never do.

**No model is called in a request.** Tailoring asks a model, so it is a queued job and the route
answers with the job id; rendering a PDF is a queued job for memory reasons. Assembling a version
from operations the person chose is pure, so that one happens inline.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Response, status

from app.api.deps import CurrentUserDep, SessionDep
from app.db.models import APPLICATION_STATUSES, RESUME_SECTIONS, Application, JobPost, ResumeVersion
from app.db.queue import JobKind, QueueFull, enqueue
from app.services.resume import ops as resume_ops
from app.services.resume import store

router = APIRouter(tags=["resumes"])


def _version(row: ResumeVersion, *, full: bool = False) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "id": str(row.id),
        "label": row.label,
        "job_post_id": str(row.job_post_id) if row.job_post_id else None,
        "fact_ids": list(row.fact_ids or []),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if full:
        summary["header"] = row.header
        summary["edit_ops"] = list(row.edit_ops or [])
        summary["rendered_text"] = row.rendered_text
    return summary


def _application(row: Application) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "job_post_id": str(row.job_post_id),
        "resume_version_id": str(row.resume_version_id) if row.resume_version_id else None,
        "status": row.status,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        "follow_up_due": row.follow_up_due.isoformat() if row.follow_up_due else None,
        "notes": row.notes,
    }


@router.get("/resume-versions")
async def list_versions(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = await store.list_versions(session, user.ctx)
    return {
        "versions": [_version(row) for row in rows],
        "sections": list(RESUME_SECTIONS),
        "note": (
            "Every line of a tailored resume comes from a confirmed fact. A version is never "
            "edited: a change makes a new one, so an application always points at the document "
            "that was actually sent."
        ),
    }


@router.post("/resume-versions", status_code=status.HTTP_201_CREATED)
async def create_version(
    user: CurrentUserDep,
    session: SessionDep,
    label: Annotated[str, Body(embed=True)],
    edit_ops: Annotated[list[dict[str, Any]], Body(embed=True)],
    header: Annotated[str, Body(embed=True)] = "",
    job_post_id: Annotated[uuid.UUID | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Assemble a version from operations the person chose. Pure: no model, no network.

    A refusal returns every problem rather than the first, because somebody fixing a resume wants
    the whole list in one pass.
    """
    try:
        parsed = resume_ops.parse_ops(edit_ops)
    except resume_ops.OpError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    try:
        created = await store.create_version(
            session,
            user.ctx,
            ops=parsed,
            label=label,
            header=header,
            job_post_id=job_post_id,
            # `user.ctx` carries no actor, so the identity is taken from the caller itself.
            # Recorded as the person's own, which is what distinguishes it from a proposal the
            # model produced -- a distinction the dataset reads (ADR-026).
            created_by=f"user:{user.user_id}",
        )
    except store.ResumeRefused as refused:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"refused": refused.report.messages},
        ) from refused

    await session.commit()
    return {"version": _version(created.version, full=True)}


@router.get("/resume-versions/{version_id}")
async def get_version(
    version_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    row = await store.get_version(session, user.ctx, version_id=version_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resume version not found."
        )
    render = await store.get_render(session, user.ctx, version_id=version_id)
    return {
        "version": _version(row, full=True),
        "pdf": (
            {"size_bytes": render.size_bytes, "rendered_at": render.rendered_at.isoformat()}
            if render is not None
            else None
        ),
    }


@router.post("/job-posts/{job_post_id}/tailor", status_code=status.HTTP_202_ACCEPTED)
async def tailor_for_posting(
    job_post_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Queue a tailoring proposal for this posting.

    Accepted rather than done: it calls a model, and a request must not wait on one. The dedupe
    key is the posting, so pressing the button twice queues one job -- and a refusal is recorded
    as an event rather than as a dead job, since a proposal the facts do not support is the
    validator working.
    """
    posting = await session.get(JobPost, job_post_id)
    if posting is None or posting.org_id != user.ctx.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")

    try:
        job = await enqueue(
            session,
            org_id=user.ctx.org_id,
            kind=JobKind.TAILOR_RESUME,
            payload={"job_post_id": str(job_post_id)},
            dedupe_key=f"tailor:{job_post_id}",
        )
    except QueueFull as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    await session.commit()
    return {
        "queued": job is not None,
        "job_id": str(job.id) if job is not None else None,
        "note": "Already queued." if job is None else "A proposal will appear when it is ready.",
    }


@router.post("/resume-versions/{version_id}/render", status_code=status.HTTP_202_ACCEPTED)
async def render_version(
    version_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Queue the PDF. Its own job because of memory on a small host, not because it is slow."""
    row = await store.get_version(session, user.ctx, version_id=version_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resume version not found."
        )
    try:
        job = await enqueue(
            session,
            org_id=user.ctx.org_id,
            kind=JobKind.RENDER_RESUME_PDF,
            payload={"resume_version_id": str(version_id)},
            dedupe_key=f"render-resume:{version_id}",
        )
    except QueueFull as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    await session.commit()
    return {"queued": job is not None, "job_id": str(job.id) if job is not None else None}


@router.get("/resume-versions/{version_id}/pdf")
async def download_pdf(
    version_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Response:
    row = await store.get_render(session, user.ctx, version_id=version_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This version has not been rendered yet. Ask for a render first.",
        )
    return Response(
        content=row.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="resume-{version_id}.pdf"'},
    )


# --------------------------------------------------------------------------- the tracker


@router.get("/applications")
async def list_applications(
    user: CurrentUserDep, session: SessionDep, status_filter: str | None = None
) -> dict[str, Any]:
    rows = await store.list_applications(session, user.ctx, status=status_filter)
    return {
        "applications": [_application(row) for row in rows],
        "statuses": list(APPLICATION_STATUSES),
    }


@router.post("/applications", status_code=status.HTTP_201_CREATED)
async def create_application(
    user: CurrentUserDep,
    session: SessionDep,
    job_post_id: Annotated[uuid.UUID, Body(embed=True)],
    resume_version_id: Annotated[uuid.UUID | None, Body(embed=True)] = None,
    notes: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Start tracking a posting. Created `planned`: this is not a claim that anybody applied."""
    try:
        row = await store.upsert_application(
            session,
            user.ctx,
            job_post_id=job_post_id,
            resume_version_id=resume_version_id,
            notes=notes,
        )
    except store.ApplicationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"application": _application(row)}


@router.patch("/applications/{application_id}")
async def update_application(
    application_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    new_status: Annotated[str, Body(embed=True)],
    follow_up_due: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Move an application. **The only route in the system that can set `applied`.**"""
    due = None
    if follow_up_due:
        try:
            due = date.fromisoformat(follow_up_due)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="follow_up_due must be a date like 2026-11-09.",
            ) from exc

    try:
        row = await store.set_status(
            session,
            user.ctx,
            application_id=application_id,
            status=new_status,
            follow_up_due=due,
            actor_id=user.user_id,
        )
    except store.ApplicationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found.")
    await session.commit()
    return {"application": _application(row)}
