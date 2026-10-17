"""Retention: forgetting on a schedule rather than on request.

`ARCHITECTURE_TARGET.md` §17 and §19, and `MASTER_PROMPT.md` **Q7**, whose default is twelve
months. Two sweeps, run by the `Scheduler`:

- **Evidence text** older than `RESEARCH_TEXT_RETENTION_DAYS` is cleared from `domain_research`.
  That is public page text about a company rather than anything about a person, so the row —
  its findings, its sources, its confidence — is kept and only the bulk text is dropped. It is
  the largest thing in the database per lead and the least useful once a draft has been written.
- **Leads that reached a terminal state** longer than `LEAD_RETENTION_MONTHS` ago are erased in
  full, through the same path a data-subject request takes, **including the hashed suppression**.
  Keeping that is the whole point: a lead that opted out thirteen months ago must not become
  contactable again merely because the system tidied up.

Two deliberate exclusions:

- **Leads still in flight are never touched**, however old. Retention removes finished work, and
  a lead mid-conversation is not finished. Age alone is not a reason to drop somebody from a
  thread they are still replying to.
- **`llm_usage` and `search_usage` are left alone.** They hold no personal data once a lead is
  gone (their `lead_id` is set to NULL), and they are what an operator reads to decide whether
  this system is affordable. Deleting them would quietly falsify that.

The sweep is idempotent and bounded: it processes at most `batch` leads per run, so a database
that has gone a year untended is cleared over several ticks rather than in one transaction that
locks the table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DomainResearch, Lead, Message
from app.db.repo import OrgContext
from app.domain.state import TERMINAL
from app.services.privacy.erasure import erase_lead

logger = logging.getLogger(__name__)

#: How long an unmatched inbound message is kept. `match_lead`'s own fallback looks back 30 days,
#: so beyond that an orphan is not a reply we might still place -- it is somebody else's mail.
DEFAULT_ORPHAN_DAYS = 45

#: How many leads one sweep will erase. Small, because each erasure cascades across five tables
#: and this runs beside the send loop on a 2 GB box.
DEFAULT_BATCH = 50


@dataclass
class RetentionReport:
    """What one sweep did. Counts only; never the values removed."""

    leads_erased: int = 0
    evidence_cleared: int = 0
    orphans_deleted: int = 0

    def __bool__(self) -> bool:
        return bool(self.leads_erased or self.evidence_cleared or self.orphans_deleted)


async def clear_old_evidence(
    session: AsyncSession, *, older_than: timedelta, now: datetime | None = None
) -> int:
    """Drop `evidence_text` from domain research fetched longer ago than `older_than`.

    The row survives: its findings are what a draft cites, and they are small. The text is the
    bulk, and once a draft exists it has served its purpose.
    """
    now = now or datetime.now(tz=UTC)
    statement = (
        update(DomainResearch)
        .where(
            DomainResearch.fetched_at < now - older_than,
            DomainResearch.evidence_text.is_not(None),
        )
        .values(evidence_text=None)
    )
    result = await session.execute(statement)
    # `rowcount` is on the cursor result rather than the typed Result protocol.
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_orphan_inbound(
    session: AsyncSession, *, older_than: timedelta, now: datetime | None = None
) -> int:
    """Delete inbound messages that never matched a lead and now never will.

    Unmatched inbound mail is stored on purpose (`messages.lead_id` is nullable), so a reply we
    failed to match is not lost -- and `match_lead`'s own fallback only looks back 30 days, so
    after that window an orphan is not a reply we might still place. It is somebody else's mail.

    Which makes this a **privacy** rule before it is a storage one. One run against a personal
    inbox ingested 1,400 unrelated messages: harmless to the pipeline, because matching happens
    before any model is called, but it is other people's correspondence sitting in our database
    with no reason to be there.

    Only orphans. A message attached to a lead is part of that lead's record and is governed by
    `LEAD_RETENTION_MONTHS` instead.
    """
    now = now or datetime.now(tz=UTC)
    result = await session.execute(
        delete(Message).where(
            Message.lead_id.is_(None),
            Message.direction == "in",
            Message.created_at < now - older_than,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def erase_expired_leads(
    session: AsyncSession,
    *,
    older_than: timedelta,
    batch: int = DEFAULT_BATCH,
    now: datetime | None = None,
) -> int:
    """Erase terminal leads whose state has not changed for longer than `older_than`.

    Scoped per org, because `erase_lead` takes an `OrgContext` and tenancy is not something to
    take a shortcut around even in a background sweep.
    """
    now = now or datetime.now(tz=UTC)
    cutoff = now - older_than

    result = await session.execute(
        select(Lead.id, Lead.org_id)
        .where(
            Lead.state.in_([state.value for state in TERMINAL]),
            Lead.state_changed_at < cutoff,
        )
        .order_by(Lead.state_changed_at)
        .limit(batch)
    )
    rows = list(result.all())

    erased = 0
    for lead_id, org_id in rows:
        # `actor_id` names the sweep rather than a person, so the log line says why a lead
        # vanished without implying somebody asked for it.
        ctx = OrgContext(org_id=org_id, actor_id="system:retention")
        try:
            await erase_lead(session, ctx, lead_id)
        except LookupError:  # pragma: no cover - raced with a manual erasure
            continue
        erased += 1
    return erased


async def run_retention(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lead_retention: timedelta,
    evidence_retention: timedelta,
    batch: int = DEFAULT_BATCH,
    orphan_retention: timedelta = timedelta(days=DEFAULT_ORPHAN_DAYS),
    now: datetime | None = None,
) -> RetentionReport:
    """One sweep. Each part commits separately, so a failure in one does not undo the other."""
    report = RetentionReport()

    async with session_factory() as session, session.begin():
        report.evidence_cleared = await clear_old_evidence(
            session, older_than=evidence_retention, now=now
        )

    async with session_factory() as session, session.begin():
        report.leads_erased = await erase_expired_leads(
            session, older_than=lead_retention, batch=batch, now=now
        )

    async with session_factory() as session, session.begin():
        report.orphans_deleted = await delete_orphan_inbound(
            session, older_than=orphan_retention, now=now
        )

    if report:
        logger.info(
            "retention sweep",
            extra={
                "leads_erased": report.leads_erased,
                "evidence_cleared": report.evidence_cleared,
                "orphans_deleted": report.orphans_deleted,
            },
        )
    return report
