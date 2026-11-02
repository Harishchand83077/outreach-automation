"""Holding one workspace's booking-webhook secret (F15, ADR-045).

**The same scheme as every other secret here**: Fernet, the key in the environment, ciphertext in
its own table. Reusing it rather than inventing a second means there is one answer to "where are
the secrets and how are they protected", and one key to rotate.

**The secret is shown exactly once**, when it is generated, because it has to be pasted into a
booking tool's settings page and there is nowhere else for it to come from. After that only the
ciphertext exists, so losing it means generating a new one -- which is the same trade `api_tokens`
makes, and the same reason: a secret a route will read back to you is a secret a stolen session
can read back too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import BookingWebhook
from app.db.repo import OrgContext
from app.services import credentials
from app.services.booking import signature


@dataclass(frozen=True)
class NewSecret:
    """The one and only time the plaintext exists outside the booking tool's settings."""

    secret: str
    created_at: datetime


async def live(session: AsyncSession, ctx: OrgContext) -> BookingWebhook | None:
    return await session.scalar(
        select(BookingWebhook).where(
            BookingWebhook.org_id == ctx.org_id, BookingWebhook.revoked_at.is_(None)
        )
    )


async def plaintext(session: AsyncSession, ctx: OrgContext, *, settings: Settings) -> str | None:
    """This workspace's live secret, or `None`. Read only to verify a signature."""
    row = await live(session, ctx)
    if row is None:
        return None
    return credentials.unseal(row.secret, settings)


async def generate(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    settings: Settings,
    created_by: str | None = None,
    now: datetime | None = None,
) -> NewSecret:
    """Replace this workspace's secret with a fresh one.

    The previous row is **revoked rather than deleted**, so "when did this change, and who
    changed it?" stays answerable -- and so a delivery signed with the old secret fails closed
    instead of matching a row that was supposed to be gone.
    """
    moment = now or datetime.now(tz=UTC)
    existing = await live(session, ctx)
    if existing is not None:
        existing.revoked_at = moment
        # Flushed before the insert, or the partial unique index on `revoked_at IS NULL` refuses
        # the new row -- the same ordering the provider-key store needed for the same index.
        await session.flush()

    raw = signature.new_secret()
    session.add(
        BookingWebhook(
            org_id=ctx.org_id,
            secret=credentials.seal(raw, settings).token,
            created_by=created_by,
            created_at=moment,
        )
    )
    await session.flush()
    return NewSecret(secret=raw, created_at=moment)


async def revoke(session: AsyncSession, ctx: OrgContext, *, now: datetime | None = None) -> bool:
    """Turn the webhook off. Bookings already recorded are untouched."""
    row = await live(session, ctx)
    if row is None:
        return False
    row.revoked_at = now or datetime.now(tz=UTC)
    await session.flush()
    return True


async def touch(session: AsyncSession, ctx: OrgContext, *, now: datetime | None = None) -> None:
    """Record that a delivery arrived, so a webhook that has never fired is visible as such.

    The commonest failure here is silence: somebody pastes the URL into the wrong field, or into
    a tool that was never enabled, and nothing ever says so. A last-used timestamp is the
    cheapest way for the screen to tell "no meetings yet" from "this was never wired up".
    """
    row = await live(session, ctx)
    if row is not None:
        row.last_used_at = now or datetime.now(tz=UTC)
