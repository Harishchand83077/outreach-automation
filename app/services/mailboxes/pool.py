"""Which mailboxes a campaign may send from, and which one a new lead gets (F7, ADR-044).

**One function answers "what is this campaign's pool?", and everything reads it.** The send loop,
the campaign blockers and the dashboard all call `pool_for`. The alternative -- each of them
joining `campaign_mailboxes` itself -- is this project's dominant defect waiting to happen: the
first time the rule gains a condition, one caller would get it and another would not, and the
symptom would be a campaign that sends from a mailbox the screen says it does not use.

**The rule.** `campaigns.mailbox_id` is the campaign's primary mailbox and is **always** a member
of its own pool, with or without a `campaign_mailboxes` row. Every other row in that table is an
additional member. A row for the primary is allowed, and is how its weight becomes anything but
1. Making the table the sole authority was considered and rejected (ADR-044): it would mean every
existing campaign and every future `POST /campaigns` had to write a row, and the one that forgot
would stop sending with nothing on screen saying why.

**Selection is by lowest weighted load, not by a stored cursor.** For each usable member we have
today's send count, which is already counted from `messages` rather than kept as a counter
(§9.2), so the pool needs no state of its own and nothing can drift. The member with the
smallest `sent_today / weight` goes next, ties broken by mailbox id so two workers asked the same
question get the same answer.

**The choice is only a proposal.** `outbox.claim_send` stamps `leads.mailbox_id` under the lead's
row lock and refuses a lead already assigned elsewhere, so the stickiness is enforced where the
failure can be contained rather than where it is first computed -- the lesson of the lazy
`Candidate.persona` properties that once crash-looped every mailbox.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, CampaignMailbox, Mailbox

DEFAULT_WEIGHT = 1


@dataclass(frozen=True)
class PoolMember:
    mailbox_id: uuid.UUID
    weight: int
    #: True for `campaigns.mailbox_id`. Carried so the dashboard can say which one detaching the
    #: campaign's mailbox would remove, rather than presenting the pool as a flat list.
    primary: bool = False


async def pool_for(
    session: AsyncSession, *, org_id: uuid.UUID, campaign: Campaign
) -> list[PoolMember]:
    """Every mailbox this campaign may send from, primary first then by mailbox id."""
    rows = list(
        (
            await session.scalars(
                select(CampaignMailbox).where(
                    CampaignMailbox.org_id == org_id,
                    CampaignMailbox.campaign_id == campaign.id,
                )
            )
        ).all()
    )
    weights = {row.mailbox_id: int(row.weight) for row in rows}

    members: list[PoolMember] = []
    if campaign.mailbox_id is not None:
        members.append(
            PoolMember(
                mailbox_id=campaign.mailbox_id,
                weight=weights.get(campaign.mailbox_id, DEFAULT_WEIGHT),
                primary=True,
            )
        )
    extra = sorted(
        (one for one in weights if one != campaign.mailbox_id),
        key=str,
    )
    members.extend(PoolMember(mailbox_id=one, weight=weights[one]) for one in extra)
    return members


async def campaign_ids_for(
    session: AsyncSession, *, org_id: uuid.UUID, mailbox_id: uuid.UUID
) -> list[uuid.UUID]:
    """Campaigns whose pool contains this mailbox, primary membership included.

    The inverse of `pool_for`, and it has to exist because the send loop works mailbox by mailbox
    -- so the question it asks is "whose work may I do?", not "what may this campaign use?". Both
    directions are here rather than in the loop, so a change to the rule changes both at once.
    """
    primary = await session.scalars(
        select(Campaign.id).where(Campaign.org_id == org_id, Campaign.mailbox_id == mailbox_id)
    )
    pooled = await session.scalars(
        select(CampaignMailbox.campaign_id).where(
            CampaignMailbox.org_id == org_id, CampaignMailbox.mailbox_id == mailbox_id
        )
    )
    found: list[uuid.UUID] = []
    for one in (*primary.all(), *pooled.all()):
        if one not in found:
            found.append(one)
    return found


def choose(
    members: list[PoolMember], *, counts: dict[uuid.UUID, int], usable: set[uuid.UUID]
) -> uuid.UUID | None:
    """Which member a lead with no sender yet should get. `None` when none can send.

    Lowest `sent_today / weight` wins, so over a day the shares come out in proportion to the
    weights without anybody storing a position in a rotation. Ties go to the lowest mailbox id,
    which makes the answer the same for every caller -- a rotation that depended on who asked
    could hand the same lead to two mailboxes in one pass.

    A paused, unconnected or missing mailbox is simply not in `usable`, so a pool of two with one
    broken member keeps sending from the other. Degrading to a smaller pool is the whole point of
    having one; stopping because a member is unavailable would make a pool worse than no pool.
    """
    eligible = [member for member in members if member.mailbox_id in usable]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda member: (
            counts.get(member.mailbox_id, 0) / max(member.weight, 1),
            str(member.mailbox_id),
        ),
    ).mailbox_id


async def usable_ids(
    session: AsyncSession, *, org_id: uuid.UUID, mailbox_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of these mailboxes could send at all: present, in this org and not paused.

    **Non-networked**, like `GET /mailboxes` and for the same reason: this is read on every send
    pass and resolving a credential would mint an OAuth token each time. Whether the credential
    actually works is decided by `login_for` at the moment of sending, which is where a failure
    writes `invalid_reason` once rather than on every cycle.
    """
    if not mailbox_ids:
        return set()
    rows = await session.scalars(
        select(Mailbox.id).where(
            Mailbox.org_id == org_id,
            Mailbox.id.in_(mailbox_ids),
            Mailbox.paused.is_(False),
        )
    )
    return set(rows.all())
