"""Who in the user's own network works at a company, and what to say to them (F10, ADR-042).

**The note is built by rules, not by a model, and that is a decision rather than a shortcut.** A
referral ask is a message the user sends under their own name to somebody who knows them, and the
cost of a fabricated line in it is not a weak email -- it is the user telling a friend something
untrue about themselves. Hard rule 6 already forbids inventing facts about the sender; here the
reader is a person who would notice.

So every substitution in the note is a value from the database: the connection's own name, their
own company and title as *they* wrote them in their profile, the posting's title and link, and
the user's confirmed `profile_facts` only where one is explicitly cited. Nothing is generated.
The note is short on purpose: it is a starting point the user edits in LinkedIn's own message
box, not a finished message.

**Nothing is sent.** There is no send path here, no SMTP, no `messages` row. The output is text
and a link the user opens themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Connection, JobPost, ProfileFact
from app.db.repo import OrgContext
from app.services.jobposts import company as jobcompany
from app.services.referrals.parsing import normalise_company

#: More than this and it is not a referral shortlist, it is a directory. The user picks a person
#: to ask; twenty candidates is already more than anybody works through.
MAX_MATCHES = 20


@dataclass(frozen=True)
class Referral:
    connection_id: uuid.UUID
    full_name: str
    position: str | None
    company: str | None
    profile_url: str | None
    connected_on: str | None
    #: Why this person matched, in words. "Company name matches exactly" reads differently from
    #: "their company contains the posting's company", and the user is the one deciding whether
    #: the match is real.
    why: str
    note: str


def _match_reason(connection_norm: str, target_norm: str) -> str | None:
    """How these two company names relate, or None if they do not.

    Exact first, then containment in either direction -- "acme" against "acme india", and
    "acme india" against "acme". Containment is reported as what it is, because the user has to
    judge whether a subsidiary or a similarly named company counts.
    """
    if connection_norm == target_norm:
        return "their company name matches the posting's exactly"
    if len(target_norm) >= 4 and target_norm in connection_norm:
        return f"their company name contains {target_norm!r}"
    if len(connection_norm) >= 4 and connection_norm in target_norm:
        return f"the posting's company name contains {connection_norm!r}"
    return None


def build_note(
    *,
    connection: Connection,
    company: str,
    role_title: str | None,
    apply_url: str | None,
    sender_name: str,
    cited_fact: str | None,
) -> str:
    """A short opener the user edits before sending. Every value comes from a row.

    `cited_fact` is one **confirmed** `profile_facts` entry, included only when there is one --
    an unconfirmed fact is a claim the user has not agreed to make about themselves (F6), and
    this message goes to somebody who knows them well enough to notice.
    """
    first_name = connection.full_name.split(" ")[0]
    role = f"the {role_title} role" if role_title else "a role"
    lines = [f"Hi {first_name},", ""]
    if connection.position:
        # Their own words from their own profile, which is both accurate and the thing that makes
        # the message read as written by a person who looked.
        lines.append(
            f"I saw you're {connection.position} at {connection.company or company} -- "
            f"I'm applying for {role} there."
        )
    else:
        lines.append(
            f"I saw you're at {connection.company or company} -- I'm applying for {role} there."
        )
    if cited_fact:
        lines.append("")
        lines.append(cited_fact)
    if apply_url:
        lines.append("")
        lines.append(f"The posting is here: {apply_url}")
    lines.extend(
        ["", "Would you be up for a quick word, or passing my name on if it makes sense?", ""],
    )
    lines.append(f"Thanks,\n{sender_name}")
    return "\n".join(lines)


async def find(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    company: str | None = None,
    job_post_id: uuid.UUID | None = None,
    sender_name: str = "",
) -> tuple[list[Referral], str]:
    """Connections at a company, with a suggested note each. Returns `(matches, company)`.

    Matching happens **in Python over this workspace's own rows**, not in SQL, because
    containment in either direction is not a `LIKE` and because the reason for each match has to
    travel back with it. A connections export is at most 30,000 rows, which is a list comparison
    rather than a problem.
    """
    post: JobPost | None = None
    if job_post_id is not None:
        post = await session.scalar(
            select(JobPost).where(JobPost.id == job_post_id, JobPost.org_id == ctx.org_id)
        )
        if post is None:
            # Scoped rather than 403: an id from another workspace resolves to nothing (ADR-004).
            return [], ""

    target = (company or "").strip()
    if not target and post is not None:
        # **The posting's host is the job board's, so its first label was "boards"** -- which
        # matched every connection whose company contains that word, confidently and wrongly
        # (ADR-047). `jobcompany.resolve` answers with the company's own domain when anything
        # knows it, and otherwise with the board slug, which *is* the company's name on that
        # board: `vercel`, `ramp`. A label from the slug is a fact the feed gave us; a label
        # from the vendor's host was one we manufactured.
        ref = await jobcompany.resolve(session, post)
        target = (ref.domain or "").split(".")[0] or ref.label
    target_norm = normalise_company(target)
    if not target_norm:
        return [], target

    rows = (await session.scalars(select(Connection).where(Connection.org_id == ctx.org_id))).all()

    #: One confirmed fact to cite, if the workspace has any. Confirmed only: F6's rule is that a
    #: fact the person has not agreed to is not theirs to assert, and that bites hardest in a
    #: message to somebody who knows them.
    fact = await session.scalar(
        select(ProfileFact)
        .where(ProfileFact.org_id == ctx.org_id, ProfileFact.confirmed_at.is_not(None))
        .order_by(ProfileFact.created_at)
        .limit(1)
    )
    cited = fact.statement if fact is not None else None

    matches: list[Referral] = []
    for row in rows:
        if not row.company_norm:
            continue
        why = _match_reason(row.company_norm, target_norm)
        if why is None:
            continue
        matches.append(
            Referral(
                connection_id=row.id,
                full_name=row.full_name,
                position=row.position,
                company=row.company,
                profile_url=row.profile_url,
                connected_on=row.connected_on.isoformat() if row.connected_on else None,
                why=why,
                note=build_note(
                    connection=row,
                    company=target,
                    role_title=post.title if post else None,
                    apply_url=post.apply_url if post else None,
                    sender_name=sender_name or "",
                    cited_fact=cited,
                ),
            )
        )
        if len(matches) >= MAX_MATCHES:
            break
    return matches, target
