"""Storing, reading and revoking a workspace's own provider keys (F9, ADR-041).

The one rule everything here follows: **a key is written and never read back out to anybody.**
It goes in sealed, it comes out only inside `lookup.run`, and no function here returns plaintext
to a route. `invalid_reason` is read to decide whether a lookup can run and is never returned,
for the same reason as `mail_credentials.invalid_reason`: it can quote a provider's reply, and
that reaches a browser and a screenshot.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import CONTACT_PROVIDERS, ProviderCredential
from app.db.repo import OrgContext
from app.services import credentials


class ProviderKeyError(Exception):
    """A provider name or a credential shape this system will not store."""


@dataclass(frozen=True)
class KeyStatus:
    """What a route may say about a stored key. No secret, no provider message."""

    provider: str
    kind: str
    label: str | None
    connected: bool
    usable: bool
    last_used_at: datetime | None
    added_by: str | None


def _check_provider(provider: str) -> str:
    name = provider.strip().lower()
    if name not in CONTACT_PROVIDERS:
        raise ProviderKeyError(
            "provider must be one of " + ", ".join(CONTACT_PROVIDERS) + f", not {name!r}"
        )
    return name


async def live(
    session: AsyncSession, ctx: OrgContext, *, provider: str
) -> ProviderCredential | None:
    """This workspace's live credential for one provider, or None."""
    name = _check_provider(provider)
    return await session.scalar(
        select(ProviderCredential).where(
            ProviderCredential.org_id == ctx.org_id,
            ProviderCredential.provider == name,
            ProviderCredential.revoked_at.is_(None),
        )
    )


async def statuses(session: AsyncSession, ctx: OrgContext) -> list[KeyStatus]:
    """Every provider, with whether this workspace has a usable key for it.

    Every provider rather than every stored row, because the question the screen asks is "what
    can I look up?" and a provider with no key is an answer to it. `mock` is included: it is how
    a deployment with no paid account sees the feature work at all.
    """
    rows = {
        row.provider: row
        for row in (
            await session.scalars(
                select(ProviderCredential).where(
                    ProviderCredential.org_id == ctx.org_id,
                    ProviderCredential.revoked_at.is_(None),
                )
            )
        ).all()
    }
    out: list[KeyStatus] = []
    for provider in CONTACT_PROVIDERS:
        row = rows.get(provider)
        out.append(
            KeyStatus(
                provider=provider,
                kind=row.kind if row else ("api_key" if provider != "snov" else "oauth"),
                label=row.label if row else None,
                connected=row is not None,
                usable=row is not None and row.invalid_reason is None,
                last_used_at=row.last_used_at if row else None,
                added_by=row.added_by if row else None,
            )
        )
    return out


async def store(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    provider: str,
    secret: str,
    label: str | None = None,
    client_id: str | None = None,
    settings: Settings | None = None,
) -> ProviderCredential:
    """Save a key, replacing this workspace's previous one for that provider.

    **Replacing rather than adding** is hard rule 8 expressed in the write path: one account per
    provider per org, never several keys rotated to dodge a free tier. The old row is revoked
    rather than deleted, so "when did this key change and who changed it?" is still answerable.

    For `snov` the credential is a client id *and* secret, sealed as one JSON object -- two
    columns would be two things to encrypt, two to rotate, and two to forget to clear.
    """
    name = _check_provider(provider)
    cleaned = secret.strip()
    if not cleaned:
        raise ProviderKeyError("the key is empty")

    kind = "api_key"
    payload = cleaned
    if name == "snov":
        if not (client_id or "").strip():
            raise ProviderKeyError("snov needs a client id as well as a client secret")
        kind = "oauth"
        payload = json.dumps({"client_id": (client_id or "").strip(), "client_secret": cleaned})

    previous = await live(session, ctx, provider=name)
    if previous is not None:
        previous.revoked_at = datetime.now(tz=UTC)
        # Flushed before the insert, or the partial unique index refuses the new row: it is
        # unique on `(org_id, provider) WHERE revoked_at IS NULL`, and both rows satisfy that
        # until the revocation lands.
        await session.flush()

    row = ProviderCredential(
        org_id=ctx.org_id,
        provider=name,
        kind=kind,
        secret=credentials.seal(payload, settings).token,
        label=(label or "").strip()[:120] or None,
        added_by=ctx.actor_id,
    )
    session.add(row)
    await session.flush()
    return row


async def revoke(session: AsyncSession, ctx: OrgContext, *, provider: str) -> bool:
    """Stop using a key. Returns whether there was one."""
    row = await live(session, ctx, provider=provider)
    if row is None:
        return False
    row.revoked_at = datetime.now(tz=UTC)
    await session.flush()
    return True


async def mark_invalid(session: AsyncSession, *, credential_id: uuid.UUID, reason: str) -> None:
    """Record that the provider refused this key, so the next lookup refuses before calling.

    Truncated, because the text is a provider's own sentence and this column is dumped nightly.
    """
    row = await session.get(ProviderCredential, credential_id)
    if row is not None:
        row.invalid_reason = reason[:500]
        await session.flush()


async def clear_invalid(session: AsyncSession, *, credential_id: uuid.UUID) -> None:
    """A successful call clears a previous refusal: a key out of credit works again next month."""
    row = await session.get(ProviderCredential, credential_id)
    if row is not None and row.invalid_reason is not None:
        row.invalid_reason = None
        await session.flush()
