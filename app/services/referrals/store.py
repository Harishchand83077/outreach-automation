"""Storing a connections export, replacing the previous one (F10, ADR-042)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Connection
from app.db.repo import OrgContext
from app.services.referrals.parsing import ParsedExport, normalise_company


@dataclass(frozen=True)
class ImportResult:
    batch_id: uuid.UUID
    stored: int
    updated: int
    skipped: int
    truncated: int


async def store(session: AsyncSession, ctx: OrgContext, parsed: ParsedExport) -> ImportResult:
    """Write an export's connections, updating anybody already known.

    **Upsert on the profile URL rather than insert.** A connections export is a snapshot of a
    growing network, so somebody re-exporting next month is handing us mostly the same people
    with a few new ones -- and inserting would double every existing row, which in a referral
    shortlist shows the same person twice and makes the list look wrong rather than long.

    A connection with **no** profile URL cannot be deduplicated, so those are inserted. The
    export has included the URL for years; rows without one are rare and a duplicate there is
    better than dropping somebody.
    """
    batch_id = uuid.uuid4()
    existing = {
        row.profile_url: row
        for row in (
            await session.scalars(
                select(Connection).where(
                    Connection.org_id == ctx.org_id, Connection.profile_url.is_not(None)
                )
            )
        ).all()
    }

    stored = 0
    updated = 0
    for one in parsed.connections:
        current = existing.get(one.profile_url) if one.profile_url else None
        if current is not None:
            # Their title and company are the fields that move, and the newer export is the
            # better answer: a referral note quoting a job somebody left two years ago is worse
            # than one with no title at all.
            current.full_name = one.full_name
            current.company = one.company
            current.company_norm = normalise_company(one.company)
            current.position = one.position
            current.connected_on = one.connected_on or current.connected_on
            current.import_batch_id = batch_id
            updated += 1
            continue
        row = Connection(
            org_id=ctx.org_id,
            full_name=one.full_name,
            profile_url=one.profile_url,
            company=one.company,
            company_norm=normalise_company(one.company),
            position=one.position,
            connected_on=one.connected_on,
            import_batch_id=batch_id,
        )
        session.add(row)
        if one.profile_url:
            existing[one.profile_url] = row
        stored += 1

    await session.flush()
    return ImportResult(
        batch_id=batch_id,
        stored=stored,
        updated=updated,
        skipped=parsed.skipped,
        truncated=parsed.truncated,
    )


async def count(session: AsyncSession, ctx: OrgContext) -> int:
    total = await session.scalar(
        select(func.count()).select_from(Connection).where(Connection.org_id == ctx.org_id)
    )
    return int(total or 0)


async def forget_all(session: AsyncSession, ctx: OrgContext) -> int:
    """Delete this workspace's connections.

    **A real delete, not a flag.** These are thousands of people who never agreed to be in our
    database -- they agreed to connect with the user. So the person who uploaded them must be
    able to take them out completely, and there is nothing here worth keeping a tombstone for:
    unlike a suppression, forgetting a connection cannot cause anybody to be contacted.
    """
    result = await session.execute(delete(Connection).where(Connection.org_id == ctx.org_id))
    await session.flush()
    # `CursorResult.rowcount` is what a DELETE returns; the base `Result` protocol mypy sees
    # does not declare it, so the narrowing is explicit rather than ignored.
    return int(getattr(result, "rowcount", 0) or 0)
