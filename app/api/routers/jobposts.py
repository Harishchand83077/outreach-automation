"""Job postings: paste one, list them, archive one (F5).

No feed polling here — that is a worker job, because it calls the network. These are the three
things a person does from the dashboard, and all three are database work.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api.deps import CurrentUserDep, SessionDep, require_role
from app.services.jobposts import intake

router = APIRouter(tags=["job-posts"])


def _serialise(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "source": row.source,
        "title": row.title,
        "location": row.location,
        "apply_url": row.apply_url,
        "company_domain": row.company_domain,
        "posted_at": row.posted_at.isoformat() if row.posted_at else None,
        "fetched_at": row.fetched_at.isoformat(),
        "has_body": bool(row.body_text),
    }


@router.get("/job-posts")
async def list_job_posts(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = await intake.list_posts(session, user.ctx)
    return {"job_posts": [_serialise(row) for row in rows]}


@router.post("/job-posts", status_code=status.HTTP_201_CREATED)
async def paste_a_job_post(
    user: CurrentUserDep,
    session: SessionDep,
    url: Annotated[str, Body(embed=True)],
    title: Annotated[str | None, Body(embed=True)] = None,
    body_text: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Store a posting the user is reading.

    The primary path for boards with no public feed. **Nothing fetches the URL** — the user
    supplies the text, which is what keeps this on the right side of hard rule 4 and of every
    job board's terms.
    """
    try:
        row, created = await intake.store_pasted(
            session, user.ctx, url=url, title=title, body_text=body_text
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"job_post": _serialise(row), "created": created}


@router.delete("/job-posts/{post_id}", dependencies=[Depends(require_role("admin"))])
async def archive_a_job_post(
    post_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Archive rather than delete: an application may point at this posting."""
    archived = await intake.archive(session, user.ctx, post_id=post_id)
    if not archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job post not found.")
    await session.commit()
    return {"archived": True}
