"""A candidate's facts, answer bank, and uploaded documents (F6, S19.1, ADR-068).

Database work only: no model is called in a request. Extracting a posting's requirements and
scoring fit are queued jobs, because they call a model and the request must not wait on one.
Parsing a resume is also a job: ``POST /documents/{id}/parse`` returns 202.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, UploadFile, status
from sqlalchemy import select

from app.api.deps import CurrentUserDep, SessionDep
from app.db.models import Document
from app.services.candidate import profile
from app.services.candidate.resume_parse import (
    extract_text,
    sha256_hex,
    text_to_proposals,
    validate_upload,
)

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


# ---------------------------------------------------------------------------
# Document upload and parse (S19.1, ADR-068)
# ---------------------------------------------------------------------------


def _doc(row: Document) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "filename": row.filename,
        "content_type": row.content_type,
        "size_bytes": row.size_bytes,
        "parsed": row.text is not None,
        "uploaded_at": row.uploaded_at.isoformat(),
    }


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    user: CurrentUserDep,
    session: SessionDep,
    file: UploadFile,
) -> dict[str, Any]:
    """Store an uploaded resume as bytes. Returns 201; call POST /documents/{id}/parse next.

    Deduplicates by sha256: uploading the same file twice returns the existing row.
    """
    content_type = file.content_type or ""
    filename = file.filename or ""
    data = await file.read()

    try:
        validate_upload(filename=filename, content_type=content_type, size=len(data))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    digest = sha256_hex(data)

    # Idempotent: return existing row if already stored.
    existing = (
        await session.execute(
            select(Document).where(
                Document.org_id == user.org_id,
                Document.sha256 == digest,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"document": _doc(existing), "created": False}

    doc = Document(
        org_id=user.org_id,
        kind="resume",
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        sha256=digest,
        bytes=data,
        uploaded_at=datetime.now(tz=UTC),
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    return {"document": _doc(doc), "created": True}


@router.get("/documents")
async def list_documents(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    rows = list(
        (
            await session.execute(
                select(Document)
                .where(Document.org_id == user.org_id)
                .order_by(Document.uploaded_at.desc())
            )
        ).scalars()
    )
    return {"documents": [_doc(row) for row in rows]}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return {"document": _doc(row)}


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> None:
    row = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    await session.delete(row)
    await session.commit()


@router.post("/documents/{document_id}/parse", status_code=status.HTTP_202_ACCEPTED)
async def parse_document(
    document_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Extract text from the stored bytes and propose facts (synchronous for now).

    Facts are stored as unconfirmed ``profile_facts``; nothing proposed here reaches a prompt
    until the owner confirms it. The endpoint returns immediately after proposing — no job is
    queued in this slice because the extraction is fast and purely local.
    """
    row = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == user.org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")

    text = extract_text(row.bytes, row.content_type)
    row.text = text

    proposals = text_to_proposals(text)
    proposed = await profile.propose_facts_from_resume(session, user.ctx, proposals=proposals)
    await session.commit()

    return {
        "document_id": str(document_id),
        "proposals": proposed,
        "note": (
            "Proposals are unconfirmed. Confirm each one in the facts screen before it can be used."
        ),
    }
