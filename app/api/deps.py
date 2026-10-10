"""Who is asking, and what they may do.

`ARCHITECTURE_TARGET.md` §14. Two steps that are deliberately separate:

1. **Authentication** — the token is verified locally and yields a Supabase `sub`.
2. **Authorisation** — that `sub` is looked up in `members`, which yields the org and the role.

The second step is what makes tenancy real. `org_id` is **never** taken from the request: not
from a path, a query string, a body or a header. A caller who can name an org is not thereby
entitled to it, and every endpoint that accepted an org from its caller would be a tenancy bug
waiting for someone to try the obvious thing. It comes from the membership row or the request
fails.

A valid token with no membership row gets 403, not 404 or an empty list. Public sign-ups are
disabled, so that case means somebody authenticated against the project and has no business
here — worth saying plainly rather than quietly returning nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.security import AuthError, TokenClaims, TokenVerifier, bearer_token
from app.core.settings import Settings, config
from app.db.engine import get_sessionmaker
from app.db.models import Member
from app.db.repo import OrgContext

#: §14's roles, strongest first. Only `owner` is issued today.
ROLE_ORDER = {"owner": 3, "admin": 2, "member": 1}


@dataclass(frozen=True)
class CurrentUser:
    """An authenticated caller who belongs to exactly one org for this request."""

    user_id: str
    email: str | None
    org_id: uuid.UUID
    role: str

    @property
    def ctx(self) -> OrgContext:
        return OrgContext(org_id=self.org_id)

    def may(self, role: str) -> bool:
        return ROLE_ORDER.get(self.role, 0) >= ROLE_ORDER.get(role, 99)


_verifier: TokenVerifier | None = None


def get_verifier(settings: Settings | None = None) -> TokenVerifier:
    """Built once: the JWKS cache is only useful if it outlives a request."""
    global _verifier
    if _verifier is None:
        settings = settings or config
        if not settings.SUPABASE_URL:
            raise RuntimeError("SUPABASE_URL is not configured; the API cannot verify tokens")
        base = settings.SUPABASE_URL.rstrip("/")
        _verifier = TokenVerifier(
            jwks_url=settings.SUPABASE_JWKS_URL or f"{base}/auth/v1/.well-known/jwks.json",
            issuer=f"{base}/auth/v1",
        )
    return _verifier


def set_verifier(verifier: TokenVerifier | None) -> None:
    """Replace the process-wide verifier. For tests and for startup wiring."""
    global _verifier
    _verifier = verifier


async def get_session() -> AsyncSession:  # pragma: no cover - trivial wiring
    raise NotImplementedError("overridden by the app factory")


def _unauthorised(detail: str = "Not authenticated") -> HTTPException:
    # `WWW-Authenticate` is what tells a browser client to go and get a new token rather than
    # treating this as a permanent failure.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def current_claims(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> TokenClaims:
    """Verify the bearer token. Fails closed on anything unexpected."""
    try:
        token = bearer_token(authorization)
        claims = get_verifier().verify(token)
    except AuthError as exc:
        # The reason goes to the log, never to the caller: "expired" and "bad signature" are
        # both just "no" from the outside, and the difference is useful to an attacker.
        request.state.auth_error = str(exc)
        raise _unauthorised() from exc
    return claims


async def current_user(
    claims: Annotated[TokenClaims, Depends(current_claims)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CurrentUser:
    """Resolve the verified subject to a membership, or refuse."""
    member = await session.scalar(select(Member).where(Member.user_id == claims.subject))
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not a member of any organisation.",
        )
    return CurrentUser(
        user_id=claims.subject,
        email=claims.email or member.email,
        org_id=member.org_id,
        role=member.role,
    )


def require_role(role: str) -> Callable[..., Awaitable[CurrentUser]]:
    """Dependency factory: refuse anyone below `role`."""

    async def dependency(user: Annotated[CurrentUser, Depends(current_user)]) -> CurrentUser:
        if not user.may(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the {role} role.",
            )
        return user

    return dependency


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def session_dependency() -> AsyncIterator[AsyncSession]:
    """The real session dependency, installed by the app factory."""
    async with get_sessionmaker()() as session:
        yield session
