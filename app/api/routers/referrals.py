"""The referral finder: upload your connections, find who is at a company (F10, ADR-042).

**Nothing on this router fetches LinkedIn, and nothing on it sends anything.** The output is a
list of people the user already knows, with a suggested opener and the profile link from their
own export, which they open themselves. Hard rule 4 and ADR-038.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, require_role
from app.services.referrals import finder, parsing, store

router = APIRouter(tags=["referrals"])

MAX_UPLOAD_BYTES = parsing.MAX_BYTES


@router.get("/connections")
async def connection_summary(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """How many connections this workspace holds. **Not a list of them.**

    A route returning thousands of named people would be a contact database with a different
    name, and nobody needs to browse their own network here -- they need to know whether the
    upload worked, and then to search it by company.
    """
    return {"connections": await store.count(session, user.ctx)}


@router.post("/connections/import", status_code=status.HTTP_201_CREATED)
async def import_connections(
    user: CurrentUserDep, session: SessionDep, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    """Upload `Connections.csv` from a LinkedIn data export.

    The only way LinkedIn data enters this system. The file is parsed in memory, the
    `Email Address` column is dropped before anything is written, and nothing is fetched.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="That file is larger than a connections export should be.",
        )
    try:
        parsed = parsing.parse_export(data)
    except parsing.ExportError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    result = await store.store(session, user.ctx, parsed)
    await session.commit()
    return {
        "batch_id": str(result.batch_id),
        "stored": result.stored,
        "updated": result.updated,
        # Reported rather than swallowed: "4,312 of 4,400" is the only thing that tells somebody
        # the file was partly unreadable.
        "skipped_without_a_name": result.skipped,
        "truncated": result.truncated,
        "note": (
            "Stored for matching only. Email addresses in the export are discarded, nothing "
            "here is fetched from LinkedIn, and no message is ever sent from this page."
        ),
    }


@router.delete("/connections")
async def forget_connections(
    session: SessionDep, user: Annotated[CurrentUser, Depends(require_role("admin"))]
) -> dict[str, Any]:
    """Delete every connection this workspace holds. A real delete, not a flag."""
    removed = await store.forget_all(session, user.ctx)
    await session.commit()
    return {"removed": removed}


@router.get("/referrals")
async def find_referrals(
    user: CurrentUserDep,
    session: SessionDep,
    company: str | None = None,
    job_post_id: uuid.UUID | None = None,
    sender_name: str = "",
) -> dict[str, Any]:
    """Who in the user's own network is at this company, and what they might say.

    A `GET`, and safe as one: it reads this workspace's own rows, calls no provider, no model
    and no network at all. The suggested note is built by rules from stored values -- see
    `finder.build_note` for why a model is deliberately not used here.
    """
    if not company and job_post_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Give a company name or a job posting to match against.",
        )
    matches, resolved = await finder.find(
        session,
        user.ctx,
        company=company,
        job_post_id=job_post_id,
        # Deliberately **not** `user.email`: signing a note with an address reads oddly and
        # puts their address into text they will paste into somebody else's message box. With
        # no name the note simply ends "Thanks," and the person types their own.
        sender_name=sender_name,
    )
    return {
        "company": resolved,
        "referrals": [
            {
                "connection_id": str(one.connection_id),
                "full_name": one.full_name,
                "position": one.position,
                "company": one.company,
                # From the user's own export. Shown for them to open; never fetched.
                "profile_url": one.profile_url,
                "connected_on": one.connected_on,
                "why": one.why,
                "note": one.note,
            }
            for one in matches
        ],
        "note": (
            "A suggested opener, not a message. Edit it and send it yourself on LinkedIn; "
            "nothing here contacts anybody."
        ),
    }
