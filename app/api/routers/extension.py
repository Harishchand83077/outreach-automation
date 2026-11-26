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

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import SessionDep
from app.db.models import Capture, FitScoreRow, JobPost, SavedPerson, Suggestion
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


# ---------------------------------------------------------------------------
# Job capture (S19.4, ADR-070) — save a job link seen in the extension
# ---------------------------------------------------------------------------


@router.post("/jobs/capture", status_code=status.HTTP_201_CREATED)
async def capture_job(
    session: SessionDep,
    url: Annotated[str, Body(embed=True)],
    title: Annotated[str, Body(embed=True)],
    company_domain: Annotated[str | None, Body(embed=True)] = None,
    body_text: Annotated[str | None, Body(embed=True)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Save a job posting link the extension sees.

    **No form filling. No submit.** The extension captures the link and title;
    the person decides what to do with it on the dashboard.
    """
    bearer = await _bearer(session, authorization)
    if not url.strip().lower().startswith(("http://", "https://")) or not title.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A job link needs an http(s) address and a title.",
        )
    now = datetime.now(tz=UTC)

    # De-duplicate by URL — a posting seen before returns the existing id.
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:64]

    existing = (
        await session.execute(
            select(JobPost).where(
                JobPost.org_id == bearer.org_id,
                JobPost.source == "pasted",
                JobPost.external_id == url_hash,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        # Update the title and verification timestamp if the posting is still open.
        # Text sent on a later save fills a link-only one in, so it can then be scored.
        if body_text and body_text.strip() and not existing.body_text:
            existing.body_text = body_text.strip()[:MAX_PAGE_CHARS]
        if existing.status == "open":
            existing.last_verified_at = now
        await session.commit()
        return {"job_post_id": str(existing.id), "created": False}

    post = JobPost(
        org_id=bearer.org_id,
        source="pasted",
        external_id=url_hash,
        company_domain=company_domain,
        title=title.strip()[:500],
        body_text=(body_text.strip()[:MAX_PAGE_CHARS] if body_text and body_text.strip() else None),
        apply_url=url[:2000],
        first_seen_at=now,
        fetched_at=now,
        created_at=now,
    )
    session.add(post)
    await session.commit()
    await session.refresh(post)
    return {"job_post_id": str(post.id), "created": True}


# ---------------------------------------------------------------------------
# S22 — remember an answer, know a job is already saved, see recent saves (ADR-083)
# ---------------------------------------------------------------------------


@router.post("/answers", status_code=status.HTTP_201_CREATED)
async def propose_answer(
    session: SessionDep,
    question: Annotated[str, Body(embed=True)],
    answer: Annotated[str, Body(embed=True)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Offer an answer for the person to approve later. **It is never offered back until they do.**

    The extension can write an *unapproved* row and nothing else: approval is a dashboard action,
    so a leaked extension token cannot make the bank say something the person never read. An
    existing entry for the same question is **left alone** -- `add_answer` would reset an
    approved answer to unapproved, which a button in a side panel must not do silently.
    """
    bearer = await _bearer(session, authorization)
    ctx = OrgContext(org_id=bearer.org_id, actor_id=f"ext:{bearer.user_id}")
    pattern = " ".join(question.split())[:MAX_LABEL_CHARS].lower()
    text = answer.strip()[:4000]
    if not pattern or not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An answer needs both the question and the answer.",
        )
    existing = {row.question_pattern for row in await profile.list_answers(session, ctx)}
    if pattern in existing:
        return {"saved": False, "reason": "You already have an answer for this question."}
    await profile.add_answer(session, ctx, question_pattern=pattern, answer=text, approve=False)
    await session.commit()
    return {"saved": True, "reason": "Saved for your approval in the dashboard."}


@router.post("/jobs/lookup")
async def lookup_job(
    session: SessionDep,
    url: Annotated[str, Body(embed=True)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Whether this exact link was already saved. POST so the URL stays out of access logs."""
    bearer = await _bearer(session, authorization)
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:64]
    row = (
        await session.execute(
            select(JobPost).where(
                JobPost.org_id == bearer.org_id,
                JobPost.source == "pasted",
                JobPost.external_id == url_hash,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return {"saved": False}
    return {"saved": True, "job_post_id": str(row.id), "title": row.title, "status": row.status}


@router.get("/jobs/recent")
async def recent_jobs(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """The last few saved postings, with a fit score **only if one was already computed**.

    Read-only and model-free: a score is computed in the dashboard on an explicit press, and
    this just reports the stored row, so the panel and the dashboard cannot disagree.
    """
    bearer = await _bearer(session, authorization)
    rows = (
        await session.execute(
            select(JobPost, FitScoreRow)
            .outerjoin(
                FitScoreRow,
                (FitScoreRow.job_post_id == JobPost.id) & (FitScoreRow.org_id == JobPost.org_id),
            )
            .where(JobPost.org_id == bearer.org_id, JobPost.source == "pasted")
            .order_by(JobPost.created_at.desc())
            .limit(8)
        )
    ).all()
    return {
        "jobs": [
            {
                "id": str(post.id),
                "title": post.title,
                "company_domain": post.company_domain,
                "status": post.status,
                "saved_at": post.created_at.isoformat() if post.created_at else None,
                "fit": None
                if fit is None
                else {
                    "score": fit.score,
                    "matched": len(fit.matched or []),
                    "requirements": len(fit.requirements or []),
                },
            }
            for post, fit in rows
        ]
    }


# ---------------------------------------------------------------------------
# ADR-084 -- save the one person whose page the user is looking at
# ---------------------------------------------------------------------------


@router.post("/people", status_code=status.HTTP_201_CREATED)
async def save_person(
    session: SessionDep,
    url: Annotated[str, Body(embed=True)],
    full_name: Annotated[str, Body(embed=True)],
    headline: Annotated[str | None, Body(embed=True)] = None,
    company_name: Annotated[str | None, Body(embed=True)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Remember a person the user pressed a button on. **Nothing is fetched** from the address.

    The address is stored for the user to open themselves (hard rule 4). It is not a lead and
    has no email; the dashboard's "People to look up" is where an address gets recorded.
    """
    bearer = await _bearer(session, authorization)
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")) or not full_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A person needs an http(s) address and a name.",
        )
    url = url.split("#", 1)[0].split("?", 1)[0][:2000]
    existing = (
        await session.execute(
            select(SavedPerson).where(
                SavedPerson.org_id == bearer.org_id, SavedPerson.profile_url == url
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"person_id": str(existing.id), "created": False, "state": existing.state}
    person = SavedPerson(
        org_id=bearer.org_id,
        full_name=full_name.strip()[:300],
        headline=(headline.strip()[:500] if headline and headline.strip() else None),
        company_name=(
            company_name.strip()[:300] if company_name and company_name.strip() else None
        ),
        profile_url=url,
    )
    session.add(person)
    await session.commit()
    return {"person_id": str(person.id), "created": True, "state": person.state}
