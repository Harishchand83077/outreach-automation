"""The Chrome extension's gateway: a third door (F8, ADR-039).

Mounted at `/ext/v1`, authenticated with an `ext:suggest` token, and **unable to reach anything
else**: this token is refused by `/api/v1` and by `/mcp`, and a Supabase JWT or an `mcp:` token
is refused here. Same reasoning as ADR-028's two doors — a credential that works everywhere is
one whose theft costs everything, and this one lives in a browser extension on somebody's laptop.

**It suggests and nothing more.** There is no route here that submits a form, uploads a file,
sends mail, approves anything or returns a rule the extension should follow. The store's policy
requires the extension's logic to ship inside the package, so the server returns **data only**;
a structural test asserts this module imports nothing that can send.

What the extension sends is the field labels it can see and the page URL. Values the user has
typed are never sent, and nothing here reads another tab.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Request, status

from app.api.deps import SessionDep
from app.db.models import Capture, Suggestion
from app.db.repo import OrgContext
from app.services import api_tokens
from app.services.candidate import profile

router = APIRouter(tags=["extension"], prefix="/ext/v1")

#: Labels longer than this are not form labels; they are prose somebody pasted.
MAX_LABEL_CHARS = 300
MAX_LABELS = 60
MAX_PAGE_CHARS = 20_000


async def _bearer(session: SessionDep, authorization: str | None) -> api_tokens.TokenBearer:
    """Resolve an `ext:suggest` token, or 401.

    One sentence for every refusal — unknown, revoked, expired, wrong scope, no longer a member.
    From outside they are all just *no*, and the difference is useful only to somebody guessing.
    """
    presented = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()

    bearer = await api_tokens.verify_token(session, presented) if presented else None
    if bearer is None or not bearer.permits(api_tokens.EXT_SUGGEST_SCOPE):
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "An extension token is required. Mint one in the dashboard under Settings and "
                "paste it into the extension."
            ),
            headers={"WWW-Authenticate": 'Bearer realm="outreach-ext"'},
        )
    await session.commit()
    return bearer


@router.get("/whoami")
async def whoami(
    session: SessionDep, authorization: Annotated[str | None, Header()] = None
) -> dict[str, Any]:
    """Confirm the token works, which is the first thing somebody pasting one wants to know.

    Returns the workspace's role and nothing identifying: not an email, not a name. The
    extension only needs to know it is connected.
    """
    bearer = await _bearer(session, authorization)
    return {"connected": True, "role": bearer.role, "scopes": list(bearer.scopes)}


@router.post("/suggest", status_code=status.HTTP_201_CREATED)
async def suggest(
    request: Request,
    session: SessionDep,
    page_url: Annotated[str, Body(embed=True)],
    labels: Annotated[list[str], Body(embed=True)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Propose an answer per form label, from **approved** answer-bank entries only.

    A label with no approved answer comes back as `needs_user` rather than a guess. That is the
    whole difference from tools that invent screening answers, and it is why there is no model
    call anywhere in this path: a guess dressed as an answer is worse than a blank.
    """
    bearer = await _bearer(session, authorization)
    ctx = OrgContext(org_id=bearer.org_id, actor_id=f"ext:{bearer.user_id}")

    cleaned = [
        " ".join(str(label).split())[:MAX_LABEL_CHARS]
        for label in labels[:MAX_LABELS]
        if str(label).strip()
    ]
    if not page_url.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="page_url must be http or https",
        )

    capture = Capture(
        org_id=ctx.org_id,
        token_id=bearer.token_id,
        page_url=page_url.strip()[:2000],
        # **No page text for a form.** Only the labels are needed to match an answer, and the
        # rest of an application page is the user's own data and somebody else's copy.
        page_text=None,
        kind="form",
    )
    session.add(capture)
    await session.flush()

    proposals: list[dict[str, Any]] = []
    for label in cleaned:
        match = await profile.match_question(session, ctx, label=label)
        row = Suggestion(
            org_id=ctx.org_id,
            capture_id=capture.id,
            field_key=label,
            proposed_answer=match.answer if match else None,
            fact_ids=list(match.fact_ids or []) if match else [],
            state="proposed" if match else "needs_user",
        )
        session.add(row)
        await session.flush()
        proposals.append(
            {
                "id": str(row.id),
                "field_key": label,
                "answer": row.proposed_answer,
                "fact_ids": list(row.fact_ids),
                "state": row.state,
            }
        )

    await session.commit()
    return {
        "capture_id": str(capture.id),
        "suggestions": proposals,
        # Said on every response, because it is the promise the whole feature rests on.
        "note": (
            "Suggestions only. Nothing here fills or submits a form: you accept, edit or reject "
            "each one and submit the page yourself."
        ),
    }


@router.post("/suggestions/{suggestion_id}/outcome")
async def record_outcome(
    suggestion_id: uuid.UUID,
    session: SessionDep,
    state: Annotated[str, Body(embed=True)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Record what the person did with a suggestion: accepted, edited or rejected.

    This is the only reason to keep a suggestion after showing it. An edit is the most valuable
    of the three (ADR-026): it says what the answer should have been.
    """
    bearer = await _bearer(session, authorization)
    if state not in ("accepted", "edited", "rejected"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="state must be accepted, edited or rejected",
        )

    row = await session.get(Suggestion, suggestion_id)
    if row is None or row.org_id != bearer.org_id:
        # Scoped rather than 403: a id from another workspace resolves to nothing instead of
        # confirming it exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    row.state = state
    await session.commit()
    return {"recorded": True}
