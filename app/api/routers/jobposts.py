"""Job postings: paste one, list them, archive one (F5).

No feed polling here — that is a worker job, because it calls the network. These are the three
things a person does from the dashboard, and all three are database work.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep, require_role
from app.db.models import WatchedBoard
from app.services.jobposts import intake
from app.services.jobposts.feeds import FeedError, feed_url

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


@router.get("/watched-boards")
async def list_watched_boards(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = await session.scalars(
        select(WatchedBoard)
        .where(WatchedBoard.org_id == user.org_id)
        .order_by(WatchedBoard.created_at)
    )
    return {
        "watched_boards": [
            {
                "id": str(row.id),
                "source": row.source,
                "slug": row.slug,
                "label": row.label,
                "last_polled_at": row.last_polled_at.isoformat() if row.last_polled_at else None,
                # The reason is the operator's own (a wrong slug, a board that moved), so it is
                # shown: without it a board that never returns anything is unexplainable.
                "last_error": row.last_error,
            }
            for row in rows
        ],
        "sources": ["greenhouse", "lever", "ashby"],
    }


@router.post(
    "/watched-boards",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("admin"))],
)
async def watch_a_board(
    user: CurrentUserDep,
    session: SessionDep,
    source: Annotated[str, Body(embed=True)],
    slug: Annotated[str, Body(embed=True)],
    label: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Add a board to the daily poll.

    The slug is validated by building the feed URL, which refuses an unknown source and an empty
    slug -- the same function the poll itself uses, so a board that saves is a board that can be
    polled.
    """
    try:
        feed_url(source, slug)
    except FeedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    cleaned = slug.strip().strip("/")
    existing = await session.scalar(
        select(WatchedBoard).where(
            WatchedBoard.org_id == user.org_id,
            WatchedBoard.source == source,
            WatchedBoard.slug == cleaned,
        )
    )
    if existing is not None:
        # Idempotent on the pair, like `POST /mailboxes` is on an address: the board *is* its
        # identity, and a 409 would only make the caller ask for the id it just described.
        return {"watched_board": {"id": str(existing.id)}, "created": False}

    row = WatchedBoard(
        org_id=user.org_id, source=source, slug=cleaned, label=(label or "").strip()[:200] or None
    )
    session.add(row)
    await session.commit()
    return {"watched_board": {"id": str(row.id)}, "created": True}


@router.delete("/watched-boards/{board_id}", dependencies=[Depends(require_role("admin"))])
async def stop_watching_a_board(
    board_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Stop polling. Postings already stored stay: they may have been applied to."""
    row = await session.scalar(
        select(WatchedBoard).where(WatchedBoard.id == board_id, WatchedBoard.org_id == user.org_id)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found.")
    await session.delete(row)
    await session.commit()
    return {"removed": True}
