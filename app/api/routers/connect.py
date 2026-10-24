"""Connecting a Gmail account to a mailbox, by clicking rather than by typing a password.

Three endpoints and a strict rule about which of them may be reached without a token.

- `POST /connect/google/start` — authenticated. Mints a `state` nonce, stores who it belongs to,
  and returns the Google URL for the browser to visit.
- `GET /connect/google/callback` — **not** authenticated, because Google's redirect is a plain
  browser navigation carrying no `Authorization` header. The `state` row *is* the authentication:
  it was written by an authenticated request moments earlier and it names the org and the user.
  Without it anybody could hand a signed-in user a crafted callback and attach **their** mailbox
  to that user's org -- every campaign would then send through an attacker's account and every
  reply would land there.
- `DELETE /connect/google/{mailbox_id}` — authenticated. Forgets the credential.

**Nothing here returns a token, and nothing logs one.** The status endpoint answers whether a
mailbox is connected, as what address, and when it was last refreshed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserDep, SessionDep, require_role
from app.core.settings import config
from app.db.models import Mailbox, MailCredential, OAuthState
from app.db.state import record_event
from app.services import credentials as credential_store
from app.services.mail import google_oauth
from app.services.mail.connection import connection_status

logger = logging.getLogger(__name__)

router = APIRouter(tags=["connect"])

#: How long a half-finished OAuth round trip stays valid. Long enough to read a consent screen
#: and pick an account, short enough that an abandoned one is not lying around to be replayed.
STATE_TTL = timedelta(minutes=15)


@router.get("/connect/google/status")
async def google_status(
    user: CurrentUserDep,
    session: SessionDep,
    mailbox_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    """Whether this org's mailbox is connected, and whether the server can do it at all.

    `configured` is reported separately from `connected` on purpose: "nobody has connected a
    mailbox" and "this server has no Google credentials, so nobody can" are different problems
    with different fixes, and a single false would send somebody looking for the wrong one.
    """
    mailbox = await _mailbox_for(session, user, mailbox_id)
    base: dict[str, Any] = {
        "configured": config.google_oauth_configured,
        "encryption_ready": credential_store.is_configured(),
        "mailbox_id": str(mailbox.id) if mailbox else None,
        "from_email": mailbox.from_email if mailbox else None,
    }
    if mailbox is None:
        return {**base, "connected": False, "needs_reconnect": False}
    return {**base, **await connection_status(session, mailbox_id=mailbox.id)}


@router.post("/connect/google/start", dependencies=[Depends(require_role("admin"))])
async def start_google(
    user: CurrentUserDep,
    session: SessionDep,
    mailbox_id: Annotated[uuid.UUID | None, Body(embed=True)] = None,
) -> dict[str, Any]:
    """Begin the flow. Returns the URL the browser should go to."""
    if not credential_store.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This server cannot store mailbox credentials yet (CREDENTIAL_KEY is not set), "
                "so connecting an account would lose the connection immediately."
            ),
        )

    mailbox = await _mailbox_for(session, user, mailbox_id)
    if mailbox is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Add a mailbox to this campaign first, then connect it.",
        )

    try:
        state = google_oauth.new_state()
        url = google_oauth.authorize_url(state)
    except google_oauth.OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    # Expired rows are cleared on the way past rather than by a scheduled sweep: this table is
    # tiny, the rows are worthless once stale, and a job to tidy it would be more moving parts
    # than the problem deserves.
    await session.execute(
        delete(OAuthState).where(OAuthState.created_at < datetime.now(tz=UTC) - STATE_TTL)
    )
    session.add(
        OAuthState(
            state=state,
            org_id=user.org_id,
            user_id=user.user_id,
            mailbox_id=mailbox.id,
        )
    )
    await session.commit()
    return {"authorize_url": url}


@router.get("/connect/google/callback")
async def google_callback(
    session: SessionDep,
    state: Annotated[str | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Where Google sends the browser back.

    **Unauthenticated by necessity and safe by the nonce.** A redirect from Google carries no
    `Authorization` header, so the `state` row is what says whose connection this is -- it was
    written by an authenticated request minutes earlier, it is single use, and it expires.

    Always ends in a redirect, never in JSON: the person is looking at a browser tab, and a page
    of JSON is not an answer to "did that work?".
    """
    if error:
        # The ordinary case is somebody pressing Cancel on the consent screen.
        return _back(f"Google did not connect the account ({error}).", ok=False)
    if not state or not code:
        return _back("That link was incomplete. Start again from the dashboard.", ok=False)

    row = await session.scalar(select(OAuthState).where(OAuthState.state == state))
    if row is None:
        return _back("That connection link is not valid. Start again from the dashboard.", ok=False)
    if row.used_at is not None:
        # A replayed callback. Refused rather than quietly re-run: a second exchange of a
        # one-time code fails anyway, and saying so is clearer than a Google error.
        return _back("That connection link was already used.", ok=False)
    if row.created_at < datetime.now(tz=UTC) - STATE_TTL:
        return _back("That connection link expired. Start again from the dashboard.", ok=False)

    row.used_at = datetime.now(tz=UTC)

    try:
        tokens = await google_oauth.exchange_code(code)
    except google_oauth.OAuthError as exc:
        await session.commit()
        return _back(str(exc), ok=False)

    assert tokens.refresh_token is not None, "exchange_code refuses a response without one"
    email = await google_oauth.account_email(tokens.access_token)

    mailbox = await session.get(Mailbox, row.mailbox_id) if row.mailbox_id else None
    if mailbox is None or mailbox.org_id != row.org_id:
        await session.commit()
        return _back("That mailbox no longer exists.", ok=False)

    # **Did they authorise the account they meant to?** With several Google accounts signed in,
    # picking the wrong one on the consent screen is easy, and without this check the mistake
    # only shows up as mail arriving from an unexpected address -- after it has been sent.
    if email and email.lower() != mailbox.from_email.lower():
        await session.commit()
        return _back(
            f"You approved {email}, but this mailbox sends as {mailbox.from_email}. "
            "Connect the matching account, or change the mailbox address first.",
            ok=False,
        )

    try:
        sealed = credential_store.seal(tokens.refresh_token)
    except credential_store.CredentialError as exc:
        await session.commit()
        return _back(str(exc), ok=False)

    existing = await session.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
    )
    if existing is None:
        existing = MailCredential(org_id=row.org_id, mailbox_id=mailbox.id, kind="google_oauth")
        session.add(existing)

    existing.kind = "google_oauth"
    existing.account_email = email or mailbox.from_email
    existing.secret = sealed.token
    existing.scopes = tokens.scopes
    existing.expires_at = tokens.expires_at
    existing.last_refreshed_at = datetime.now(tz=UTC)
    # Reconnecting is the fix for an expired token, so clearing this is the whole point.
    existing.invalid_reason = None
    existing.connected_by = row.user_id

    # A mailbox paused *because* its connection lapsed is un-paused by reconnecting. One paused
    # by a person for any other reason is not: this only lifts the pause it set itself.
    if mailbox.paused and (mailbox.pause_reason or "").startswith(
        ("Google would not renew", "this credential cannot be decrypted")
    ):
        mailbox.paused = False
        mailbox.pause_reason = None

    await record_event(
        session,
        org_id=row.org_id,
        type="mailbox.connected",
        actor="user",
        actor_id=row.user_id,
        data={"mailbox_id": str(mailbox.id), "account_email": existing.account_email},
    )
    await session.commit()
    logger.info("mailbox connected", extra={"mailbox_id": str(mailbox.id)})
    return _back(f"{existing.account_email} is connected.", ok=True)


@router.delete("/connect/google/{mailbox_id}", dependencies=[Depends(require_role("admin"))])
async def disconnect_google(
    mailbox_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    """Forget this mailbox's stored credential.

    Does **not** revoke the grant at Google — that is the user's own page, and a button here
    claiming to do it would be claiming more than it does. The campaign stops sending because
    there is nothing to authenticate with, which is what disconnecting means.
    """
    mailbox = await session.get(Mailbox, mailbox_id)
    if mailbox is None or mailbox.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found.")

    await session.execute(delete(MailCredential).where(MailCredential.mailbox_id == mailbox.id))
    mailbox.paused = True
    mailbox.pause_reason = "the mailbox was disconnected"
    await record_event(
        session,
        org_id=user.org_id,
        type="mailbox.disconnected",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"mailbox_id": str(mailbox.id)},
    )
    await session.commit()
    return {"disconnected": True, "revoke_at": "https://myaccount.google.com/permissions"}


async def _mailbox_for(
    session: AsyncSession, user: CurrentUserDep, mailbox_id: uuid.UUID | None
) -> Mailbox | None:
    """This org's mailbox. `org_id` comes from the membership, never from the caller."""
    stmt = select(Mailbox).where(Mailbox.org_id == user.org_id)
    if mailbox_id is not None:
        stmt = stmt.where(Mailbox.id == mailbox_id)
    return await session.scalar(stmt.order_by(Mailbox.created_at).limit(1))


def _back(message: str, *, ok: bool) -> RedirectResponse:
    """Send the browser back to the dashboard with something to show the person."""
    base = (config.DASHBOARD_URL or "/").rstrip("/")
    from urllib.parse import quote

    return RedirectResponse(
        url=f"{base}/?connected={'1' if ok else '0'}&message={quote(message)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
