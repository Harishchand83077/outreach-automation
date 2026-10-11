"""Erasing one lead, on request.

`ARCHITECTURE_TARGET.md` §17 (“Data subject requests”) and issue D6, which recorded that there
was no `DELETE` route in the API at all.

**The rule that shapes everything here: forgetting somebody must not mean contacting them
again.** A naive erasure deletes the lead and the opt-out that came with them, and the next
import of the same sheet mails the person who asked to be left alone — turning a privacy
request into the precise harm it was meant to prevent. So erasure *adds* a suppression before it
deletes anything, keyed on a **hash** of the address rather than the address itself.

What the hash does and does not buy, stated plainly rather than implied: it means the database
no longer holds the address, so a dump of it does not disclose who was contacted. It is not
anonymisation. An email address has little entropy, so anyone holding both this table and a
candidate address can confirm a match. That is unavoidable in any scheme that must recognise the
address again, and recognising it again is the entire point. `PRIVACY.md` says so in those words.

What is deleted, and what deliberately survives:

**Deleted:** the `leads` row, and by cascade its drafts, messages, events, jobs and research;
classifications go with their message.

**Survives:** `llm_usage` and `search_usage` rows, with `lead_id` set to NULL; the hashed
suppression; and `domain_research`, which is public page text about a company rather than
anything about a person.

The accounting rows survive on purpose: they record what was spent, hold no personal data once
the id is cleared, and deleting them would silently falsify the usage figures an operator uses
to decide whether the system is affordable.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Draft, Event, Lead, Message, Research, Suppression
from app.db.repo import OrgContext

logger = logging.getLogger(__name__)

#: `suppressions.kind` for an address we hold only as a digest. Matched at send time alongside
#: the plain-text kinds, so an erased person stays unreachable.
EMAIL_HASH = "email_hash"

#: Domain separation, so these digests cannot be compared against a rainbow table built for
#: bare addresses, and so a digest from this table is recognisable as ours.
_HASH_PREFIX = "outreach-suppression:v1:"


def hashed_email(email: str) -> str:
    """A stable digest of a normalised address.

    Normalised the same way `is_suppressed` normalises before comparing, or the digest would
    only match when the caller happened to use the same capitalisation.
    """
    normalised = email.strip().lower()
    return hashlib.sha256((_HASH_PREFIX + normalised).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ErasureResult:
    """What one erasure removed. Counts only — never the values that were removed."""

    lead_id: uuid.UUID
    drafts: int
    messages: int
    research: int
    events: int
    suppression_kept: bool

    @property
    def rows(self) -> int:
        return self.drafts + self.messages + self.research + self.events + 1


async def erase_lead(
    session: AsyncSession,
    ctx: OrgContext,
    lead_id: uuid.UUID,
    *,
    keep_suppressed: bool = True,
) -> ErasureResult:
    """Erase one lead and everything personal attached to it.

    Raises `LookupError` when the id is not this org's — the same answer as for an id that does
    not exist, because confirming an id belongs to somebody else is itself a disclosure.

    `keep_suppressed` exists for the one case where it should be false: erasing a lead that was
    imported by mistake and never contacted, where there is no opt-out to honour and no reason
    to retain even a digest. It defaults to true because the costly mistake runs the other way.
    """
    lead = (
        await session.execute(select(Lead).where(Lead.id == lead_id, Lead.org_id == ctx.org_id))
    ).scalar_one_or_none()
    if lead is None:
        raise LookupError(f"no lead {lead_id}")

    email = lead.email_norm

    async def count(statement: Select[int]) -> int:
        return int(await session.scalar(statement) or 0)

    drafts = await count(select(func.count()).select_from(Draft).where(Draft.lead_id == lead_id))
    messages = await count(
        select(func.count()).select_from(Message).where(Message.lead_id == lead_id)
    )
    research = await count(
        select(func.count()).select_from(Research).where(Research.lead_id == lead_id)
    )
    events = await count(select(func.count()).select_from(Event).where(Event.lead_id == lead_id))

    if keep_suppressed and email:
        # Before the delete, not after: if the transaction fails between the two, the safe
        # outcome is a suppression with no lead rather than a lead erased with no suppression.
        await session.execute(
            insert(Suppression)
            .values(
                org_id=ctx.org_id,
                kind=EMAIL_HASH,
                value=hashed_email(email),
                reason="manual",
            )
            .on_conflict_do_nothing(index_elements=["org_id", "kind", "value"])
        )

    # `events` is append-only, enforced by a trigger. The migration that created it left exactly
    # one escape hatch, for exactly this case. SET LOCAL, so it lasts only this transaction.
    await session.execute(text("SET LOCAL app.allow_event_delete = 'on'"))
    await session.delete(lead)
    await session.flush()

    logger.info(
        "lead erased",
        extra={
            "lead_id": str(lead_id),
            "org_id": str(ctx.org_id),
            "actor": ctx.actor_id,
            "drafts": drafts,
            "messages": messages,
            "research": research,
            "events": events,
            "suppression_kept": keep_suppressed,
        },
    )
    return ErasureResult(
        lead_id=lead_id,
        drafts=drafts,
        messages=messages,
        research=research,
        events=events,
        suppression_kept=keep_suppressed and bool(email),
    )
