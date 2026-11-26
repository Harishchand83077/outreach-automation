"""Searching everything one workspace knows, by keyword (ADR-086).

**Retrieval without a model.** The corpus is one small workspace's leads, notes, replies, research,
job postings and saved people: thousands of rows at the outside. Postgres full-text search is
exact, explainable, free, needs no new extension (the production image is plain `postgres:17`)
and sends nobody's data to an embedding provider. The generator that benefits from retrieval is
the user's own connected Claude, which calls this over MCP and writes the answer; nothing here
generates text, so there is nothing to verify and nothing for a model to invent.

**Queried at read time, no index yet.** A scan of a few thousand rows is milliseconds; a GIN index
is a migration to add when a measurement says so, not before.

**Every branch filters on `org_id`.** The one mistake that would be invisible from inside a single
workspace, so it is asserted by a test with two.

Results from text a third party wrote (replies, postings, researched pages) are flagged
`untrusted`; the MCP tool wraps those before they leave (hard rule 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repo import OrgContext

MIN_QUERY_CHARS = 2
MAX_QUERY_CHARS = 200
#: Per source, so one chatty table (replies) cannot crowd the others out of the list.
PER_SOURCE = 5
DEFAULT_LIMIT = 20
#: A tsvector holds at most 1 MB; a long reply or posting is cut well under that.
MAX_DOC_CHARS = 100_000
_HEADLINE = "StartSel=[[,StopSel=]],MaxFragments=1,MaxWords=22,MinWords=8"

# (kind, untrusted, FROM + joins, id, lead id, title, document). Every fragment is a constant in
# this file; the only caller-supplied values are bound parameters.
_SOURCES: tuple[tuple[str, bool, str, str, str, str, str], ...] = (
    (
        "lead",
        False,
        "app.leads t",
        "t.id::text",
        "t.id::text",
        "coalesce(t.full_name, t.email_norm)",
        "concat_ws(' ', t.full_name, t.company_name, t.job_title, t.domain, t.notes)",
    ),
    (
        "note",
        False,
        "app.lead_notes t",
        "t.id::text",
        "t.lead_id::text",
        "'Note'",
        "t.body",
    ),
    (
        "reply",
        True,
        "app.messages t",
        "t.id::text",
        "t.lead_id::text",
        "coalesce(t.subject, '(no subject)')",
        "concat_ws(' ', t.subject, t.body_text)",
    ),
    (
        "research",
        True,
        "app.research t JOIN app.leads l ON l.id = t.lead_id",
        "t.id::text",
        "t.lead_id::text",
        "coalesce(l.full_name, l.company_name, 'Research')",
        "concat_ws(' ', t.hooks::text, t.person_notes::text)",
    ),
    (
        "job_post",
        True,
        "app.job_posts t",
        "t.id::text",
        "NULL",
        "t.title",
        "concat_ws(' ', t.title, t.company_domain, t.body_text)",
    ),
    (
        "person",
        False,
        "app.saved_people t",
        "t.id::text",
        "NULL",
        "t.full_name",
        "concat_ws(' ', t.full_name, t.headline, t.company_name)",
    ),
)

#: Extra filter per kind: only inbound mail is "a reply".
_EXTRA = {"reply": " AND t.direction = 'in'"}


@dataclass(frozen=True)
class Hit:
    kind: str
    id: str
    lead_id: str | None
    title: str
    snippet: str
    untrusted: bool
    rank: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "lead_id": self.lead_id,
            "title": self.title,
            "snippet": self.snippet,
            "untrusted": self.untrusted,
        }


def clean_query(raw: str) -> str:
    return " ".join(raw.split())[:MAX_QUERY_CHARS]


def _branch(source: tuple[str, bool, str, str, str, str, str]) -> str:
    kind, _untrusted, source_from, ident, lead_id, title, raw_doc = source
    doc = f"left({raw_doc}, {MAX_DOC_CHARS})"
    return f"""
        (SELECT '{kind}' AS kind, {ident} AS id, {lead_id} AS lead_id, {title} AS title,
                ts_headline('simple', {doc}, q.query,
                            '{_HEADLINE}') AS snippet,
                ts_rank(to_tsvector('simple', {doc}), q.query) AS rank
         FROM {source_from}, (SELECT websearch_to_tsquery('simple', :q) AS query) q
         WHERE t.org_id = :org AND to_tsvector('simple', {doc}) @@ q.query{_EXTRA.get(kind, "")}
         ORDER BY rank DESC LIMIT :per)
    """  # noqa: S608 -- constants only; the caller's text is the bound :q


async def search(
    session: AsyncSession, ctx: OrgContext, query: str, *, limit: int = DEFAULT_LIMIT
) -> list[Hit]:
    """Ranked hits across every source, or `[]` for a query too short to mean anything."""
    needle = clean_query(query)
    if len(needle) < MIN_QUERY_CHARS:
        return []
    sql = " UNION ALL ".join(_branch(source) for source in _SOURCES)
    rows = await session.execute(
        text(f"SELECT * FROM ({sql}) hits ORDER BY rank DESC LIMIT :limit"),  # noqa: S608
        {"q": needle, "org": ctx.org_id, "per": PER_SOURCE, "limit": min(max(limit, 1), 50)},
    )
    untrusted = {source[0]: source[1] for source in _SOURCES}
    return [
        Hit(
            kind=row.kind,
            id=row.id,
            lead_id=row.lead_id,
            title=str(row.title)[:200],
            snippet=str(row.snippet),
            untrusted=untrusted[row.kind],
            rank=float(row.rank),
        )
        for row in rows
    ]
