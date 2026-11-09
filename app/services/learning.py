"""Counting what reviewers keep changing, and remembering only what somebody approved (F16).

Three steps, and each is only safe given the one before it:

1. **Describe** — `domain.edits.describe`, arithmetic over the two texts, no model. A model
   asked what a human changed writes a fluent sentence that is sometimes about a change that
   did not happen, and the output of this feature goes into a prompt, so a hallucinated
   observation would become a standing instruction nobody wrote.
2. **Compare** — count observations across drafts, per draft rather than per occurrence.
3. **Summarise for a person** — write the patterns as `proposed` lessons. **Nothing reaches a
   prompt until somebody approves one.**

**This is the only path by which the product's own behaviour changes on the strength of what it
observed**, so the gate is a person rather than a threshold. A loop that closed itself would be
the system teaching itself from its own output with nobody reading the lesson, and one bad
afternoon of edits would become a rule for everybody.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Draft, DraftLesson
from app.db.repo import OrgContext
from app.domain import edits

#: How many edited drafts to read. Far more than a workspace will have for a long time, and a
#: ceiling so this cannot become a query that grows without limit.
MAX_PAIRS = 500


async def pairs(
    session: AsyncSession, ctx: OrgContext
) -> list[tuple[str, list[edits.Observation]]]:
    """Every (model draft, human edit) pair, described.

    The pair is explicit in the schema: `derived_from_id` is set only when a reviewer approved
    with changes, and ADR-026 added it precisely because version numbers cannot express the
    difference between that and a regeneration after a rejection.
    """
    source = aliased(Draft)
    rows = (
        await session.execute(
            select(Draft, source)
            .join(source, Draft.derived_from_id == source.id)
            .where(Draft.org_id == ctx.org_id)
            .order_by(Draft.created_at.desc())
            .limit(MAX_PAIRS)
        )
    ).all()

    described: list[tuple[str, list[edits.Observation]]] = []
    for edited, original in rows:
        described.append(
            (
                str(edited.id),
                edits.describe(
                    before_subject=original.subject or "",
                    before_body=original.body or "",
                    after_subject=edited.subject or "",
                    after_body=edited.body or "",
                ),
            )
        )
    return described


async def propose(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    minimum: int = edits.MIN_OCCURRENCES,
    now: datetime | None = None,
) -> list[DraftLesson]:
    """Write the recurring patterns as proposals. Returns what is waiting for a decision.

    **A dismissed lesson is never re-proposed.** Somebody looked at it and said no, and a
    feature that asked again every week would train them to stop reading. The unique key on
    `(org_id, kind)` is what makes that hold, and the kind is stable across recomputations.
    """
    moment = now or datetime.now(UTC)
    found = edits.recurring(await pairs(session, ctx), minimum=minimum)

    existing = {
        row.kind: row
        for row in (
            await session.scalars(select(DraftLesson).where(DraftLesson.org_id == ctx.org_id))
        ).all()
    }

    for pattern in found:
        row = existing.get(pattern.kind)
        if row is None:
            session.add(
                DraftLesson(
                    org_id=ctx.org_id,
                    kind=pattern.kind,
                    detail=pattern.detail,
                    instruction=edits.as_instruction(pattern),
                    occurrences=pattern.occurrences,
                    evidence=list(pattern.draft_ids),
                    state="proposed",
                    created_at=moment,
                )
            )
            continue
        if row.state == "proposed":
            # Still waiting: keep the count current so somebody deciding sees today's number
            # rather than the one from whenever it was first noticed.
            row.occurrences = pattern.occurrences
            row.evidence = list(pattern.draft_ids)

    await session.flush()
    return await waiting(session, ctx)


async def waiting(session: AsyncSession, ctx: OrgContext) -> list[DraftLesson]:
    rows = await session.scalars(
        select(DraftLesson)
        .where(DraftLesson.org_id == ctx.org_id, DraftLesson.state == "proposed")
        .order_by(DraftLesson.occurrences.desc())
    )
    return list(rows)


async def decide(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    lesson_id: uuid.UUID,
    approve: bool,
    decided_by: str,
    now: datetime | None = None,
) -> DraftLesson | None:
    """Approve or dismiss. Only an approved lesson ever reaches a prompt."""
    row = await session.get(DraftLesson, lesson_id)
    if row is None or row.org_id != ctx.org_id:
        return None
    row.state = "approved" if approve else "dismissed"
    row.decided_by = decided_by
    row.decided_at = now or datetime.now(UTC)
    await session.flush()
    return row


async def instructions(session: AsyncSession, ctx: OrgContext) -> list[str]:
    """The approved lessons, as lines for a drafting prompt.

    **Only `approved`.** A proposal is an observation nobody has agreed with yet, and the
    difference between the two states is the entire safety property of this feature.

    What comes back is advice about *how to write*, never about what is true: the drafter may
    still assert only what the evidence supports, and every draft produced under these lines
    goes through the same `validate_draft` and the same claim check as one produced without
    them. Hard rule 6 is untouched by anything here.
    """
    rows = await session.scalars(
        select(DraftLesson)
        .where(DraftLesson.org_id == ctx.org_id, DraftLesson.state == "approved")
        .order_by(DraftLesson.occurrences.desc())
    )
    return [row.instruction for row in rows]
