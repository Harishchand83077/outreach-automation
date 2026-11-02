"""Provider keys and proposed hiring contacts (F9, ADR-041).

Four things a person does: save their own key, run one lookup, review what came back, and turn
one of them into a lead.

**Every network call on this router is a `POST` somebody pressed.** No `GET` here talks to a
provider -- the same rule as `GET /mailboxes`, which is non-networked because refreshing it would
mint OAuth tokens. Here the stake is higher: a `GET` that called a provider would spend the
user's own credits every time somebody pressed F5.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, SettingsDep, require_role
from app.db.models import Campaign, ContactCandidate
from app.db.queue import QueueFull
from app.services.contacts import keys, lookup
from app.services.importer import first_job
from app.services.importer import service as importer
from app.services.importer.sheet import SheetError
from app.services.research import company_cache

router = APIRouter(tags=["contacts"])


def _serialise(row: ContactCandidate) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "company_domain": row.company_domain,
        "provider": row.provider,
        "full_name": row.full_name,
        "title": row.title,
        "email": row.email,
        # The screen needs this more than it needs the confidence number: a guess is a naming
        # pattern somebody inferred, and mailing one is how a bounce rate rises.
        "email_status": row.email_status,
        "confidence": row.confidence,
        "profile_url": row.profile_url,
        "state": row.state,
        "job_post_id": str(row.job_post_id) if row.job_post_id else None,
        "lead_id": str(row.lead_id) if row.lead_id else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/provider-keys")
async def list_provider_keys(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """Which providers this workspace can look up with.

    Every provider is listed, not every stored row: the question is "what can I look up?", and a
    provider with no key is an answer to it. No secret and no `invalid_reason` -- a provider's
    own sentence can reach a browser and a screenshot, so a row carries a boolean `usable`
    instead, exactly as `GET /mailboxes` does.
    """
    statuses = await keys.statuses(session, user.ctx)
    return {
        "providers": [
            {
                "provider": status_.provider,
                "kind": status_.kind,
                "label": status_.label,
                "connected": status_.connected,
                "usable": status_.usable,
                "last_used_at": status_.last_used_at.isoformat() if status_.last_used_at else None,
                "added_by": status_.added_by,
            }
            for status_ in statuses
        ],
        "daily_lookup_budget": lookup.DAILY_LOOKUP_BUDGET,
        "used_today": await lookup.used_today(session, org_id=user.org_id),
    }


@router.put("/provider-keys/{provider}")
async def save_a_provider_key(
    provider: str,
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    api_key: Annotated[str, Body(embed=True)],
    client_id: Annotated[str | None, Body(embed=True)] = None,
    label: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Store this workspace's own key for one provider, replacing any previous one.

    **`admin`, like the send allowlist**, because a key spends the workspace's money and a
    member reviews drafts rather than deciding what the workspace pays for.

    **Saving does not call the provider.** `POST /provider-keys/{provider}/verify` does, which is
    the same split as `POST /connect/google/verify`: a save that silently spent a credit to check
    itself would be a write with a cost nobody asked for, and a key pasted with a trailing space
    deserves a verify button rather than a surprise.
    """
    try:
        row = await keys.store(
            session,
            user.ctx,
            provider=provider,
            secret=api_key,
            label=label,
            client_id=client_id,
            settings=settings,
        )
    except keys.ProviderKeyError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    # The response says what was stored and never any part of it.
    return {"provider": row.provider, "kind": row.kind, "connected": True, "verified": False}


@router.delete("/provider-keys/{provider}")
async def revoke_a_provider_key(
    provider: str,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Stop using a key. The row is kept, revoked, so the change itself stays auditable."""
    try:
        removed = await keys.revoke(session, user.ctx, provider=provider)
    except keys.ProviderKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"provider": provider.lower(), "revoked": removed}


@router.post("/provider-keys/{provider}/verify")
async def verify_a_provider_key(
    provider: str,
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    domain: Annotated[str, Body(embed=True)] = "example.com",
) -> dict[str, Any]:
    """Prove the key works, by doing the smallest real thing it can do.

    There is no cheaper check: none of these providers offers a "is this key valid?" endpoint, so
    verification is a lookup. It therefore **counts against the daily budget and may spend one of
    the user's credits**, and the response says so rather than presenting it as free.

    The default domain is `example.com`, which is reserved by RFC 2606 and belongs to nobody -- a
    real company's domain would spend a credit on a lookup the person did not ask for.
    """
    try:
        result = await lookup.run(
            session, user.ctx, provider=provider, domain=domain, settings=settings
        )
    except lookup.LookupRefused as exc:
        await session.rollback()
        return {"provider": provider.lower(), "ok": False, "reason": str(exc)}
    await session.commit()
    return {
        "provider": result.provider,
        "ok": True,
        "note": (
            "The key works. This check is a real lookup, so it used one of today's lookups and "
            "may have spent a provider credit."
        ),
    }


@router.get("/contact-candidates")
async def list_contact_candidates(
    user: CurrentUserDep,
    session: SessionDep,
    state: str | None = None,
) -> dict[str, Any]:
    """Proposed contacts, newest first. Nothing here has been or can be emailed."""
    query = select(ContactCandidate).where(ContactCandidate.org_id == user.org_id)
    if state:
        query = query.where(ContactCandidate.state == state)
    rows = (
        await session.scalars(query.order_by(ContactCandidate.created_at.desc()).limit(200))
    ).all()
    return {"contact_candidates": [_serialise(row) for row in rows]}


@router.post("/contact-candidates/lookup", status_code=status.HTTP_201_CREATED)
async def run_a_lookup(
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    provider: Annotated[str, Body(embed=True)],
    domain: Annotated[str | None, Body(embed=True)] = None,
    job_post_id: Annotated[uuid.UUID | None, Body(embed=True)] = None,
    role_hint: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Ask one provider who hires at a company. **The button press** (F9).

    Any member may press it: looking somebody up is the work, and `admin` guards the key rather
    than the use of it -- the same split as adding an allowlist entry against going `live`.
    """
    try:
        result = await lookup.run(
            session,
            user.ctx,
            provider=provider,
            domain=domain,
            job_post_id=job_post_id,
            role_hint=role_hint,
            settings=settings,
        )
    except lookup.LookupRefused as exc:
        await session.rollback()
        # 422 rather than 500: every one of these is a sentence somebody can act on, and a bare
        # exception would reach FastAPI's error middleware *outside* CORS, which is how a
        # duplicate key once looked like an unreachable backend.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {
        "provider": result.provider,
        "domain": result.domain,
        "proposed": [_serialise(row) for row in result.proposed],
        # Said explicitly, because an empty list after a lookup that cost a credit reads as a
        # failure when it usually means we already had everybody.
        "already_known": result.already_known,
        "note": (
            "Proposals only. None of these can be emailed until you import one, which creates a "
            "lead that goes through the ordinary research, drafting and approval path."
        ),
    }


@router.post("/contact-candidates/{candidate_id}/dismiss")
async def dismiss_a_candidate(
    candidate_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    try:
        found = await lookup.dismiss(session, user.ctx, candidate_id=candidate_id)
    except lookup.LookupRefused as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    await session.commit()
    return {"dismissed": True}


@router.post("/contact-candidates/{candidate_id}/import", status_code=status.HTTP_201_CREATED)
async def import_a_candidate(
    candidate_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    campaign_id: Annotated[uuid.UUID, Body(embed=True)],
) -> dict[str, Any]:
    """Turn a proposal into a lead.

    **Through the ordinary importer, as a one-row sheet.** That looks indirect and it is the
    whole point: `importer.commit` is where the suppression check, the MX check, the
    already-in-this-campaign check, the free-mail warning and the batch id live. A second
    lead-creation path would be a second place every one of those could be forgotten -- and the
    one that would get forgotten is the suppression check, which is the one that must not be.

    So a contact a provider proposed is subject to exactly the rules a contact from a spreadsheet
    is, including that an address somebody opted out of is refused.
    """
    row = await session.scalar(
        select(ContactCandidate).where(
            ContactCandidate.id == candidate_id, ContactCandidate.org_id == user.org_id
        )
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    if row.state == "imported":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That contact is already a lead in this workspace.",
        )
    if not row.email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "That proposal has no email address. Apollo's search returns names and titles "
                "and keeps addresses behind an enrichment credit, so there is nothing to import "
                "yet."
            ),
        )

    campaign = await session.scalar(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.org_id == user.org_id)
    )
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such campaign.")

    # **The importer requires a company name and a provider does not give us one.** The shared
    # public-company cache (F19) usually has it, because this is a company somebody has already
    # looked at -- and when it does not, the fallback is the **domain itself**. A domain is a
    # true fact about the company; a trading name guessed from it ("acme.test" -> "Acme") is one
    # we would have made up, and hard rule 6 is about exactly that, even for a field this minor.
    cached = await company_cache.get(session, domain=row.company_domain)
    company_name = (cached.profile.name if cached else None) or row.company_domain

    sheet = importer.one_row_csv(
        email=row.email,
        full_name=row.full_name,
        company_name=company_name,
        company_url=f"https://{row.company_domain}",
        job_title=row.title,
        linkedin_url=row.profile_url,
    )
    try:
        leads, preview = await importer.commit(
            session,
            user.ctx,
            data=sheet,
            filename="contact.csv",
            campaign_id=campaign_id,
        )
        await first_job.queue_for(session, org_id=user.org_id, campaign=campaign, leads=leads)
    except SheetError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except QueueFull as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc

    if not leads:
        # The importer refused the row: suppressed, already in the campaign, or an unusable
        # address. Its own verdict is returned rather than a sentence of ours, because it knows
        # which of those it was and we would be guessing.
        await session.rollback()
        reasons = [reason for verdict in preview.rows for reason in verdict.reasons]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="; ".join(str(reason) for reason in reasons[:3])
            or "That contact cannot be imported into this campaign.",
        )

    row.state = "imported"
    row.lead_id = leads[0].id
    await session.commit()
    return {"lead_id": str(leads[0].id), "campaign_id": str(campaign_id)}
