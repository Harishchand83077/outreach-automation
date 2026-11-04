"""Writing and reading the graph (F12, ADR-051).

Every function that writes an edge takes its provenance as a **required argument**, so the
database constraint is not the first thing to notice a missing source -- it is the last. A
caller that forgets cannot compile a call, which is cheaper than a caller that forgets and gets
an `IntegrityError` out of a worker pass that was storing something else at the time.

**A model never reaches this module.** The producers are a job posting, whose apply URL is the
source, and an inbound message, whose id is. S15.3's brief reads these rows and may reword them;
it cannot introduce a claim, because there would be nothing to put in the provenance columns.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Edge, Entity, JobPost, SignalEvent
from app.db.repo import OrgContext
from app.services.research.net import company_domain

_SPACES = re.compile(r"\s+")
#: Suffixes that are how a company writes itself down rather than part of its name. The same
#: problem the referral matcher solves, and deliberately the same shape of answer -- except that
#: here a name reduced to nothing is simply kept, because an entity with an empty key would
#: collide with every other one.
_SUFFIXES = (
    "inc",
    "inc.",
    "llc",
    "ltd",
    "ltd.",
    "limited",
    "pvt",
    "private",
    "plc",
    "gmbh",
    "corp",
    "corporation",
    "co",
    "company",
)


class ProvenanceRequired(ValueError):
    """An edge was offered with nothing to say where it came from."""


@dataclass(frozen=True)
class Claim:
    """One thing to record, with the thing that says so.

    `source_url` and `message_id` are both optional *here* and at least one is required by
    `__post_init__`, so the refusal names the claim rather than arriving as a constraint
    violation three frames away.
    """

    subject: Entity
    predicate: str
    object_entity: Entity | None = None
    object_text: str | None = None
    source_url: str | None = None
    message_id: uuid.UUID | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source_url and self.message_id is None:
            raise ProvenanceRequired(
                f"a {self.predicate} claim needs a source url or a message id; "
                "an edge nobody can trace is a sentence we cannot defend"
            )
        if (self.object_entity is None) == (self.object_text is None):
            raise ProvenanceRequired(
                "an edge points at an entity or states a value, never both and never neither"
            )


def key_for(kind: str, name: str, *, domain: str | None = None) -> str:
    """The identity of an entity, so two spellings of one thing are one node.

    A company is keyed on its **domain** when it has one, because that is what
    `domain_research`, the watchlist and the research cache all use (ADR-011) -- four tables
    agreeing on what a company is matters more than any of them being clever about names.
    Everything else is keyed on a flattened name.
    """
    if kind == "company" and domain:
        return company_domain(domain) or domain.strip().lower()

    flat = _SPACES.sub(" ", (name or "").strip().lower())
    if kind == "company":
        parts = flat.replace(",", " ").split()
        while len(parts) > 1 and parts[-1].strip(".") in _SUFFIXES:
            parts.pop()
        flat = " ".join(parts)
    return flat[:300]


async def entity(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    kind: str,
    name: str,
    domain: str | None = None,
    lead_id: uuid.UUID | None = None,
) -> Entity:
    """Find or make a node. Idempotent on `(kind, norm_key)`."""
    key = key_for(kind, name, domain=domain)
    row = await session.scalar(
        select(Entity).where(
            Entity.org_id == ctx.org_id, Entity.kind == kind, Entity.norm_key == key
        )
    )
    if row is not None:
        # A name seen with better capitalisation, or a lead we have since matched, improves the
        # node. The key never moves: that would split it in two.
        if lead_id is not None and row.lead_id is None:
            row.lead_id = lead_id
        if domain and not row.domain:
            row.domain = company_domain(domain) or domain
        return row

    row = Entity(
        org_id=ctx.org_id,
        kind=kind,
        name=(name or key)[:300],
        norm_key=key,
        domain=(company_domain(domain) or domain) if domain else None,
        lead_id=lead_id,
    )
    session.add(row)
    await session.flush()
    return row


async def record(session: AsyncSession, ctx: OrgContext, claim: Claim) -> tuple[Edge, bool]:
    """Store one claim. Returns `(edge, created)`; recording it twice is harmless.

    Idempotent because the producers are re-run: the daily poll sees yesterday's postings again
    every morning, and without this the graph would grow a duplicate claim a day until a brief
    cited the same sentence six times.
    """
    existing = await session.scalar(
        select(Edge).where(
            Edge.org_id == ctx.org_id,
            Edge.subject_id == claim.subject.id,
            Edge.predicate == claim.predicate,
            Edge.object_id == (claim.object_entity.id if claim.object_entity else None),
            Edge.object_text == claim.object_text,
            Edge.source_url == claim.source_url,
            Edge.message_id == claim.message_id,
        )
    )
    if existing is not None:
        return existing, False

    edge = Edge(
        org_id=ctx.org_id,
        subject_id=claim.subject.id,
        predicate=claim.predicate,
        object_id=claim.object_entity.id if claim.object_entity else None,
        object_text=claim.object_text,
        source_url=claim.source_url,
        message_id=claim.message_id,
        observed_at=claim.observed_at,
    )
    session.add(edge)
    await session.flush()
    return edge, True


async def from_job_post(
    session: AsyncSession, ctx: OrgContext, *, post: JobPost, domain: str, company_name: str
) -> list[Edge]:
    """A posting says a company is hiring for a role. Its apply URL is the source.

    **This is the producer, and it ships with the table** -- a table with no writer is how search
    came to be wired to nothing for three phases. It is also the cleanest provenance this system
    has: the sentence "they are hiring a Backend Engineer" and the page that says so, with
    nothing in between that could have invented it.

    The company's domain is passed in rather than read from the posting, because a feed's
    `company_domain` is NULL by design (ADR-047) and the caller is the one that resolved it.
    """
    company = await entity(session, ctx, kind="company", name=company_name, domain=domain)
    role = await entity(session, ctx, kind="role", name=post.title)
    edge, _ = await record(
        session,
        ctx,
        Claim(
            subject=company,
            predicate="hiring_for",
            object_entity=role,
            source_url=post.apply_url,
            observed_at=post.posted_at,
        ),
    )
    return [edge]


async def from_signal(
    session: AsyncSession, ctx: OrgContext, *, signal: SignalEvent, company_name: str | None = None
) -> list[Edge]:
    """The same claim, from a signal that already carries its own URL.

    A signal with no URL produces nothing rather than an edge with no source: the constraint
    would refuse it, and refusing here says which signal rather than which transaction.
    """
    if not signal.url or signal.kind != "hiring":
        return []

    company = await entity(
        session,
        ctx,
        kind="company",
        name=company_name or signal.domain,
        domain=signal.domain,
    )
    role = await entity(session, ctx, kind="role", name=signal.title)
    edge, _ = await record(
        session,
        ctx,
        Claim(
            subject=company,
            predicate="hiring_for",
            object_entity=role,
            source_url=signal.url,
            observed_at=signal.observed_at,
        ),
    )
    return [edge]


@dataclass(frozen=True)
class Statement:
    """One edge, read back as something a person could say, with its source beside it."""

    subject: str
    predicate: str
    object: str
    source_url: str | None
    message_id: uuid.UUID | None
    observed_at: datetime | None

    @property
    def sentence(self) -> str:
        phrasing = {
            "hiring_for": "is hiring for",
            "works_at": "works at",
            "uses": "uses",
            "mentioned": "mentioned",
        }
        return f"{self.subject} {phrasing.get(self.predicate, self.predicate)} {self.object}"


async def about(
    session: AsyncSession, ctx: OrgContext, *, domain: str, limit: int = 50
) -> list[Statement]:
    """Everything known about one company, newest first, each with its source.

    Read by the dashboard and, in S15.3, by the brief. Returning the source **with** every line
    rather than beside the set is what makes "a model may phrase, never add" checkable: a
    sentence that arrives without one did not come from here.
    """
    key = key_for("company", domain, domain=domain)
    company = await session.scalar(
        select(Entity).where(
            Entity.org_id == ctx.org_id, Entity.kind == "company", Entity.norm_key == key
        )
    )
    if company is None:
        return []

    edges = (
        await session.scalars(
            select(Edge)
            .where(
                Edge.org_id == ctx.org_id,
                or_(Edge.subject_id == company.id, Edge.object_id == company.id),
            )
            .order_by(Edge.observed_at.desc().nullslast(), Edge.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
    ).all()

    names: dict[uuid.UUID, str] = {company.id: company.name}
    wanted = {edge.object_id for edge in edges if edge.object_id} | {
        edge.subject_id for edge in edges
    }
    for row in (
        await session.scalars(
            select(Entity).where(Entity.org_id == ctx.org_id, Entity.id.in_(wanted))
        )
    ).all():
        names[row.id] = row.name

    return [
        Statement(
            subject=names.get(edge.subject_id, "?"),
            predicate=edge.predicate,
            object=(names.get(edge.object_id, "?") if edge.object_id else (edge.object_text or "")),
            source_url=edge.source_url,
            message_id=edge.message_id,
            observed_at=edge.observed_at,
        )
        for edge in edges
    ]
