"""The booking webhook, and the settings that configure it (F15, ADR-045).

Two routers in one module, deliberately, because they are two halves of one feature and the
things that must stay true about them are easiest to read together:

- **`POST /hooks/booking/{token}`** is public and mounted at the root. A booking tool posts to it
  from its own servers with no session, so it is the second unauthenticated write in this system
  after the one-click unsubscribe -- and everything about it is narrowing that. It takes **no
  `org_id` from the caller** (ADR-004): the token proves which workspace, and the body must then
  carry a signature made with that workspace's own secret.
- **`/booking-webhook` and `/bookings`** are the ordinary dashboard door, admin-gated, where a
  person generates the secret and sees what has arrived.

**No `GET` on the public path, and that is a safety property rather than an omission.** The
unsubscribe endpoint has one because a human clicks that link; nobody clicks a webhook URL, and
the things that *do* fetch URLs found in text -- scanners, link previews, a provider's own
"test" button on a GET -- would otherwise be able to reach a write path. A `GET` here would be a
page explaining how to book a meeting for somebody, to nobody.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep, SettingsDep, require_role
from app.db.models import BookingEvent, Org
from app.db.repo import OrgContext
from app.services import credentials
from app.services.booking import intake, payload, secrets_store, signature

logger = logging.getLogger(__name__)

#: The public half. Mounted at the root by `create_app`, outside `/api/v1`.
hooks = APIRouter(tags=["booking"])
#: The dashboard half.
router = APIRouter(tags=["booking"])

PATH = "/hooks/booking/{token}"


@hooks.post(PATH, include_in_schema=False)
async def receive(
    token: str, request: Request, session: SessionDep, settings: SettingsDep
) -> Response:
    """Record a signed booking, and move the lead to `MEETING_BOOKED`.

    **Every refusal answers the same way: 204, having done nothing.** A public endpoint that
    distinguishes an unknown workspace from a bad signature from an unreadable body is an oracle
    for all three, and a provider's retry logic reads any non-2xx as "try again" -- so answering
    401 to a body we will never accept buys a retry storm and tells an attacker which guess was
    closer. What the operator sees instead is the dashboard saying no booking has ever arrived,
    which is the symptom they can act on.

    The one exception is a body too large, which is refused with 413 before it is read: a
    provider sending megabytes is a configuration error worth reporting to the sender.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > payload.MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    # **The raw bytes.** Verifying a signature against JSON we parsed and re-serialised is the
    # classic way a correct-looking implementation accepts a forged body: key order, spacing and
    # number formatting all change under the round trip.
    body = await request.body()
    if len(body) > payload.MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    # **The app's own settings, not the module-level singleton.** The singleton is built at
    # import time and reads `.env`, so a route reaching for it is partially ignoring the
    # `Settings` its app was constructed with -- the gap `SettingsDep` was added for in F9. The
    # signing key here has to be the one `generate_secret` signed the URL with, or every
    # delivery is refused for a reason nobody could find.
    org_id = signature.verify_token(token, secret=signature.signing_key(settings.CREDENTIAL_KEY))
    if org_id is None:
        await session.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    org = await session.get(Org, org_id)
    if org is None:
        await session.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    ctx = OrgContext(org_id=org_id, actor_id="booking")
    secret = await secrets_store.plaintext(session, ctx, settings=settings)
    if not secret:
        await session.rollback()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if not signature.verify_body(body, headers=dict(request.headers), secret=secret):
        await session.rollback()
        # Logged without the signature, the token or the body: this line reaches a log file that
        # is read in a terminal and pasted into issues.
        logger.info(
            "a booking webhook failed signature verification", extra={"org_id": str(org_id)}
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    try:
        data = json.loads(body.decode("utf-8"))
        booking = payload.read(data, provider_hint="")
    except (UnicodeDecodeError, json.JSONDecodeError, payload.BookingUnreadable) as exc:
        await session.rollback()
        # **A verified body we cannot read is worth a log line**, unlike the refusals above: the
        # signature proved it came from this workspace's own tool, so this is a shape we should
        # support and do not. The message names the missing field and no payload content.
        logger.warning(
            "a verified booking webhook could not be read",
            extra={"org_id": str(org_id), "problem": str(exc)[:200]},
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    result = await intake.record(session, ctx, booking)
    await secrets_store.touch(session, ctx)
    await session.commit()
    logger.info(
        "a booking webhook was accepted",
        extra={
            "org_id": str(org_id),
            "created": result.created,
            "matched_by": result.matched_by,
            "state_changed": result.state_changed,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- the dashboard half


def _public_url(token: str, *, base_url: str) -> str:
    """The URL to paste into a booking tool, or empty when no public base is configured.

    Empty is the ordinary state of a deployment that has not set `PUBLIC_API_URL`, and the screen
    says so rather than printing a URL built on a guess -- ADR-033's rule, for the same reason: a
    webhook pointed at a guessed host delivers somebody's meetings into the void.
    """
    base = (base_url or "").strip().rstrip("/")
    return f"{base}/hooks/booking/{token}" if base else ""


@router.get("/booking-webhook")
async def describe(
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """The URL, and whether a secret exists. **Never the secret itself.**"""
    ctx = OrgContext(org_id=user.org_id)
    row = await secrets_store.live(session, ctx)
    token = signature.sign_token(
        org_id=user.org_id, secret=signature.signing_key(settings.CREDENTIAL_KEY)
    )
    return {
        "configured": row is not None,
        "url": _public_url(token, base_url=settings.PUBLIC_API_URL) if row else "",
        "public_base_set": bool((settings.PUBLIC_API_URL or "").strip()),
        "created_at": row.created_at.isoformat() if row is not None else None,
        # The one number that tells "no meetings yet" from "this was never wired up".
        "last_delivery_at": (
            row.last_used_at.isoformat() if row is not None and row.last_used_at else None
        ),
        "signature_headers": list(signature.SIGNATURE_HEADERS),
        "note": (
            "Paste this URL into your booking tool's webhook settings and give it the secret as "
            "the signing secret. Each delivery must be signed with HMAC-SHA256 over the raw "
            "request body. A booking that matches a lead's email address moves that lead to "
            "Meeting booked; one that matches nobody is still recorded so you can see it."
        ),
    }


@router.post("/booking-webhook/secret", status_code=status.HTTP_201_CREATED)
async def generate_secret(
    session: SessionDep,
    settings: SettingsDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Generate a secret and return it **once**. A previous one stops working immediately."""
    if not credentials.is_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This deployment has no CREDENTIAL_KEY, so a secret cannot be stored.",
        )
    ctx = OrgContext(org_id=user.org_id)
    minted = await secrets_store.generate(
        session, ctx, settings=settings, created_by=user.email or user.user_id
    )
    token = signature.sign_token(
        org_id=user.org_id, secret=signature.signing_key(settings.CREDENTIAL_KEY)
    )
    await session.commit()
    return {
        "secret": minted.secret,
        "url": _public_url(token, base_url=settings.PUBLIC_API_URL),
        "note": (
            "This is the only time this secret is shown. Paste it into your booking tool now; "
            "if you lose it, generate another one -- the old one stops working at that moment."
        ),
    }


@router.delete("/booking-webhook/secret")
async def revoke_secret(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
) -> dict[str, Any]:
    """Turn the webhook off. Bookings already recorded are untouched."""
    revoked = await secrets_store.revoke(session, OrgContext(org_id=user.org_id))
    await session.commit()
    return {"revoked": revoked}


@router.get("/bookings")
async def list_bookings(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    limit: int = 50,
) -> dict[str, Any]:
    """What has arrived, including bookings that matched nobody.

    **The unmatched ones are the point of this screen.** A booking from an address in no campaign
    usually means the lead replied from a second address or forwarded the link, and without a
    list of them the only symptom is a funnel that is quietly one meeting short.
    """
    rows = list(
        (
            await session.scalars(
                select(BookingEvent)
                .where(BookingEvent.org_id == user.org_id)
                .order_by(BookingEvent.received_at.desc())
                .limit(max(1, min(limit, 200)))
            )
        ).all()
    )
    return {
        "bookings": [
            {
                "id": str(row.id),
                "lead_id": str(row.lead_id) if row.lead_id else None,
                "provider": row.provider,
                "kind": row.kind,
                "invitee_email": row.invitee_email_norm,
                "invitee_name": row.invitee_name,
                "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
                # In words, so somebody reviewing a funnel can see *why* this booking was
                # attributed to this lead rather than trusting that it was.
                "matched_by": row.matched_by,
                "received_at": row.received_at.isoformat(),
            }
            for row in rows
        ],
        "unmatched": sum(1 for row in rows if row.lead_id is None),
    }
