"""What the five tasks share.

Dependencies are assembled once here and handed to every task, so a test can build a context out
of fakes and drive the real loops without a network, an SMTP server or a model.

One thing deliberately is *not* assembled once: the `LLMRouter`. Its usage store is bound to a
session and an org (`DbUsageStore`), because a daily spend cap that is not scoped to an org is
not a cap at all in a multi-tenant system. So the context exposes `router_for(session, org_id)`
and each unit of work builds its own, inside the transaction that will record what it spent.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings, config
from app.db.engine import get_sessionmaker
from app.services.llm.entries import LLMChain, parse_chain
from app.services.llm.router import LLMRouter
from app.services.llm.usage import DbUsageStore, UsageRecord
from app.services.mail.imap import ImapPoller
from app.services.mail.smtp import SmtpSender
from app.worker.health import WorkerHealth


class PooledUsageStore:
    """A `UsageStore` that opens its own short transaction per call.

    `DbUsageStore` is bound to one session, which is right for a job: the spend and the work it
    paid for commit together. The ingest loop cannot use it, because it opens a session per
    message internally and the router outlives all of them. This store takes the session factory
    instead, so accounting still lands in the database and a daily cap still means something.
    """

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], org_id: uuid.UUID
    ) -> None:
        self._session_factory = session_factory
        self._org_id = org_id

    async def record(self, usage: UsageRecord) -> None:
        async with self._session_factory() as session, session.begin():
            await DbUsageStore(session, self._org_id).record(usage)

    async def tokens_today(self, entry_name: str, *, now: datetime | None = None) -> int:
        async with self._session_factory() as session:
            return await DbUsageStore(session, self._org_id).tokens_today(entry_name, now=now)

    async def tokens_in_last_minute(self, entry_name: str, *, now: datetime | None = None) -> int:
        async with self._session_factory() as session:
            return await DbUsageStore(session, self._org_id).tokens_in_last_minute(
                entry_name, now=now
            )

    async def requests_in_last_minute(self, entry_name: str, *, now: datetime | None = None) -> int:
        async with self._session_factory() as session:
            return await DbUsageStore(session, self._org_id).requests_in_last_minute(
                entry_name, now=now
            )


@dataclass
class WorkerContext:
    """Everything the tasks need, and nothing they do not."""

    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    poller: ImapPoller
    sender: SmtpSender
    health: WorkerHealth = field(default_factory=WorkerHealth)
    #: Identifies this process in the `worker_heartbeats` table and in every log line it writes.
    #: Host plus pid, so two workers on one box are two rows rather than one row fighting over
    #: itself.
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}:{os.getpid()}")

    @classmethod
    def build(cls, settings: Settings | None = None) -> WorkerContext:
        """Wire the real collaborators from configuration."""
        settings = settings or config
        return cls(
            session_factory=get_sessionmaker(),
            settings=settings,
            poller=ImapPoller(settings),
            sender=SmtpSender(settings),
        )

    def router_for(self, session: AsyncSession, org_id: uuid.UUID) -> LLMRouter:
        """A router whose spend is accounted against this org, in this transaction."""
        return LLMRouter(self._chain(), DbUsageStore(session, org_id))

    def router_for_org(self, org_id: uuid.UUID) -> LLMRouter:
        """A router for code that manages its own sessions, such as the ingest loop."""
        return LLMRouter(self._chain(), PooledUsageStore(self.session_factory, org_id))

    def _chain(self) -> LLMChain:
        return parse_chain(
            self.settings.LLM_CHAIN,
            paid_enabled=self.settings.PAID_LLM_ENABLED,
            paid_daily_usd_cap=self.settings.PAID_LLM_DAILY_USD_CAP,
        )
