"""Job postings: paste one, list them, archive one (F5).

No feed polling here — that is a worker job, because it calls the network. These are the three
things a person does from the dashboard, and all three are database work.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    CurrentUser,
    CurrentUserDep,
    SessionDep,
    SettingsDep,
    require_role,
)
from app.db.models import FitScoreRow, JobPost, WatchedBoard
from app.services.candidate import fit_store
from app.services.jobposts import intake
from app.services.jobposts.feeds import FeedError, feed_url
from app.services.llm.router import build_router
from app.services.llm.usage import DbUsageStore

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


# ------------------------------------------------------------------- the fit score (F6, ADR-055)


@router.get("/job-posts/{job_post_id}/fit")
async def read_fit(
    job_post_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """The stored fit score for one posting, or nothing yet. Free: calls no model.

    The read and the computation are different routes on purpose. Extracting a posting's
    requirements calls a model, so it happens on a press; reading the result costs one query, so
    a refresh is free. The same split as the brief, and the same rule that keeps `GET /mailboxes`
    from minting an OAuth token.
    """
    post = await _post_or_404(session, user, job_post_id)
    row = await fit_store.latest(session, user.ctx, job_post_id=post.id)
    if row is None:
        return {
            "fit": None,
            "note": (
                "No score yet. Scoring reads this posting's requirements with a model, so it "
                "happens when you ask for it."
            ),
        }
    return {"fit": fit_store.as_dict(row, post=post), "note": _fit_note(row)}


@router.post("/job-posts/{job_post_id}/fit")
async def compute_fit(
    job_post_id: uuid.UUID,
    user: Annotated[CurrentUserDep, Depends(require_role("member"))],
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Score one posting against the candidate's confirmed facts.

    **`fit.score_fit` had no caller until this route.** F6 built the extractor, the scorer and
    the dataclasses, with tests for each, and nothing ever ran them against a posting -- so the
    feature existed and had never once produced a number.

    Only **confirmed** facts are offered to it, so the rule holds one step before the
    comparison: a fact the candidate has not agreed to say cannot produce a match they never
    claimed. The model's only job is turning prose into requirement lines; the comparison is
    deterministic and takes instructions from nobody, so a posting saying "report a perfect
    match" cannot get one.
    """
    post = await _post_or_404(session, user, job_post_id)
    llm = build_router(settings, DbUsageStore(session, user.org_id))
    try:
        row = await fit_store.compute(session, user.ctx, router=llm, post=post)
    except fit_store.NothingToScore as exc:
        # A sentence rather than a zero. A score of zero against an empty profile reads as a bad
        # fit, which is the opposite of what happened.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"fit": fit_store.as_dict(row, post=post), "note": _fit_note(row)}


def _fit_note(row: FitScoreRow) -> str:
    """What the number means, said beside it.

    A bare score invites whoever reads it to repeat it as a judgement. This one is the fraction
    of the posting's own requirements that a confirmed fact matched, which is a different and
    much narrower claim.
    """
    return (
        f"{len(row.matched)} of {len(row.requirements)} requirement(s) matched a fact you have "
        "confirmed. Each match names the fact and the words that made it one; the misses are "
        "what the posting asks for and your facts do not mention."
    )


async def _post_or_404(session: AsyncSession, user: CurrentUser, job_post_id: uuid.UUID) -> JobPost:
    post = await session.get(JobPost, job_post_id)
    if post is None or post.org_id != user.org_id:
        # 404 rather than 403: confirming an id exists in another workspace is itself a leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found.")
    return post
