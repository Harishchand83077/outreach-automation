"""Computing and storing a posting's fit score (F6, ADR-055).

**`fit.score_fit` had no caller.** Phase 12 built the extractor, the scorer and the dataclasses,
with tests for each, and nothing ever ran them against a posting: a grep for the function
returned its definition and one docstring mentioning it. The thirteenth instance of this
project's signature shape, and the cheapest kind to find.

**The split here is the one the brief uses.** Computing a score calls a model, so it happens on
an explicit press and nowhere else -- never on a page load, never on a tab change, never from a
`GET`. Reading one is free, so the screen and the MCP tool both read the stored row and cannot
disagree about what the score is. A read that spends is the pattern this project refuses
everywhere: `GET /mailboxes` does not mint an OAuth token, and a contact lookup does not spend a
credit because somebody pressed F5.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FitScoreRow, JobPost
from app.db.repo import OrgContext
from app.services.candidate.fit import score_fit
from app.services.candidate.profile import confirmed_facts
from app.services.candidate.requirements import extract_requirements
from app.services.drafting.generator import Router


class NothingToScore(ValueError):
    """The posting has no text, or the candidate has confirmed no facts."""


async def compute(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    router: Router,
    post: JobPost,
    now: datetime | None = None,
) -> FitScoreRow:
    """Extract this posting's requirements, score them, and store the result.

    **Only confirmed facts are offered to the scorer**, so the rule holds one step before the
    comparison: a fact the candidate has not agreed to say cannot produce a match they never
    claimed (ADR-037). Refused with a sentence when there are none, because a score of zero
    against an empty profile reads as a bad fit rather than as an empty profile.
    """
    facts = await confirmed_facts(session, ctx)
    if not facts:
        raise NothingToScore(
            "You have no confirmed facts yet, so there is nothing to score this against. A "
            "score of zero against an empty profile would read as a bad fit."
        )
    if not (post.body_text or "").strip():
        raise NothingToScore(
            "This posting has no description stored, so there are no requirements to read. "
            "Greenhouse's list response carries no body; open the posting and paste it."
        )

    requirements = await extract_requirements(
        router=router, posting_text=post.body_text or "", job_post_id=str(post.id)
    )
    if not requirements:
        raise NothingToScore(
            "No requirements could be read from this posting. That is a correct answer for a "
            "posting that only describes the company."
        )

    result = score_fit(requirements, facts)
    row = await latest(session, ctx, job_post_id=post.id)
    moment = now or datetime.now(tz=UTC)

    matched = [
        {"requirement": match.requirement, "fact_id": match.fact_id, "shared": list(match.shared)}
        for match in result.matched
    ]
    if row is None:
        row = FitScoreRow(org_id=ctx.org_id, job_post_id=post.id)
        session.add(row)

    # Replaced rather than appended: a second score for one posting is the same question asked
    # again, and two rows would mean the screen and the tool could show different answers
    # depending on which they read first.
    row.score = result.score
    row.matched = matched
    row.missing = list(result.missing)
    row.requirements = requirements
    row.computed_at = moment
    await session.flush()
    return row


async def latest(
    session: AsyncSession, ctx: OrgContext, *, job_post_id: uuid.UUID
) -> FitScoreRow | None:
    """The stored score for one posting, or None. Free, and calls no model."""
    return await session.scalar(
        select(FitScoreRow).where(
            FitScoreRow.org_id == ctx.org_id, FitScoreRow.job_post_id == job_post_id
        )
    )


def as_dict(row: FitScoreRow, *, post: JobPost | None = None) -> dict[str, object]:
    """The shape the screen and the MCP tool both read.

    **`score` never travels without `matched` and `missing`.** A number with nothing under it
    invites whoever reads it to repeat it as a judgement, and this score is rules over an
    extraction: every match names the fact it rests on and the words that made it one.
    """
    return {
        "job_post_id": str(row.job_post_id),
        "title": post.title if post is not None else None,
        "score": row.score,
        "matched": row.matched,
        "missing": row.missing,
        "requirements": row.requirements,
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }
