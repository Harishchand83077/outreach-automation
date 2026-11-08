"""Issuing codes and tokens (F14, ADR-058).

The rules are in `app/domain/oauth.py` and have no I/O; this is the part that needs rows. Two
operations: a person consents and gets a code, and a client exchanges that code for a token.

**An access token is an `api_tokens` row**, which is the design decision the whole slice rests
on. The tool catalogue, the scope gate, the untrusted-text wrapper and -- most importantly --
the re-resolution of the membership on every single call are all untouched, because an OAuth
token is not a second kind of credential with its own parallel rules to keep in step. It is a
different way to *obtain* the same credential. The two columns that differ say which client
holds it and which resource it is for.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiToken, Member, OAuthClient, OAuthCode, OAuthRefreshToken
from app.domain import oauth
from app.services import api_tokens


@dataclass(frozen=True)
class IssuedTokens:
    """What goes back to the client. The secrets are here and are never stored in clear."""

    access_token: str
    refresh_token: str
    expires_in: int
    scopes: tuple[str, ...]


async def client_for(session: AsyncSession, client_id: str, *, org_id: uuid.UUID) -> OAuthClient:
    """The client **in this workspace**, or raise.

    Scoped by `org_id`, so a client registered in one workspace cannot be named on another
    workspace's consent screen. A disabled client is refused exactly as an unknown one is: the
    difference is useful only to somebody enumerating.
    """
    row = await session.scalar(
        select(OAuthClient).where(
            OAuthClient.org_id == org_id, OAuthClient.client_id == (client_id or "")
        )
    )
    if row is None or row.disabled_at is not None:
        raise oauth.OAuthError("invalid_client", "Unknown client.")
    return row


async def authorize(
    session: AsyncSession,
    *,
    client_id: str,
    user_id: str,
    org_id: uuid.UUID,
    redirect_uri: str,
    scope: str | None,
    resource: str | None,
    code_challenge: str,
    code_challenge_method: str,
    expected_resource: str,
    allowed_scopes: tuple[str, ...],
    now: datetime | None = None,
) -> tuple[str, str]:
    """Record a consent and return `(code, redirect_uri)`.

    Everything is checked **before** a row is written, so a refused request leaves nothing
    behind for anybody to find or replay.
    """
    moment = now or datetime.now(UTC)
    client = await client_for(session, client_id, org_id=org_id)

    redirect = oauth.check_redirect_uri(redirect_uri, list(client.redirect_uris or []))
    target = oauth.check_resource(resource, expected_resource)
    scopes = oauth.check_scopes(scope, allowed_scopes)

    if code_challenge_method != oauth.CHALLENGE_METHOD:
        raise oauth.OAuthError(
            "invalid_request",
            "code_challenge_method must be S256. `plain` is a challenge equal to its verifier, "
            "which protects against nothing.",
        )
    if not code_challenge:
        raise oauth.OAuthError(
            "invalid_request",
            "A code_challenge is required: OAuth 2.1 requires PKCE of every client, because an "
            "authorization code travels through a browser redirect and can be stolen there.",
        )

    code, code_hash = oauth.new_code()
    session.add(
        OAuthCode(
            code_hash=code_hash,
            client_id=client.client_id,
            user_id=user_id,
            org_id=org_id,
            scopes=list(scopes),
            redirect_uri=redirect,
            resource=target,
            code_challenge=code_challenge,
            code_challenge_method=oauth.CHALLENGE_METHOD,
            expires_at=oauth.expiry(moment, oauth.CODE_TTL),
        )
    )
    await session.flush()
    return code, redirect


async def exchange_code(
    session: AsyncSession,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    resource: str | None,
    expected_resource: str,
    now: datetime | None = None,
) -> IssuedTokens:
    """Trade an authorization code for tokens.

    **Every failure is `invalid_grant` with one sentence.** Unknown, expired, replayed, wrong
    client, wrong redirect and wrong verifier are six different mistakes and one answer, because
    distinguishing them tells somebody guessing which half of their guess was right.
    """
    moment = now or datetime.now(UTC)
    refusal = oauth.OAuthError("invalid_grant", "That code cannot be exchanged.")

    row = await session.scalar(
        select(OAuthCode).where(OAuthCode.code_hash == oauth.hash_secret(code or ""))
    )
    if row is None or row.used_at is not None or moment >= row.expires_at:
        raise refusal
    if row.client_id != client_id or row.redirect_uri != redirect_uri:
        raise refusal
    if not oauth.verify_challenge(code_verifier, row.code_challenge, row.code_challenge_method):
        raise refusal
    # The resource is checked again here and not merely trusted from the code: the token carries
    # it, and a token's audience must be the one the person actually consented to.
    if resource is not None and resource.rstrip("/") != row.resource.rstrip("/"):
        raise refusal
    oauth.check_resource(row.resource, expected_resource)

    # **Single use, marked before anything is issued.** Two concurrent exchanges of one code
    # must not both succeed, and the unique index on `code_hash` plus this write is what makes
    # the second one lose.
    row.used_at = moment

    member = await _member_or_refuse(session, user_id=row.user_id, org_id=row.org_id)
    return await _issue(
        session,
        client_id=row.client_id,
        user_id=row.user_id,
        org_id=member.org_id,
        scopes=tuple(row.scopes),
        resource=row.resource,
        now=moment,
    )


async def refresh(
    session: AsyncSession,
    *,
    refresh_token: str,
    client_id: str,
    expected_resource: str,
    now: datetime | None = None,
) -> IssuedTokens:
    """Rotate a refresh token and issue a new access token.

    **A reused refresh token revokes the whole chain.** Somebody presenting a token that was
    already exchanged is either a client that lost the response or a copy of a stolen token, and
    the safe answer to both is the same: stop. The alternative -- letting the real client carry
    on -- means a stolen token keeps working for ninety days and nothing ever notices.
    """
    moment = now or datetime.now(UTC)
    refusal = oauth.OAuthError("invalid_grant", "That refresh token cannot be used.")

    row = await session.scalar(
        select(OAuthRefreshToken).where(
            OAuthRefreshToken.token_hash == oauth.hash_secret(refresh_token or "")
        )
    )
    if row is None or row.client_id != client_id:
        raise refusal
    if row.replaced_by_id is not None:
        await _revoke_chain(session, row, now=moment)
        # **Committed before raising, and that is a bug this test caught.** The route turns an
        # `OAuthError` into a response and rolls the transaction back, which is right for every
        # other refusal -- a refused request should leave nothing behind. This one is the
        # exception: the refusal *is* a write, and rolling it back un-revokes the chain we just
        # revoked, so a stolen token keeps working and the detection achieved nothing. Same
        # shape as the proposal that committed before its audit row (ADR-030): the ordering is
        # the defect, not the code around it.
        await session.commit()
        raise oauth.OAuthError(
            "invalid_grant",
            "That refresh token was already used. Every token in its chain has been revoked; "
            "connect again.",
        )
    if row.revoked_at is not None or moment >= row.expires_at:
        raise refusal

    member = await _member_or_refuse(session, user_id=row.user_id, org_id=row.org_id)
    issued = await _issue(
        session,
        client_id=row.client_id,
        user_id=row.user_id,
        org_id=member.org_id,
        scopes=tuple(row.scopes),
        resource=row.resource,
        now=moment,
    )

    # The old access token goes with the old refresh token, or an hour of it stays live beside
    # the replacement for no reason.
    if row.access_token_id is not None:
        old = await session.get(ApiToken, row.access_token_id)
        if old is not None and old.revoked_at is None:
            old.revoked_at = moment
    row.revoked_at = moment
    row.replaced_by_id = await _refresh_row_id(session, issued.refresh_token)
    await session.flush()
    return issued


async def _member_or_refuse(session: AsyncSession, *, user_id: str, org_id: uuid.UUID) -> Member:
    """The membership, or refuse.

    Checked here as well as on every call, so a code or a refresh token that outlived the
    membership that justified it issues nothing -- an ex-colleague's connector must not be able
    to refresh its way back in.
    """
    member = await session.scalar(
        select(Member).where(Member.user_id == user_id, Member.org_id == org_id)
    )
    if member is None:
        raise oauth.OAuthError("invalid_grant", "That account is no longer in this workspace.")
    return member


async def _refresh_row_id(session: AsyncSession, token: str) -> uuid.UUID | None:
    row = await session.scalar(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == oauth.hash_secret(token))
    )
    return row.id if row else None


async def _revoke_chain(session: AsyncSession, row: OAuthRefreshToken, *, now: datetime) -> None:
    """Revoke every refresh token for this client and person, and their access tokens."""
    rows = (
        await session.scalars(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.org_id == row.org_id,
                OAuthRefreshToken.user_id == row.user_id,
                OAuthRefreshToken.client_id == row.client_id,
            )
        )
    ).all()
    for entry in rows:
        if entry.revoked_at is None:
            entry.revoked_at = now
        if entry.access_token_id is not None:
            token = await session.get(ApiToken, entry.access_token_id)
            if token is not None and token.revoked_at is None:
                token.revoked_at = now
    await session.flush()


async def _issue(
    session: AsyncSession,
    *,
    client_id: str,
    user_id: str,
    org_id: uuid.UUID,
    scopes: tuple[str, ...],
    resource: str,
    now: datetime,
) -> IssuedTokens:
    """Write the access token and its refresh token."""
    secret, prefix, token_hash = api_tokens.mint_secret()
    access = ApiToken(
        org_id=org_id,
        user_id=user_id,
        # The client's id rather than a person's label: these are not listed beside hand-minted
        # tokens to be told apart, they are listed to be revoked by client.
        name=f"{client_id} (connected)",
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=list(scopes),
        expires_at=oauth.expiry(now, oauth.ACCESS_TTL),
        client_id=client_id,
        resource=resource,
    )
    session.add(access)
    await session.flush()

    refresh_secret, refresh_hash = oauth.new_refresh_token()
    session.add(
        OAuthRefreshToken(
            token_hash=refresh_hash,
            client_id=client_id,
            user_id=user_id,
            org_id=org_id,
            scopes=list(scopes),
            resource=resource,
            access_token_id=access.id,
            expires_at=oauth.expiry(now, oauth.REFRESH_TTL),
        )
    )
    await session.flush()

    return IssuedTokens(
        access_token=secret,
        refresh_token=refresh_secret,
        expires_in=int(oauth.ACCESS_TTL.total_seconds()),
        scopes=scopes,
    )
