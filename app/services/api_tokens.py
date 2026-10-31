"""Minting and verifying the MCP endpoint's own credentials (ADR-028).

**This module never sees a Supabase token and never calls `current_user`.** That is asserted by
`tests/unit/test_mcp_safety.py`, and the reason is specific: Supabase accepts the RFC 8707
`resource` parameter and ignores it, issuing `aud: "authenticated"` whatever was requested
(supabase/auth#2610). A token whose audience is a constant cannot be shown to have been minted
*for* this endpoint, so accepting one here would mean accepting a credential obtained for
something else — the confused-deputy problem resource indicators exist to prevent.

So there are two doors and each takes one kind of key. A Supabase JWT is refused by `/mcp`; one
of these is refused everywhere else.

**The secret is returned exactly once**, at creation, and only the hash is kept. There is no
route that reads a token back, because a credential a route can read is a credential a stolen
session can read.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiToken, Member
from app.services.mcp import protocol

#: Identifies our tokens at a glance, in a log, in a paste and in a client's settings screen.
#: A credential nobody can recognise is one nobody reports when they find it somewhere it should
#: not be.
PREFIX = "oa_"

#: 32 bytes from `secrets`, so roughly 256 bits before base64. The hash below is deliberately
#: unstretched; see the model's docstring.
ENTROPY_BYTES = 32

#: The default offered by the UI. A credential that never expires is one that outlives the
#: reason it was made.
DEFAULT_TTL_DAYS = 90

#: Re-exported from the protocol module, which is the one place the set is defined: the
#: database's check constraint, the minting route and the tool gate must all mean the same thing
#: by `mcp:propose`, and a literal copied into a second module is how they stop meaning it.
READ_SCOPE = protocol.READ_SCOPE
PROPOSE_SCOPE = protocol.PROPOSE_SCOPE

#: What a token gets when nobody says otherwise. Reading is the default because a credential
#: that leaves the browser should start as narrow as it can be and be widened on purpose --
#: `SEND_MODE=disabled` and `SIGNUP_MODE=closed` are the same decision.
DEFAULT_SCOPES: tuple[str, ...] = (READ_SCOPE,)


def hash_token(token: str) -> str:
    """`sha256` hex. See `ApiToken` for why this is not a password hash."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mint() -> tuple[str, str, str]:
    secret = f"{PREFIX}{secrets.token_urlsafe(ENTROPY_BYTES)}"
    return secret, secret[: len(PREFIX) + 8], hash_token(secret)


@dataclass(frozen=True)
class MintedToken:
    """The one and only time the secret exists outside the client's settings."""

    id: uuid.UUID
    name: str
    secret: str
    prefix: str
    expires_at: datetime | None
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # `Sealed` and `MailLogin` do the same. A traceback must not print a credential.
        return f"MintedToken(id={self.id!r}, name={self.name!r}, secret='[redacted]')"


@dataclass(frozen=True)
class TokenBearer:
    """A verified caller: the token, and the membership it still resolves to.

    It carries `org_id` and `role` from the **membership row**, re-read on every call, never
    from the token. The token says who is asking; the membership says what they may reach.
    """

    token_id: uuid.UUID
    user_id: str
    org_id: uuid.UUID
    role: str
    scopes: tuple[str, ...]

    def may_read(self) -> bool:
        return READ_SCOPE in self.scopes

    def may_propose(self) -> bool:
        """Whether this credential may write a draft into the review queue (ADR-030).

        A separate question from `may_read`, and asked separately: most tokens are read-only,
        and a token that may propose is one somebody deliberately asked for.
        """
        return PROPOSE_SCOPE in self.scopes

    def permits(self, scope: str) -> bool:
        """Whether this credential carries one named scope.

        The tool says which scope it needs and this answers; neither hard-codes the other, so a
        tool added later is refused by default rather than permitted by omission.
        """
        return scope in self.scopes


async def create_token(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: str,
    name: str,
    ttl_days: int | None = DEFAULT_TTL_DAYS,
    scopes: Sequence[str] | None = None,
    now: datetime | None = None,
) -> MintedToken:
    """Mint a token. The caller must already have been authorised by the route.

    `scopes` defaults to read-only. An unknown scope is refused **here**, before the INSERT, so
    the error names the scope rather than arriving as a constraint violation nobody can read --
    but the check constraint is still the authority, because this function is not the only way a
    row can be written and a guard in application code does not bind `psql`.
    """
    moment = now or datetime.now(UTC)
    requested = tuple(scopes) if scopes is not None else DEFAULT_SCOPES
    if not requested:
        raise ValueError("a token with no scopes could do nothing; ask for at least one")
    if unknown := [scope for scope in requested if scope not in protocol.SCOPES]:
        raise ValueError(
            f"unknown scope(s): {', '.join(sorted(unknown))}. "
            f"This endpoint has {', '.join(protocol.SCOPES)} and nothing else."
        )
    secret, prefix, digest = _mint()
    expires_at = moment + timedelta(days=ttl_days) if ttl_days else None

    row = ApiToken(
        org_id=org_id,
        user_id=user_id,
        name=name.strip()[:80] or "Unnamed",
        token_prefix=prefix,
        token_hash=digest,
        # Whatever was asked for, from the one set the database will accept. There is no scope
        # here that could authorise a send -- see ADR-027 and ADR-030.
        scopes=list(requested),
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()

    return MintedToken(
        id=row.id,
        name=row.name,
        secret=secret,
        prefix=prefix,
        expires_at=expires_at,
        scopes=requested,
    )


async def verify_token(
    session: AsyncSession, presented: str, *, now: datetime | None = None
) -> TokenBearer | None:
    """Resolve a presented secret to a bearer, or `None`.

    `None` for every kind of failure — unknown, revoked, expired, or a token whose owner is no
    longer a member of the org. The caller turns that into one 401 with one sentence, because
    "revoked" and "never existed" are both just *no* from outside, and the difference is useful
    only to somebody guessing.
    """
    moment = now or datetime.now(UTC)
    if not presented or not presented.startswith(PREFIX):
        # Cheap rejection of anything that is obviously not ours -- in particular a Supabase JWT,
        # which must not even reach the lookup.
        return None

    row = await session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(presented)))
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= moment:
        return None

    # **The membership is re-resolved here, every call.** Removing somebody from a workspace has
    # to take their tokens with it, or a token is an ex-colleague holding a key. Scoped to the
    # token's org as well as its user, so a person who moved workspaces does not carry a token
    # for the old one into the new.
    member = await session.scalar(
        select(Member).where(Member.user_id == row.user_id, Member.org_id == row.org_id)
    )
    if member is None:
        return None

    if row.last_used_at is None or (moment - row.last_used_at) > timedelta(minutes=1):
        # At most once a minute: the question this answers is "is this still in use?", asked
        # before revoking something, and that needs the day rather than the second.
        row.last_used_at = moment

    return TokenBearer(
        token_id=row.id,
        user_id=row.user_id,
        org_id=member.org_id,
        role=member.role,
        scopes=tuple(row.scopes),
    )


async def list_tokens(session: AsyncSession, *, org_id: uuid.UUID) -> list[ApiToken]:
    """Live tokens for a workspace, newest first. Never the secret — it is not stored."""
    rows = await session.scalars(
        select(ApiToken)
        .where(ApiToken.org_id == org_id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
    )
    return list(rows)


async def revoke_token(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    token_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Revoke one token of this org. Returns whether there was one to revoke.

    Scoped by `org_id` in the `WHERE`, not checked afterwards: an id from another workspace
    resolves to nothing rather than to a 403 that confirms it exists.
    """
    row = await session.scalar(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.org_id == org_id)
    )
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = now or datetime.now(UTC)
    return True
