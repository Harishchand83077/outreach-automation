"""What happens to a lead the moment it is created, wherever it came from.

Extracted from the import route when a second caller appeared -- importing a contact a provider
proposed (F9). **Copying it would have been this project's signature defect again**, in a place
where the two copies would disagree about something that matters: which job a template campaign
queues, and how a large import is paced against a free tier's daily limits. One of those copies
would have been corrected and the other would not.

Two rules live here and both are decisions rather than mechanics:

- **Which job depends on the campaign's mode.** A `research` campaign researches first; a
  `template` campaign goes straight to drafting, because there is nothing to research -- the
  email is already written and the row fills in the blanks. Queueing research for it would spend
  a page fetch and a model call to produce findings nothing reads.
- **A research campaign's jobs are staggered over the days the leads could actually be sent.**
  Per-kind concurrency stops the worker melting; nothing used to pace against the model chain's
  *daily* limit, so a thousand leads spent the quota in minutes and every later job failed into
  a cool-down -- a stalled pipeline with no visible cause.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Lead
from app.db.queue import JobKind, enqueue
from app.domain.campaign import CampaignSettings

#: One day of leads is made runnable immediately, so there is always a drafted queue to approve
#: from without a thousand leads racing the free tier on day one.
HEAD_START_DAYS = 1


def due_at(offset: int, campaign: Campaign, *, template_mode: bool) -> datetime | None:
    """When the nth lead's first job becomes runnable.

    `None` -- runnable now -- for a template campaign: rendering is local and instant, costs no
    quota, and a thousand drafts in seconds is the point of that mode.

    For a research campaign the pace is the campaign's own daily cap, because that is the real
    ceiling on how many of these leads can ever be emailed. An unreadable settings row falls
    back to the product defaults rather than raising: pacing is not the place to refuse work,
    and `blockers` is where a bad row is reported.
    """
    if template_mode:
        return None
    try:
        settings = CampaignSettings.model_validate(campaign.settings or {})
    except ValidationError:
        settings = CampaignSettings()
    per_day = max(settings.daily_cap, 1)
    day = max(offset // per_day - HEAD_START_DAYS, 0)
    if day == 0:
        return None
    return datetime.now(tz=UTC) + timedelta(days=day)


async def queue_for(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    campaign: Campaign,
    leads: list[Lead],
    start_offset: int = 0,
) -> None:
    """Queue each new lead's first job, in the caller's transaction.

    The caller commits, so a lead never exists without the work that moves it forward and a
    rollback takes both. `QueueFull` is deliberately **not** caught here: the right answer
    differs per caller -- an import rolls back wholesale and answers 429 -- and swallowing it
    would leave leads sitting `NEW` for ever, looking imported and doing nothing.
    """
    template_mode = campaign.mode == "template"
    kind = JobKind.DRAFT_LEAD if template_mode else JobKind.RESEARCH_LEAD
    prefix = "draft" if template_mode else "research"
    for offset, lead in enumerate(leads, start=start_offset):
        await enqueue(
            session,
            org_id=org_id,
            kind=kind,
            lead_id=lead.id,
            payload={"lead_id": str(lead.id)},
            dedupe_key=f"{prefix}:{lead.id}",
            run_after=due_at(offset, campaign, template_mode=template_mode),
        )
