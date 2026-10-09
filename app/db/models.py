"""SQLAlchemy 2.0 models — the Phase 1 schema.

Follows `ARCHITECTURE_TARGET.md` §5. Every table except `orgs` carries `org_id`, from the first
migration onward (ADR-004), because adding a tenancy column to a populated schema later is the
expensive kind of migration. Timestamps are `timestamptz` in UTC; conversion to IST happens only
in `app.domain.scheduling`.

Tables that belong to later phases — `research`, `domain_research`, `classifications`,
`llm_usage`, `search_usage`, `imap_cursors`, `alerts`, `worker_heartbeats`, `imports`,
`members` — are deliberately absent. They are added by the phase that first needs them rather
than guessed at now.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "app"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now_col(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


class Org(Base):
    """A tenant. One today, but the column exists everywhere from the start."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Org-level kill switch. A flag, never a state, so pausing loses no position.
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)


class Mailbox(Base):
    """A sending identity. Secrets stay in the environment; `secret_ref` names the group."""

    __tablename__ = "mailboxes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    from_name: Mapped[str] = mapped_column(String(200), nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String(320))
    secret_ref: Mapped[str] = mapped_column(String(100), nullable=False, default="default")

    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    warmup_started_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    warmup_start: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    warmup_step: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    warmup_step_days: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    #: The send loop's pacing cursor — one email goes out when now >= this.
    next_send_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "from_email", name="uq_mailboxes_org_from_email"),
        CheckConstraint("daily_cap > 0", name="ck_mailboxes_daily_cap_positive"),
    )


class Campaign(Base):
    """Persona and settings live as JSONB, validated by Pydantic at the edges."""

    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    persona: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','running','paused','completed','archived')",
            name="ck_campaigns_status",
        ),
    )


class Lead(Base):
    """One person in one campaign.

    `UNIQUE(campaign_id, email_norm)` rather than a global unique on email, so the same person
    can appear in a later campaign and the same address can exist at two companies. The
    prototype made email globally unique and used `INSERT OR IGNORE`, so re-uploading a
    corrected sheet silently changed nothing (issue B16).
    """

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )

    email_norm: Mapped[str] = mapped_column(String(320), nullable=False)
    email_raw: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    company_name: Mapped[str | None] = mapped_column(String(200))
    company_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(253))
    job_title: Mapped[str | None] = mapped_column(String(200))
    #: Stored and displayed only. Never fetched — hard rule 4.
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    #: Every extra sheet column, kept so research and drafting can use it (issue A5).
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Only `app.db.state.transition()` may write this.
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW")
    state_changed_at: Mapped[datetime] = _now_col(nullable=False)
    followups_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: When the scheduler should next look at this lead. "Follow-up due" is computed from
    #: (state, next_action_at), never stored as a state of its own.
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "email_norm", name="uq_leads_campaign_email"),
        Index("ix_leads_org_state", "org_id", "state"),
        # The scheduler's hot query: due work, cheapest possible scan.
        Index(
            "ix_leads_due",
            "state",
            "next_action_at",
            postgresql_where=text("next_action_at IS NOT NULL"),
        ),
    )


class Draft(Base):
    """A new row per edit or regeneration — never overwritten, so history is intact (E3)."""

    __tablename__ = "drafts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Each asserted claim with the evidence it maps to: a research hook or a persona proof id.
    claims: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    feedback: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    created_by: Mapped[str | None] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(200))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("lead_id", "kind", "version", name="uq_drafts_lead_kind_version"),
        CheckConstraint("kind IN ('initial','followup','reply')", name="ck_drafts_kind"),
        CheckConstraint(
            "status IN ('generated','validated','validation_failed','pending_review',"
            "'approved','rejected','superseded','sent')",
            name="ck_drafts_status",
        ),
    )


class Message(Base):
    """The outbox, and the record of what was actually sent or received.

    `idempotency_key` is UNIQUE and assigned before the SMTP call, with the `Message-ID`
    pre-generated. A crash between sending and marking can therefore never produce a second
    send: the row already exists. At-most-once by construction (ARCHITECTURE_TARGET §9.5), which
    is the trade we want — a missed cold email is cheaper than a duplicate one. The prototype
    sent first and wrote the database afterwards, so a crash in between resent on the next run
    (issue B12).
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="SET NULL")
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.drafts.id", ondelete="SET NULL")
    )

    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(20))

    #: RFC 5322 threading headers, so replies match on identity rather than guesswork (B11).
    message_id_hdr: Mapped[str | None] = mapped_column(String(998))
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references_hdr: Mapped[str | None] = mapped_column(Text)

    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    from_addr: Mapped[str | None] = mapped_column(String(320))
    to_addr: Mapped[str | None] = mapped_column(String(320))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="sending")
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    smtp_response: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imap_uid: Mapped[int | None] = mapped_column(BigInteger)
    imap_folder: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_messages_idempotency_key"),
        CheckConstraint("direction IN ('out','in')", name="ck_messages_direction"),
        CheckConstraint(
            "status IN ('sending','sent','failed','unknown','received')",
            name="ck_messages_status",
        ),
        # An outbound message must carry the key that makes it idempotent.
        CheckConstraint(
            "direction = 'in' OR idempotency_key IS NOT NULL",
            name="ck_messages_out_has_idempotency_key",
        ),
        Index("ix_messages_lead", "lead_id", "created_at"),
        Index("ix_messages_message_id_hdr", "message_id_hdr"),
        # Counting today's sends from this table means the cap cannot drift (§9.2).
        Index("ix_messages_sent_at", "mailbox_id", "sent_at"),
    )


class Event(Base):
    """Append-only audit log. A trigger blocks UPDATE and DELETE (see migration 1).

    This is what lets us answer "what did we send this person, and why?" — a question the
    prototype's single mutable row could not answer at all (issue E3).
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="CASCADE")
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="CASCADE")
    )
    #: Who caused this: a person, the system, or a model.
    actor: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint("actor IN ('user','system','llm')", name="ck_events_actor"),
        Index("ix_events_lead_at", "lead_id", "at"),
        Index("ix_events_org_at", "org_id", "at"),
    )


class Job(Base):
    """The queue. Claimed with `SELECT ... FOR UPDATE SKIP LOCKED` — no Redis, no Celery.

    `dedupe_key` plus a partial unique index means enqueueing the same work twice is a no-op
    while it is still queued or running.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="CASCADE")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    run_after: Mapped[datetime] = _now_col(nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_col(nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','dead','cancelled')",
            name="ck_jobs_status",
        ),
        # The claim query's covering index.
        Index(
            "ix_jobs_claimable",
            "status",
            "run_after",
            "priority",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_lease", "status", "locked_until"),
    )


class Suppression(Base):
    """Addresses and domains we must never contact. Checked at import and before every send."""

    __tablename__ = "suppressions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.messages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "kind", "value", name="uq_suppressions_org_kind_value"),
        CheckConstraint("kind IN ('email','domain')", name="ck_suppressions_kind"),
        CheckConstraint(
            "reason IN ('opt_out','bounce','manual','complaint')",
            name="ck_suppressions_reason",
        ),
    )
