"""A candidate's facts and answer bank (F6).

Database work only: no model is called in a request. Extracting a posting's requirements and
scoring fit are queued jobs, because they call a model and the request must not wait on one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status

from app.api.deps import CurrentUserDep, SessionDep
from app.services.candidate import profile

router = APIRouter(tags=["candidate"])


def _fact(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "fact_id": row.fact_id,
        "kind": row.kind,
        "statement": row.statement,
        "evidence_url": row.evidence_url,
        "source": row.source,
        # The word a reader needs: a proposed fact is not usable yet.
        "confirmed": row.confirmed_at is not None,
    }


def _answer(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "question_pattern": row.question_pattern,
        "answer": row.answer,
        "fact_ids": list(row.fact_ids or []),
        "approved": row.approved_at is not None,
    }


@router.get("/profile/facts")
async def list_facts(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = await profile.list_facts(session, user.ctx)
    return {
        "facts": [_fact(row) for row in rows],
        "kinds": list(profile.FACT_KINDS),
        # Said plainly, because it is the rule that makes the whole thing trustworthy.
        "note": (
            "A draft may only claim a confirmed fact. Facts mined from a resume or GitHub "
            "arrive as proposals until you agree to them."
        ),
    }


@router.post("/profile/facts", status_code=status.HTTP_201_CREATED)
async def add_fact(
    user: CurrentUserDep,
    session: SessionDep,
    kind: Annotated[str, Body(embed=True)],
    statement: Annotated[str, Body(embed=True)],
    evidence_url: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Add a fact the candidate typed, which is confirmed by the act of typing it."""
    try:
        row = await profile.add_fact(
            session, user.ctx, kind=kind, statement=statement, evidence_url=evidence_url
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"fact": _fact(row)}


@router.post("/profile/facts/{fact_row_id}/confirm")
async def confirm_fact(
    fact_row_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    if not await profile.confirm_fact(session, user.ctx, fact_row_id=fact_row_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fact not found.")
    await session.commit()
    return {"confirmed": True}


@router.delete("/profile/facts/{fact_row_id}")
async def delete_fact(
    fact_row_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    if not await profile.delete_fact(session, user.ctx, fact_row_id=fact_row_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fact not found.")
    await session.commit()
    return {"deleted": True}


@router.get("/profile/answers")
async def list_answers(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = await profile.list_answers(session, user.ctx)
    return {"answers": [_answer(row) for row in rows]}


@router.post("/profile/answers", status_code=status.HTTP_201_CREATED)
async def add_answer(
    user: CurrentUserDep,
    session: SessionDep,
    question_pattern: Annotated[str, Body(embed=True)],
    answer: Annotated[str, Body(embed=True)],
    fact_ids: Annotated[list[str] | None, Body(embed=True)] = None,
    approve: Annotated[bool, Body(embed=True)] = False,
) -> dict[str, Any]:
    """Store an answer. Unapproved unless asked for, so nothing is offered by accident."""
    try:
        row = await profile.add_answer(
            session,
            user.ctx,
            question_pattern=question_pattern,
            answer=answer,
            fact_ids=fact_ids,
            approve=approve,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"answer": _answer(row)}


@router.post("/profile/answers/{answer_id}/approve")
async def approve_answer(
    answer_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    if not await profile.approve_answer(session, user.ctx, answer_id=answer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer not found.")
    await session.commit()
    return {"approved": True}
