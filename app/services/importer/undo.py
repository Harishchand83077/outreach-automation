"""Undo an import: remove the leads an upload created, while none of them has been emailed.

The question this answers is "I uploaded the wrong sheet". Until now the only way back was
`DELETE /leads/{id}`, one lead at a time, and **that is the wrong tool** -- it is the erasure path
for a data-subject request, so it deliberately leaves a hashed suppression behind. Undoing a
mistaken import that way would permanently block every address in it: re-uploading the corrected
sheet would skip exactly the people it was meant to reach, and the operator would have no way of
knowing why.

So this is a different operation with a different rule. **Nothing is suppressed**, because nobody
asked not to be contacted; a sheet was uploaded by mistake, and the correct state of the world is
that it never happened.

**What it will not do is unsay anything.** A lead that has been emailed stays, whatever the
operator meant to upload. The record of what we sent to whom is the one thing in this system that
must not be editable -- a reply can still arrive, an opt-out has to be honourable, and a bounce
has to be attributable. Those leads are reported as kept, with the count, so the answer is "9
removed, 1 kept because it has already been emailed" rather than a silent partial success.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lead, Message
from app.db.repo import OrgContext
from app.domain.state import LeadState

#: States a lead may be in and still be removed by an undo: everything before a message has
#: been handed to SMTP. `APPROVED` is included deliberately -- an approved draft has not been
#: sent, and an operator who has realised the sheet is wrong should not have to wait for it to
#: go out first. The send loop re-checks the lead's existence when it claims the outbox row.
REMOVABLE_STATES: frozenset[str] = frozenset(
    {
        LeadState.NEW.value,
        LeadState.ENRICHING.value,
        LeadState.RESEARCHED.value,
        LeadState.DRAFTED.value,
        LeadState.AWAITING_APPROVAL.value,
        LeadState.APPROVED.value,
        LeadState.SKIPPED.value,
        LeadState.FAILED.value,
    }
)


@dataclass
class UndoReport:
    """What an undo did, and what it refused to do."""

    removed: int = 0
    #: Leads left in place, keyed by the reason, so the operator is told rather than left to
    #: compare counts.
    kept: dict[str, int] = field(default_factory=dict)

    @property
    def total_kept(self) -> int:
        return sum(self.kept.values())

    def summary(self) -> str:
        if not self.total_kept:
            return f"Removed {self.removed} lead(s)."
        reasons = "; ".join(f"{count} {reason}" for reason, count in sorted(self.kept.items()))
        return f"Removed {self.removed} lead(s). Kept {self.total_kept}: {reasons}."


async def undo_import(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> UndoReport:
    """Delete the un-emailed leads of one import batch. Never suppresses anything.

    The caller commits. Deleting a lead cascades to its drafts, research, events and queued
    jobs, which is what makes this a real undo rather than a tidy-up: a research job for a lead
    that no longer exists is already handled as a permanent failure, not a retry.
    """
    report = UndoReport()

    rows = (
        await session.execute(
            select(Lead.id, Lead.state)
            .where(
                Lead.org_id == ctx.org_id,
                Lead.campaign_id == campaign_id,
                Lead.import_batch_id == batch_id,
            )
            .with_for_update()
        )
    ).all()

    if not rows:
        return report

    # One query rather than one per lead: a sheet is hundreds of rows and this runs inside a
    # request. `direction == "out"` covers `sending` as well as `sent` -- a message claimed but
    # not yet resolved may still have left, and ADR-002 is explicit that we never assume it did
    # not.
    emailed = set(
        (
            await session.execute(
                select(Message.lead_id)
                .where(
                    Message.org_id == ctx.org_id,
                    Message.lead_id.in_([lead_id for lead_id, _ in rows]),
                    Message.direction == "out",
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )

    removable: list[uuid.UUID] = []
    for lead_id, state in rows:
        if lead_id in emailed:
            report.kept["already emailed"] = report.kept.get("already emailed", 0) + 1
            continue
        if state not in REMOVABLE_STATES:
            # In conversation, replied, bounced, opted out, meeting booked. Each of those is
            # something that happened to a person, and an undo does not get to unsay it.
            report.kept[f"in {state}"] = report.kept.get(f"in {state}", 0) + 1
            continue
        removable.append(lead_id)

    if removable:
        await session.execute(delete(Lead).where(Lead.id.in_(removable)))
        report.removed = len(removable)

    return report


async def batch_summary(
    session: AsyncSession, ctx: OrgContext, *, campaign_id: uuid.UUID, batch_id: uuid.UUID
) -> dict[str, int]:
    """How many leads of this batch survive, and how many can still be undone.

    Read by the dashboard so the undo button can say what it will do before it is pressed, and
    disappear once there is nothing left to undo.
    """
    total = await session.scalar(
        select(func.count())
        .select_from(Lead)
        .where(
            Lead.org_id == ctx.org_id,
            Lead.campaign_id == campaign_id,
            Lead.import_batch_id == batch_id,
        )
    )
    emailed = await session.scalar(
        select(func.count(func.distinct(Message.lead_id)))
        .select_from(Message)
        .join(Lead, Lead.id == Message.lead_id)
        .where(
            Lead.org_id == ctx.org_id,
            Lead.campaign_id == campaign_id,
            Lead.import_batch_id == batch_id,
            Message.direction == "out",
        )
    )
    return {"leads": int(total or 0), "emailed": int(emailed or 0)}
