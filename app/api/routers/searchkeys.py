"""A workspace's own search keys, and the order F17's chain tries them in (ADR-046).

Four things a person does: see which providers they can search with, save their own key, prove
it works, and stop using it. The same shape as F9's `/provider-keys`, deliberately, because it
is the same table, the same encryption and the same rule -- **there is no shared key**, and a
workspace with none degrades to the company's own pages rather than failing.

**No `GET` here talks to a provider.** `GET /mailboxes` is non-networked because refreshing it
would mint OAuth tokens; here a networked `GET` would spend one of the user's credits every time
somebody pressed F5. Verifying is a `POST` somebody pressed, and it says that it costs a credit
rather than presenting itself as free.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, SettingsDep, require_role
from app.db.models import SEARCH_PROVIDERS
from app.db.repo import OrgContext
from app.services.contacts import keys
from app.services.research import chain
from app.services.research.search import SearchBudget, SearchKeyRefused, SearchUnavailable

router = APIRouter(tags=["search"])

#: The query the verify button searches for. A real search is the only check these providers
#: offer, so verification costs a credit -- and it is spent on a phrase that tells us nothing
#: about anybody, rather than on a real company somebody did not ask us to look up. `example.com`
#: is reserved by RFC 2606 and belongs to nobody, which is the same choice the contact verify
#: made for the same reason.
VERIFY_QUERY = "example.com"


@router.get("/search-keys")
async def list_search_keys(
    user: CurrentUserDep, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    """Which providers this workspace can search with, in the order they would be tried.

    `order` is the chain as it would actually run, which is the thing worth showing: a list of
    configured keys does not tell anybody which one pays for the next search, and that is the
    only question the screen exists to answer. The deployment entry appears in it when one is
    configured, named as shared, because a workspace reading "search works" without knowing
    whose credits it spends has been told something misleading.
    """
    statuses = await keys.statuses(session, user.ctx, family="search")
    budget = SearchBudget(session, user.org_id, monthly=settings.TAVILY_MONTHLY_CREDIT_BUDGET)

    order: list[dict[str, Any]] = []
    for status_ in statuses:
        if status_.connected and status_.usable:
            order.append(
                {
                    "provider": status_.provider,
                    "shared": False,
                    "used_this_month": await budget.used_this_month(provider=status_.provider),
                }
            )
    if str(settings.TAVILY_API_KEY or "").strip():
        order.append(
            {
                "provider": chain.DEPLOYMENT_MEMBER,
                "shared": True,
                "used_this_month": await budget.used_this_month(provider=chain.DEPLOYMENT_MEMBER),
            }
        )

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
        "order": order,
        "monthly_budget_per_provider": budget.monthly,
        "used_this_month": await budget.used_this_month(),
        "note": (
            "A search runs only when a company's own pages say too little, and a cached answer "
            "costs nothing. The chain stops at the first provider that answers, so the order is "
            "what decides which key is spent."
        ),
    }


@router.put("/search-keys/{provider}")
async def save_a_search_key(
    provider: str,
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    api_key: Annotated[str, Body(embed=True)],
    label: Annotated[str | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Store this workspace's own key for one search provider, replacing any previous one.

    `admin`, like the send allowlist and like a contact provider's key: a key spends the
    workspace's money, and a member reviews drafts rather than deciding what the workspace pays
    for. Saving does not call the provider -- the verify button does, and says what it costs.
    """
    try:
        row = await keys.store(
            session,
            user.ctx,
            provider=provider,
            secret=api_key,
            label=label,
            settings=settings,
            allowed=SEARCH_PROVIDERS,
        )
    except keys.ProviderKeyError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    # The response says what was stored and never any part of it.
    return {"provider": row.provider, "kind": row.kind, "connected": True, "verified": False}


@router.delete("/search-keys/{provider}")
async def revoke_a_search_key(
    provider: str,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Stop using a key. The row is kept, revoked, so the change itself stays auditable."""
    try:
        removed = await keys.revoke(session, user.ctx, provider=provider, allowed=SEARCH_PROVIDERS)
    except keys.ProviderKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    await session.commit()
    return {"provider": provider.lower(), "revoked": removed}


@router.post("/search-keys/{provider}/verify")
async def verify_a_search_key(
    provider: str,
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Prove the key works, by doing the smallest real thing it can do.

    There is no cheaper check: none of these providers offers an "is this key valid?" endpoint,
    so verification is a search and **costs one credit**, which the response says rather than
    implying it was free. The alternative is ADR-021's problem again -- a key pasted with a
    trailing space, a green tick, and a search fallback that quietly never runs.

    It runs **one member only**, not the chain: the question is whether *this* key works, and a
    chain would answer it by falling through to a different provider and reporting success.
    """
    try:
        name = keys.check_provider(provider, allowed=SEARCH_PROVIDERS)
    except keys.ProviderKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    credential = await keys.live(session, user.ctx, provider=name, allowed=SEARCH_PROVIDERS)
    if credential is None:
        return {
            "provider": name,
            "ok": False,
            "reason": (
                f"this workspace has no {name} key. Add your own; there is no shared key, by "
                "design."
            ),
        }

    budget = SearchBudget(session, user.org_id, monthly=settings.TAVILY_MONTHLY_CREDIT_BUDGET)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        members = await chain.build_chain(
            session, OrgContext(org_id=user.org_id), client=client, settings=settings
        )
        mine = [member for member in members if member.usage_name == name]
        if not mine:
            # The chain leaves out a key the provider already refused, which is the one case
            # where "no member" and "no key" are different answers.
            return {
                "provider": name,
                "ok": False,
                "reason": (
                    f"the {name} key was refused the last time it was used. Save it again, or "
                    "check the plan and credit on your account."
                ),
            }
        try:
            await mine[0].provider.search(VERIFY_QUERY, max_results=1)
        except SearchKeyRefused as exc:
            await keys.mark_invalid(session, credential_id=credential.id, reason=str(exc))
            # Committed, or the refusal rolls back with the response and the next press calls
            # the provider again with the key it just told us was dead.
            await session.commit()
            return {"provider": name, "ok": False, "reason": str(exc)}
        except SearchUnavailable as exc:
            # Not marked invalid: an outage is theirs and the key may be perfect.
            return {"provider": name, "ok": False, "reason": str(exc)}

    await keys.clear_invalid(session, credential_id=credential.id)
    await budget.record(provider=name, credits=1)
    await session.commit()
    return {
        "provider": name,
        "ok": True,
        "note": (
            "The key works. This check is a real search, so it spent one of your credits and "
            "counts against this month's ceiling."
        ),
    }
