"""Build a brief out of rows that carry their own provenance (F12, ADR-052).

**Every line cites.** `Line.__post_init__` refuses a line with neither a `source_url` nor a
`message_id`, so a section cannot be padded with something nobody can point at. That is the same
device as `ck_edges_provenance` one layer up (ADR-051): the brief is built from the graph, from
research hooks and from messages, which are the three things in this system that carry a source
*per item*.

**What is deliberately left out is the interesting half.** `domain_research.findings` holds a
synthesised `what_they_do`, `stage_signals` and `tech_signals` -- genuinely useful prose with no
per-item source, because it is a summary of several pages. A line citing "one of these four
URLs" is a line whose source cannot be checked, which is worse than no line: somebody would
repeat it. So the synthesis is reported in `gaps` as something we hold and cannot cite, with the
reader sent to the research panel for it.

**The header is not a claim.** Who the lead is comes from the operator's own spreadsheet, so it
is named above the sections rather than asserted inside one. A brief is addressed *about*
somebody; naming them is not a statement about the world that needs a footnote.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DomainResearch, Lead, Message, Research
from app.db.repo import OrgContext
from app.services.graph import store as graph
from app.services.research.net import company_domain

#: The five sections, in the order somebody about to make a call wants them. A fixed tuple
#: because a section a model could invent is a section with nothing to put in it -- the same
#: device as `ProposedEdits` having no field for a heading (ADR-043).
SECTIONS: tuple[str, ...] = (
    "who_they_are",
    "what_they_are_doing",
    "what_they_use",
    "what_we_found",
    "the_conversation",
)

SECTION_TITLES: dict[str, str] = {
    "who_they_are": "Who they are",
    "what_they_are_doing": "What they are doing",
    "what_they_use": "What they use",
    "what_we_found": "What we found",
    "the_conversation": "The conversation so far",
}

#: Which graph predicate belongs in which section. A predicate with no home here is still
#: stored and still shown by the Signals screen; it simply has no sentence in a brief yet.
_PREDICATE_SECTION: dict[str, str] = {
    "works_at": "who_they_are",
    "hiring_for": "what_they_are_doing",
    "uses": "what_they_use",
    "mentioned": "what_we_found",
}

#: Research hook kinds, mapped the same way. An unknown kind lands in "what we found" rather
#: than being dropped: a hook that passed the snippet check is citable whatever it is about.
_HOOK_SECTION: dict[str, str] = {
    "hiring": "what_they_are_doing",
    "launch": "what_they_are_doing",
    "news": "what_they_are_doing",
    "product": "what_we_found",
    "customer": "what_we_found",
    "other": "what_we_found",
}

MAX_LINES_PER_SECTION = 8


class NoProvenance(ValueError):
    """A brief line was offered with nothing to say where it came from."""


@dataclass(frozen=True)
class Line:
    """One sentence a person could say out loud, and the thing that says so.

    `line_id` is a stable handle within one brief, which is what makes the phrasing step
    checkable: a model returns `{line_id: better wording}` and cannot return a line that was
    not already here, because there would be no id to attach it to.
    """

    line_id: str
    section: str
    text: str
    source_url: str | None = None
    message_id: uuid.UUID | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.section not in SECTIONS:
            raise NoProvenance(f"{self.section!r} is not one of the five sections")
        if not self.source_url and self.message_id is None:
            raise NoProvenance(
                f"a brief line needs a source url or a message id; {self.text!r} has neither, "
                "and a line nobody can open is a line nobody should repeat"
            )


@dataclass
class Brief:
    """The whole thing: a header that is not a claim, cited lines, and honest absences."""

    lead_id: uuid.UUID
    header: dict[str, Any]
    lines: list[Line] = field(default_factory=list)
    #: What we hold and cannot cite, and what we simply do not have. Never a claim, so these
    #: carry no source by construction -- an absence has nothing to point at.
    gaps: list[str] = field(default_factory=list)

    def section(self, name: str) -> list[Line]:
        return [line for line in self.lines if line.section == name]

    @property
    def cited(self) -> int:
        return len(self.lines)


async def build(session: AsyncSession, ctx: OrgContext, *, lead: Lead) -> Brief:
    """Everything citable about one lead's company and the conversation with them.

    Reads the graph, the lead's research hooks and the message history. No network call and no
    model: this function is the thing a model is later allowed to *reword*, which means it has
    to be complete and correct before any model sees it.
    """
    brief = Brief(
        lead_id=lead.id,
        header={
            "name": lead.full_name,
            "company": lead.company_name,
            "company_url": lead.company_url,
            "state": lead.state,
            # Said plainly, because the whole point of the sections below is that they are not
            # this: a header is what you told us, not what we established.
            "note": "From your own import. Every line below carries the source that says so.",
        },
    )

    domain = company_domain(lead.company_url or "") or company_domain(lead.domain or "")
    seen: set[str] = set()
    counts: dict[str, int] = {}

    def keep(section: str, text: str) -> bool:
        """One line per thing said, and a section that cannot be scrolled."""
        flat = text.strip().lower()
        if not flat or flat in seen:
            return False
        if counts.get(section, 0) >= MAX_LINES_PER_SECTION:
            return False
        seen.add(flat)
        counts[section] = counts.get(section, 0) + 1
        return True

    # ---- the graph: claims that already passed `ck_edges_provenance` ----------------------
    if domain:
        for index, statement in enumerate(await graph.about(session, ctx, domain=domain)):
            section = _PREDICATE_SECTION.get(statement.predicate)
            if section is None or not keep(section, statement.sentence):
                continue
            brief.lines.append(
                Line(
                    line_id=f"g{index}",
                    section=section,
                    text=statement.sentence,
                    source_url=statement.source_url,
                    message_id=statement.message_id,
                    observed_at=statement.observed_at,
                )
            )

    # ---- research hooks: already verified against the text they were read from -------------
    research = await session.scalar(
        select(Research)
        .where(Research.org_id == ctx.org_id, Research.lead_id == lead.id)
        .order_by(Research.created_at.desc())
        .limit(1)
    )
    for index, hook in enumerate(list((research.hooks or []) if research else [])):
        text = str(hook.get("text") or "").strip()
        url = str(hook.get("source_url") or "").strip()
        if not text:
            continue
        if not url:
            # A hook with no URL cannot have passed the snippet check against a fetched page, so
            # this is a row from somewhere unexpected. Reported rather than rendered.
            brief.gaps.append("One research hook has no source URL, so it is not shown.")
            continue
        section = _HOOK_SECTION.get(str(hook.get("kind") or "other"), "what_we_found")
        if not keep(section, text):
            continue
        brief.lines.append(Line(line_id=f"h{index}", section=section, text=text, source_url=url))

    # ---- the conversation: each message is its own provenance ------------------------------
    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.org_id == ctx.org_id, Message.lead_id == lead.id)
                .order_by(Message.created_at.desc())
                .limit(MAX_LINES_PER_SECTION)
            )
        ).all()
    )
    for index, message in enumerate(reversed(messages)):
        who = "You wrote" if message.direction == "out" else "They wrote"
        subject = (message.subject or "(no subject)").strip()
        brief.lines.append(
            Line(
                line_id=f"m{index}",
                section="the_conversation",
                text=f"{who}: {subject}",
                message_id=message.id,
                observed_at=message.sent_at or message.created_at,
            )
        )

    brief.gaps.extend(await _absences(session, ctx, brief, research=research, domain=domain))
    return brief


async def _absences(
    session: AsyncSession,
    ctx: OrgContext,
    brief: Brief,
    *,
    research: Research | None,
    domain: str | None,
) -> list[str]:
    """What this brief does not know, said rather than filled in.

    The reason this exists at all: a brief with four thin sections invites whoever reads it to
    supply the rest from memory, and a model asked to "write a brief" would supply it from
    nothing. Naming the hole is the only honest version, and it is also the list that tells an
    operator which button to press next.
    """
    gaps: list[str] = []
    if not domain:
        gaps.append(
            "No company domain on this lead, so nothing from the graph or a careers board can "
            "be matched to them."
        )
    for name in SECTIONS:
        if not brief.section(name):
            gaps.append(f"Nothing citable yet for '{SECTION_TITLES[name]}'.")

    if research is not None and research.domain_research_id is not None:
        shared = await session.get(DomainResearch, research.domain_research_id)
        findings = (shared.findings if shared else None) or {}
        summary = str(findings.get("what_they_do") or "").strip()
        if summary:
            gaps.append(
                "Research holds a summary of this company drawn from several pages, so no one "
                "page says it and it is not a line here. It is on the lead's research panel."
            )
    if research is not None and research.thin:
        gaps.append(
            "The research on this company was thin, so treat the lines above as all we have."
        )
    return gaps


def render(brief: Brief) -> str:
    """The brief as plain text, each line followed by its own source.

    Read by the MCP tool and by nothing that sends: this is text for a person before a call. The
    source is printed *with* the line rather than in a block at the end, because a footnote list
    is exactly how a line gets separated from the thing that justified it.
    """
    head = brief.header
    out: list[str] = [
        f"{head.get('name') or 'This lead'} at {head.get('company') or 'an unnamed company'}",
        str(head["note"]),
    ]
    for name in SECTIONS:
        lines = brief.section(name)
        out.append("")
        out.append(SECTION_TITLES[name])
        if not lines:
            out.append("  (nothing citable yet)")
            continue
        for line in lines:
            where = line.source_url or (
                f"message {line.message_id}" if line.message_id else "no source"
            )
            out.append(f"  - {line.text}  [{where}]")
    if brief.gaps:
        out.append("")
        out.append("What we do not know")
        out.extend(f"  - {gap}" for gap in brief.gaps)
    return "\n".join(out)


def as_dict(brief: Brief) -> dict[str, Any]:
    """The shape the dashboard reads. One list per section, each line with its own source."""
    return {
        "lead_id": str(brief.lead_id),
        "header": brief.header,
        "sections": [
            {
                "name": name,
                "title": SECTION_TITLES[name],
                "lines": [
                    {
                        "line_id": line.line_id,
                        "text": line.text,
                        "source_url": line.source_url,
                        "from_a_message": line.message_id is not None,
                        "observed_at": line.observed_at.isoformat() if line.observed_at else None,
                    }
                    for line in brief.section(name)
                ],
            }
            for name in SECTIONS
        ],
        "gaps": brief.gaps,
        "cited": brief.cited,
    }
