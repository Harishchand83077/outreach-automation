"""A candidate's facts and answer bank (F6).

Two rules shape everything here:

1. **A fact is unusable until confirmed.** Mined facts arrive as proposals, because the person
   who will be asked about a claim in an interview is the person who has to have agreed to it.
2. **Only an approved answer is ever offered**, and an unmatched question is reported as needing
   the user rather than guessed at. That is the whole difference from tools that invent screening
   answers.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AnswerBankEntry, ProfileFact
from app.db.repo import OrgContext
from app.domain.kinds import CandidateFact

FACT_KINDS = ("skill", "project", "role", "education")
FACT_SOURCES = ("user", "github", "resume")

_FACT_ID = re.compile(r"^pp(\d+)$")


async def next_fact_id(session: AsyncSession, ctx: OrgContext) -> str:
    """The next free `ppN` for this workspace.

    Computed from the rows rather than counted, so deleting `pp2` does not later produce a second
    `pp3` -- a duplicate id would make a citation ambiguous, which is the one thing an evidence
    reference cannot be.
    """
    rows = await session.scalars(
        select(ProfileFact.fact_id).where(ProfileFact.org_id == ctx.org_id)
    )
    highest = 0
    for value in rows:
        match = _FACT_ID.match(value or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return f"pp{highest + 1}"


async def add_fact(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    kind: str,
    statement: str,
    evidence_url: str | None = None,
    source: str = "user",
    now: datetime | None = None,
) -> ProfileFact:
    """Add a fact. A typed one is confirmed immediately; a mined one is a proposal."""
    if kind not in FACT_KINDS:
        raise ValueError(f"kind must be one of {', '.join(FACT_KINDS)}")
    if source not in FACT_SOURCES:
        raise ValueError(f"source must be one of {', '.join(FACT_SOURCES)}")
    text = statement.strip()
    if not text:
        raise ValueError("a fact needs a statement")

    row = ProfileFact(
        org_id=ctx.org_id,
        fact_id=await next_fact_id(session, ctx),
        kind=kind,
        statement=text,
        evidence_url=(evidence_url or "").strip() or None,
        source=source,
        # Typed by the person, so already agreed to. Mined, so not.
        confirmed_at=(now or datetime.now(tz=UTC)) if source == "user" else None,
    )
    session.add(row)
    await session.flush()
    return row


async def confirm_fact(
    session: AsyncSession, ctx: OrgContext, *, fact_row_id: uuid.UUID, now: datetime | None = None
) -> bool:
    """Agree to a proposed fact. Scoped in the WHERE, so another workspace's id is nothing."""
    row = await session.scalar(
        select(ProfileFact).where(ProfileFact.id == fact_row_id, ProfileFact.org_id == ctx.org_id)
    )
    if row is None:
        return False
    if row.confirmed_at is None:
        row.confirmed_at = now or datetime.now(tz=UTC)
    return True


async def list_facts(
    session: AsyncSession, ctx: OrgContext, *, confirmed_only: bool = False
) -> list[ProfileFact]:
    statement = select(ProfileFact).where(ProfileFact.org_id == ctx.org_id)
    if confirmed_only:
        statement = statement.where(ProfileFact.confirmed_at.is_not(None))
    rows = await session.scalars(statement.order_by(ProfileFact.created_at))
    return list(rows)


async def confirmed_facts(session: AsyncSession, ctx: OrgContext) -> list[CandidateFact]:
    """The workspace's confirmed facts, as the domain type a persona compiles from.

    Returning `CandidateFact` rather than rows is what lets a campaign with no facts of its own
    draw on the person's, through the `as_persona` path that already exists -- so the claim check
    resolves these with no new machinery.
    """
    rows = await list_facts(session, ctx, confirmed_only=True)
    return [CandidateFact(id=row.fact_id, text=row.statement) for row in rows]


async def delete_fact(session: AsyncSession, ctx: OrgContext, *, fact_row_id: uuid.UUID) -> bool:
    row = await session.scalar(
        select(ProfileFact).where(ProfileFact.id == fact_row_id, ProfileFact.org_id == ctx.org_id)
    )
    if row is None:
        return False
    await session.delete(row)
    return True


# --------------------------------------------------------------------------- answer bank


def _words(text: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9+]+", (text or "").lower()) if word}


async def add_answer(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    question_pattern: str,
    answer: str,
    fact_ids: list[str] | None = None,
    approve: bool = False,
    now: datetime | None = None,
) -> AnswerBankEntry:
    """Store an answer. **Unapproved by default**, so nothing is offered by accident."""
    pattern = question_pattern.strip().lower()
    text = answer.strip()
    if not pattern or not text:
        raise ValueError("an answer needs both a question pattern and an answer")

    existing = await session.scalar(
        select(AnswerBankEntry).where(
            AnswerBankEntry.org_id == ctx.org_id,
            AnswerBankEntry.question_pattern == pattern,
        )
    )
    moment = now or datetime.now(tz=UTC)
    if existing is not None:
        existing.answer = text
        existing.fact_ids = list(fact_ids or [])
        # An edited answer is unapproved again: the approval was of the previous wording.
        existing.approved_at = moment if approve else None
        await session.flush()
        return existing

    row = AnswerBankEntry(
        org_id=ctx.org_id,
        question_pattern=pattern,
        answer=text,
        fact_ids=list(fact_ids or []),
        approved_at=moment if approve else None,
    )
    session.add(row)
    await session.flush()
    return row


async def approve_answer(
    session: AsyncSession, ctx: OrgContext, *, answer_id: uuid.UUID, now: datetime | None = None
) -> bool:
    row = await session.scalar(
        select(AnswerBankEntry).where(
            AnswerBankEntry.id == answer_id, AnswerBankEntry.org_id == ctx.org_id
        )
    )
    if row is None:
        return False
    row.approved_at = now or datetime.now(tz=UTC)
    return True


async def list_answers(
    session: AsyncSession, ctx: OrgContext, *, approved_only: bool = False
) -> list[AnswerBankEntry]:
    statement = select(AnswerBankEntry).where(AnswerBankEntry.org_id == ctx.org_id)
    if approved_only:
        statement = statement.where(AnswerBankEntry.approved_at.is_not(None))
    rows = await session.scalars(statement.order_by(AnswerBankEntry.created_at))
    return list(rows)


async def match_question(
    session: AsyncSession, ctx: OrgContext, *, label: str
) -> AnswerBankEntry | None:
    """The approved answer for a form label, or `None`.

    Word overlap, not a regex and not a model. A pattern the person typed has to be one they can
    read back and predict, and the honest failure here is "we do not have an answer for this",
    which the caller reports rather than papers over.
    """
    wanted = _words(label)
    if not wanted:
        return None

    best: AnswerBankEntry | None = None
    best_score = 0
    for row in await list_answers(session, ctx, approved_only=True):
        pattern_words = _words(row.question_pattern)
        if not pattern_words:
            continue
        # Every word of the pattern must appear in the label, so "notice period" does not match
        # "period" alone. The longest matching pattern wins, since it is the most specific.
        if pattern_words <= wanted and len(pattern_words) > best_score:
            best, best_score = row, len(pattern_words)
    return best
