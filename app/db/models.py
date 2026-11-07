"""SQLAlchemy 2.0 models — the Phase 1 schema.

Follows `ARCHITECTURE_TARGET.md` §5. Every table except `orgs` carries `org_id`, from the first
migration onward (ADR-004), because adding a tenancy column to a populated schema later is the
expensive kind of migration. Timestamps are `timestamptz` in UTC; conversion to IST happens only
in `app.domain.scheduling`.

Tables that belong to later phases — `research`, `domain_research`, `classifications`,
`llm_usage`, `search_usage`, `imap_cursors`, `imports`, `members` — were deliberately absent
from the first migration and added by the phase that first needed them, rather than guessed at
in advance. `alerts` and `worker_heartbeats` arrived with Phase 9.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
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
    #: The operator's chosen send mode, or NULL for "use the environment" (ADR-010). Nullable
    #: rather than defaulted, so adding the column changed no org's behaviour.
    send_mode: Mapped[str | None] = mapped_column(String(20))
    #: Who last changed it, and when. A switch that turns real mail on needs an answer to "who
    #: did this"; the `events` table cannot hold it because every row there is keyed to a lead.
    send_mode_set_by: Mapped[str | None] = mapped_column(String(320))
    send_mode_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: May this org's outcomes be pooled with other orgs' for analysis?
    #:
    #: **Shipped now and read by nothing**, deliberately. The flag has to predate the data: if it
    #: were added later, pooling could be switched on retroactively over results gathered while
    #: nobody had been asked. Defaulting to false means adding it changes nothing.
    dataset_pooling_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        # The database refuses a value the code has never heard of, so a hand-written UPDATE
        # cannot invent a fourth mode. NULL passes, which is what "no override" is.
        CheckConstraint(
            "send_mode IS NULL OR send_mode IN ('disabled','allowlist','live')",
            name="ck_orgs_send_mode",
        ),
    )


class SendAllowlistEntry(Base):
    """An address this org may send to while `send_mode` is `allowlist`.

    **It used to be `SEND_ALLOWLIST` in the environment**, one list for the whole deployment.
    That was right while there was one user and wrong the moment there were ten: everybody
    rehearsing shared one list that only somebody with shell access could edit, so a new
    workspace could select `allowlist` and then discover there was no way to put their own
    address on it. Worse, they could put mail into *the operator's* rehearsal list, and read the
    operator's addresses out of their own settings screen.

    So the list is the org's, like `send_mode` itself (ADR-010). The **decision** has not moved:
    `allowlist_permits()` in `smtp.py` is still the only function that compares a recipient
    against anything, and the structural test over the whole tree still asserts it. What changed
    is where the values come from -- exactly the trade ADR-010 made for the mode.

    An entry is **not** a suppression's opposite. Adding one widens who may be emailed, which is
    why it is an `admin` action and why `live` remains `owner`. Removing one is always safe.
    """

    __tablename__ = "send_allowlist"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Lower-cased at the door. `allowlist_permits` compares case-insensitively, and a list whose
    #: entries do not match the comparison is a list that silently permits nobody.
    email_norm: Mapped[str] = mapped_column(String(320), nullable=False)
    added_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (UniqueConstraint("org_id", "email_norm", name="uq_send_allowlist_org_email"),)


class Invitation(Base):
    """A place held in an org for somebody who has not signed in yet.

    **This exists because order mattered and should not have.** Adding a teammate meant inserting
    a `members` row by hand, and that row had to exist *before* their first authenticated
    request -- because `provision()` sees no membership, creates them a workspace of their own,
    and makes them its owner. Do it in the other order and the symptom is not an error: they land
    in an empty workspace and report that every campaign has vanished, and the "fix" of inserting
    the row afterwards leaves them with **two** memberships, which `deps.current_user` resolves by
    taking whichever the database returns first. `uq_members_org_user` is scoped per org, so
    nothing refuses the second row.

    An invitation is the same intent expressed before the fact: when that address signs in for the
    first time, `provision()` finds this row and joins them to **this** org with **this** role
    instead of making a new one. Nothing has to happen in a particular order, and nothing depends
    on somebody remembering which order it was.

    **Matched on the email address, which is a deliberate, stated limit.** We cannot know a
    person's Supabase `sub` before they have ever signed in -- that is the identifier the identity
    provider mints for them -- so the address Google or the magic link reports is the only thing
    available to match on. That means whoever controls the invited address gets the seat, which is
    the same assumption every invitation email in existence makes. It is not a bearer token and it
    is not secret: a pending invitation grants nothing until somebody authenticates as that
    address through Supabase.
    """

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Lower-cased at the door, because an invitation to `Asha@X` that does not match a sign-in
    #: as `asha@x` is an invitation that silently does nothing.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    #: Who sent it, for the audit trail. Their address, not their `sub`: this is read by people.
    invited_by: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = _now_col(nullable=False)
    #: Set when it is used. Kept rather than deleted: "who let them in, and when?" is a question
    #: worth being able to answer, and a deleted row answers nothing.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_invitations_role"),
        Index("ix_invitations_email", "email"),
        Index("ix_invitations_org", "org_id"),
        # **One pending invitation per address, across the whole deployment.** Per org would let
        # two workspaces invite the same person and decide the winner by whoever signs in first;
        # refusing the second is a sentence somebody can act on, losing a race is not.
        #
        # Partial, so a used or revoked row never blocks a fresh invitation -- re-inviting
        # somebody who left has to work.
        Index(
            "uq_invitations_pending_email",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )


class Member(Base):
    """Who may act for an org, and in what capacity (§14).

    `user_id` is Supabase's `sub` claim, kept as text rather than a foreign key: the identity
    provider owns that table and we never want a join across the boundary. The row is the only
    thing that grants access — a token proves who someone is, this proves what they may do here.

    Public sign-ups are disabled, so a token with no row gets no org and therefore no data. That
    is a deliberate default: an authenticated stranger is still a stranger.
    """

    __tablename__ = "members"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Supabase `sub`. Opaque to us.
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    #: owner | admin | member. Only `owner` is issued today; the others exist so adding them
    #: later is a row, not a migration.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="owner")
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_members_org_user"),
        CheckConstraint("role IN ('owner', 'admin', 'member')", name="ck_members_role"),
    )


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
    #: Who writes the email. `research` researches the company and asks a model for a grounded
    #: draft; `template` renders the operator's own text with merge fields and calls no model
    #: for a first email or a follow-up. Replies use a model in both modes -- a reply has to
    #: answer what the person actually said. See `app/domain/template.py`.
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default="research")
    #: What this campaign is *for*: `outreach` (an agency selling) or `job_hunt` (a candidate
    #: looking for work). **Orthogonal to `mode`**, which decides who writes the email -- four
    #: combinations, one pipeline. See `app/domain/kinds.py` and `docs/CAMPAIGN_KINDS.md`.
    #:
    #: The kind also decides which schema validates `persona`: today's `Persona` for outreach, a
    #: `CandidateProfile` for a job hunt, which compiles into the same `Persona` so that nothing
    #: downstream has to know either exists.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="outreach")
    persona: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: `CampaignTemplates` for a `template`-mode campaign; `{}` for a `research` one.
    templates: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The window this campaign may send in, as calendar instants. Both nullable: a campaign
    #: with neither behaves exactly as every campaign did before they existed. The `Scheduler`
    #: moves `status` when either boundary passes, rather than every reader of `status` learning
    #: to check the clock -- see `app/domain/campaign_schedule.py`.
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','scheduled','running','paused','completed','archived')",
            name="ck_campaigns_status",
        ),
        CheckConstraint("mode IN ('research','template')", name="ck_campaigns_mode"),
        CheckConstraint("kind IN ('outreach','job_hunt')", name="ck_campaigns_kind"),
        CheckConstraint(
            "starts_at IS NULL OR ends_at IS NULL OR ends_at > starts_at",
            name="ck_campaigns_schedule_order",
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
    #: Which experiment arm this lead is in, assigned once when its first draft is written and
    #: never recomputed (see `Experiment`). NULL means no experiment was running then, which is
    #: every lead in the live database and stays a perfectly sendable lead.
    variant: Mapped[str | None] = mapped_column(String(40))
    #: Every extra sheet column, kept so research and drafting can use it (issue A5).
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Which upload this lead arrived in, so the wrong sheet can be undone. NULL for anything
    #: imported before batches existed: there is no record of which upload those came from, and
    #: inventing one would make an undo delete rows it was not asked about.
    import_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

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
    #: **The sticky sender** (F7, ADR-044). NULL until the first send, then never changed: a
    #: follow-up has to come from the address that sent the first email, or the recipient sees
    #: one conversation arriving from two strangers and the thread breaks in their client.
    #: Written by `outbox.claim_send` under the lead's row lock, which is the only place that
    #: can make the assignment and the outbox row land together.
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "email_norm", name="uq_leads_campaign_email"),
        Index("ix_leads_org_state", "org_id", "state"),
        Index(
            "ix_leads_import_batch",
            "import_batch_id",
            postgresql_where=text("import_batch_id IS NOT NULL"),
        ),
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
    #: Whether this particular email carries the campaign's attachment. NULL means nobody has
    #: expressed an opinion, so the campaign's setting decides; `False` is a reviewer who
    #: unticked the box, and that has to survive. See `app/domain/attachment.should_attach`.
    attach: Mapped[bool | None] = mapped_column(Boolean)
    #: The draft this one was edited from, when a reviewer approved with changes.
    #:
    #: Versions alone cannot express this: a regeneration after a rejection also makes a new
    #: version, and that is a *different* event from a human rewriting the model's words. The
    #: pair (AI draft, human edit) is the preference data this dataset is most interested in, so
    #: the link is explicit rather than inferred from adjacent version numbers.
    derived_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.drafts.id", ondelete="SET NULL")
    )
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
    #: Nullable because inbound mail that matches no lead is still stored. §10.2 is explicit
    #: that unmatched mail is never deleted and never marked read — the alternative is silently
    #: losing somebody's reply. An outbound message always has one; see the check constraint.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="CASCADE")
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
    #: Which A/B arm this send used, or NULL when the campaign has no experiment running.
    variant: Mapped[str | None] = mapped_column(String(40))
    #: Which day of the mailbox's warm-up ramp this went out on. Stored rather than derived:
    #: `warmup_started_on` can be reset, so the same `sent_at` would compute a different answer
    #: later. Weekday and hour are *not* stored -- those are derivable from `sent_at` and the
    #: campaign's timezone, and a denormalised copy is one that can disagree.
    warmup_day: Mapped[int | None] = mapped_column(Integer)
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
        # Only inbound mail may be orphaned. We never send to nobody.
        CheckConstraint(
            "direction = 'in' OR lead_id IS NOT NULL",
            name="ck_messages_out_has_lead",
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
        # Enqueueing is idempotent: a live job already holding this (org_id, kind, dedupe_key)
        # blocks a duplicate, while a finished one does not, so the same work can be queued again
        # later.
        #
        # **`org_id` is part of the key (ADR-032), and leaving it out was a cross-tenant bug.**
        # Without it a live job in one workspace refused the same key in every other workspace,
        # and `enqueue` reports a refusal as `None`, which every caller reads as "already
        # queued, nothing to do" — so one workspace's work silently did not happen. Latent only
        # because every key in the tree is built from a UUID; a key built from a shared
        # identifier (`feed:greenhouse:{token}`, `company:{domain}`) collides immediately.
        #
        # Declared here as well as created by migration. It must be in the metadata, or
        # autogenerate reads it as drift and writes a migration that DROPS it — which it did
        # once, silently removing the guarantee until the queue tests caught it.
        Index(
            "uq_jobs_org_kind_dedupe_live",
            "org_id",
            "kind",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
    )


class LlmUsage(Base):
    """One LLM call, successful or not.

    Failures are recorded too: a provider that fails often is exactly what the admin view needs
    to show, and §20's 9-12k tokens-per-lead estimate was always meant to be replaced by
    measurement. The prototype had no accounting at all, so when its daily quota ran out there
    was no record of why or of how close it had been (issue E5).
    """

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The chain entry's name, not the provider: two models from one provider are separate pools
    #: of headroom, and that is the level caps apply at.
    entry_name: Mapped[str] = mapped_column(String(60), nullable=False)
    task: Mapped[str] = mapped_column(String(40), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        # The headroom query: tokens for one entry since a cutoff, run before every call.
        Index("ix_llm_usage_entry_at", "entry_name", "at"),
        Index("ix_llm_usage_org_at", "org_id", "at"),
        Index("ix_llm_usage_lead", "lead_id"),
    )


class DomainResearch(Base):
    """Research cached per company domain, not per lead.

    A sheet routinely contains several founders at one company, and their company page is the
    same page. Fetching it once per lead would be rude to the server and would multiply the
    token cost of the most expensive step. §12 caches for 30 days.
    """

    __tablename__ = "domain_research"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    findings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Raw fetched text, purged after 30 days by the retention job (§19). Never whole HTML: the
    #: free tier is 500 MB and raw markup is almost all of the bytes and none of the value.
    evidence_text: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    llm_entry: Mapped[str | None] = mapped_column(String(60))
    fetched_at: Mapped[datetime] = _now_col(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "domain", name="uq_domain_research_org_domain"),
        Index("ix_domain_research_expiry", "expires_at"),
    )


class Research(Base):
    """Per-lead research: the shared domain findings plus anything specific to this person."""

    __tablename__ = "research"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="CASCADE"), nullable=False
    )
    domain_research_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.domain_research.id", ondelete="SET NULL")
    )
    #: Job title, operator notes and extra sheet columns. LinkedIn is never fetched (rule 4).
    person_notes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Hooks that survived the deterministic snippet check, with their citations.
    hooks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: Hooks the check dropped, kept so a weak model is visible rather than merely quiet.
    rejected: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    thin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("lead_id", name="uq_research_lead"),
        Index("ix_research_org", "org_id"),
    )


class SearchUsage(Base):
    """Search credits spent, tracked against a monthly budget.

    Tavily's free plan is 1,000 credits a month, so this is what stops a large import quietly
    spending the lot and what shows credits-left in the UI (§12).
    """

    __tablename__ = "search_usage"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    query_domain: Mapped[str | None] = mapped_column(String(253))
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (Index("ix_search_usage_org_at", "org_id", "at"),)


#: How long one search answer may be reused. A week, and the number is doing a different job
#: from `research.service.CACHE_DAYS`: that one governs findings verified against page text we
#: fetched, this one governs *somebody else's* index of what a company has been up to lately,
#: which is the most perishable thing in this system. `PERISHABLE_AFTER_DAYS` already drops the
#: hooks that cite it after thirty days; a week keeps the cache from serving news that has moved
#: on while still covering the case this exists for -- a second lead at the same company, or a
#: re-research after the ninety-day findings cache expires.
SEARCH_CACHE_DAYS = 7


class SearchCache(Base):
    """One search answer, reused so a repeat question costs nobody a credit (F17, ADR-046).

    **This is the half of F17 that saves money.** The chain stopping at the first provider that
    answers decides *which* key is spent; the cache decides whether any key is spent at all, and
    a cache hit calls no provider and records no usage.

    **Per workspace, like `domain_research` and unlike `company_profiles`.** ADR-038 made the
    public-company cache the single table with no `org_id`, and drew the line precisely: a name
    and an ATS slug are public facts, while findings a draft goes on to cite were paid for by one
    workspace out of its own credits and are not ours to pool. A search answer is on the second
    side of that line -- it is a provider's billable response to a question one workspace asked.

    **A confirmed empty is cached too, and that is deliberate.** A provider that answered and
    found nothing is an answer: caching it stops the next lead at the same company paying for the
    same silence. What is **never** cached is a chain where every member failed -- that is the
    absence of an answer, and storing it would let one provider outage keep a workspace from
    searching for a week. The two are told apart by `provider`, which names who answered.
    """

    __tablename__ = "search_cache"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The normalised query. Not a domain: two companies can share a domain in nobody's index,
    #: and the question we asked is what the answer is an answer to.
    query_norm: Mapped[str] = mapped_column(String(300), nullable=False)
    #: Which member of the chain answered. One of `SEARCH_PROVIDERS`, widened to 40 characters
    #: like `search_usage.provider` so a provider added later needs no migration.
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The hits as stored: title, url and snippet, nothing else. No lead id and no person -- a
    #: search is asked about a company, and a row keyed per workspace per query has no business
    #: recording which lead happened to prompt it.
    hits: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    fetched_at: Mapped[datetime] = _now_col(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "query_norm", name="uq_search_cache_org_query"),
        Index("ix_search_cache_expires", "expires_at"),
    )


class ImapCursor(Base):
    r"""Where the poller got to in one folder.

    `UIDVALIDITY` plus the last UID seen, per §10.1. The prototype searched `UNSEEN` and fetched
    with `RFC822`, which implicitly set the `\Seen` flag — so a reply arriving while the lead
    was not in the one state it watched was discarded *and* marked read, invisible forever. It
    also marked unrelated mail in the operator's own inbox as read (issue B10).

    A cursor means we never depend on flags at all: we ask for UIDs above the last one handled.
    If the server resets `UIDVALIDITY`, every UID is meaningless and a bounded rescan is needed.
    """

    __tablename__ = "imap_cursors"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="CASCADE"),
        nullable=False,
    )
    folder: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The server's generation marker. A change invalidates every stored UID.
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Consecutive failures, so an alert can fire after three (§18) rather than on the first.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("mailbox_id", "folder", name="uq_imap_cursors_mailbox_folder"),
    )


class Classification(Base):
    """What we decided a reply was, and why.

    Persisted **before** anything acts on it (§10.3). The prototype classified inside the
    handler, so a crash or retry re-ran the model and could reach a different answer the second
    time — and there was no record of the first (issue B17).

    The reasons are stored because "why did we suppress this address?" has to be answerable
    months later without re-running a model whose weights may have changed.
    """

    __tablename__ = "classifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    label: Mapped[str] = mapped_column(String(30), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Why this label, including which rule overrode the model where one did.
    rationale: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    llm_entry: Mapped[str | None] = mapped_column(String(60))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    #: What the system did as a result. Never "sent" — see app/domain/reply.py.
    action: Mapped[str | None] = mapped_column(String(20))
    acted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("message_id", name="uq_classifications_message"),
        Index("ix_classifications_lead", "lead_id", "created_at"),
    )


#: Why an address is suppressed. **The database's check constraint is built from this tuple**, so
#: a caller reading it cannot pass a value the database will refuse — which is exactly what
#: happened when the one-click unsubscribe route invented its own wording.
#:
#: The vocabulary is deliberately short and it is not where detail belongs. "They clicked the
#: one-click header" and "they replied with the word unsubscribe" are both `opt_out`; which
#: mechanism it was goes in the append-only event, where there is room for it and where
#: "why is this address suppressed?" is actually answered months later.
SUPPRESSION_REASONS: tuple[str, ...] = ("opt_out", "bounce", "manual", "complaint")


class Suppression(Base):
    """Addresses and domains we must never contact. Checked at import and before every send."""

    __tablename__ = "suppressions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: email | domain | email_hash. `email_hash` holds a SHA-256 digest instead of the address
    #: and is written by erasure (§17): the person must stay unreachable after we forget who
    #: they were. See `app/services/privacy/erasure.py`.
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(20), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.messages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "kind", "value", name="uq_suppressions_org_kind_value"),
        CheckConstraint("kind IN ('email','domain','email_hash')", name="ck_suppressions_kind"),
        CheckConstraint(
            # Built from `SUPPRESSION_REASONS` rather than spelled out again. A literal that has
            # to match a database constraint, copied into a caller that cannot see it change, is
            # this project's most repeated defect -- and it caught me here: the one-click
            # unsubscribe route passed `reason="one-click unsubscribe"`, which this constraint
            # refused with a 500 that said nothing about why.
            f"reason IN ({', '.join(repr(reason) for reason in SUPPRESSION_REASONS)})",
            name="ck_suppressions_reason",
        ),
    )


class Capture(Base):
    """A page the user deliberately sent from the extension (F8).

    Created **only** by a button press, never by the extension watching a tab. Kept for days
    rather than months and listed in `PRIVACY.md`: it is somebody's browsing, narrowed to the
    one page they chose to share.
    """

    __tablename__ = "captures"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Which token sent it, so a device can be revoked and its captures identified.
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.api_tokens.id", ondelete="SET NULL")
    )
    page_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    page_text: Mapped[str | None] = mapped_column(Text)
    #: form | posting
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    captured_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint("kind IN ('form','posting')", name="ck_captures_kind"),
        Index("ix_captures_org_at", "org_id", "captured_at"),
    )


class Suggestion(Base):
    """One proposed answer to one form field (F8).

    `state` records what the person did with it, which is the preference signal the dataset wants
    (ADR-026) and the only reason to store a suggestion at all after it has been shown.
    """

    __tablename__ = "suggestions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    capture_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.captures.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(300), nullable=False)
    proposed_answer: Mapped[str | None] = mapped_column(Text)
    fact_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    #: proposed | accepted | edited | rejected | needs_user
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "state IN ('proposed','accepted','edited','rejected','needs_user')",
            name="ck_suggestions_state",
        ),
    )


class CompanyProfile(Base):
    """Public facts about a company, shared across every workspace (F19, ADR-038).

    **This is the one table in the schema with no `org_id`, and that is a deliberate exception to
    ADR-004.** Researching the same company separately for every workspace wastes search credits
    on facts that are identical and public either way: a careers page is a careers page.

    What makes the exception safe is what the table is allowed to hold: a name, a careers URL, an
    ATS type and slug, a size band, a short summary, and the source URL and fetch time behind
    each. **No lead, no contact, no message, no workspace note.** A structural test asserts that
    by column name, and the reason it is a test rather than a convention is that the first person
    to add `notes` here would be leaking one workspace's thinking into another's.

    Anything a workspace knows *about* a company -- why it matters to them, who they spoke to --
    stays in their own org-scoped tables.
    """

    __tablename__ = "company_profiles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: The registrable domain, which is also the research cache key (ADR-011).
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    name: Mapped[str | None] = mapped_column(String(300))
    careers_url: Mapped[str | None] = mapped_column(String(2000))
    #: greenhouse | lever | ashby, when we can tell from the careers page.
    ats_type: Mapped[str | None] = mapped_column(String(20))
    ats_slug: Mapped[str | None] = mapped_column(String(200))
    #: A band rather than a headcount: a precise number is wrong within a month.
    size_band: Mapped[str | None] = mapped_column(String(20))
    summary: Mapped[str | None] = mapped_column(Text)
    #: Where each field came from, keyed by field name. Provenance is not optional here: a
    #: shared fact with no source is a rumour every workspace then repeats.
    sources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Hash of the fetched page, so a refresh past the age limit does no work when nothing
    #: changed.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = _now_col(nullable=False)
    #: Bumped on every real change, so a consumer can tell whether its copy is stale.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (UniqueConstraint("domain", name="uq_company_profiles_domain"),)


class ProfileFact(Base):
    """One thing a candidate can truthfully say about themselves (F6).

    `CandidateProfile.facts` already held these inside `campaigns.persona`, which is right for
    one campaign and wrong for a person: a candidate applies to many roles with one history.
    These are the workspace's own, compiled into a persona per campaign.

    **A fact is unusable until confirmed.** Facts mined from GitHub or a résumé arrive as
    proposals, and `confirmed_at` is what the claim check will resolve against -- because the
    person who gets asked about a claim in an interview is the person who has to have agreed to
    it. That is the no-invention rule (hard rule 6) applied to the one case where inventing is
    most tempting and most costly.
    """

    __tablename__ = "profile_facts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: `pp1`, `pp2`... The same ids `resolve_evidence` already understands, so a draft citing a
    #: fact needs no new machinery.
    fact_id: Mapped[str] = mapped_column(String(20), nullable=False)
    #: skill | project | role | education
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(2000))
    #: Where it came from: `user` for typed, `github` or `resume` for mined.
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    #: NULL means proposed and not yet usable.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "fact_id", name="uq_profile_facts_org_fact"),
        CheckConstraint(
            "kind IN ('skill','project','role','education')", name="ck_profile_facts_kind"
        ),
        CheckConstraint("source IN ('user','github','resume')", name="ck_profile_facts_source"),
    )


class AnswerBankEntry(Base):
    """An answer to a question application forms keep asking (F6).

    Notice period, expected pay, work authorisation, links. Answered once by the person, with the
    facts it rests on, and **only an approved row is ever offered** -- to the extension or to
    anything else. Anything not in the bank is reported as needing the user rather than guessed,
    which is the direct answer to tools that invent screening answers.
    """

    __tablename__ = "answer_bank"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Lower-cased words to match a form label against. Not a regex: a pattern a user types is a
    #: pattern a user must be able to read back.
    question_pattern: Mapped[str] = mapped_column(String(300), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    fact_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "question_pattern", name="uq_answer_bank_org_pattern"),
    )


class WatchedBoard(Base):
    """A public ATS board this workspace wants polled daily (F5).

    The slug is the board's own, taken from its URL. Two workspaces may watch the same board,
    which is why job deduplication had to be scoped by org first (ADR-032): the poll's dedupe
    key is built from the source and the slug, which they share.
    """

    __tablename__ = "watched_boards"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    #: What to call it on screen. The slug is rarely the company's name.
    label: Mapped[str | None] = mapped_column(String(200))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why the last poll failed, if it did. Shown so a wrong slug is fixable rather than silent.
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "source", "slug", name="uq_watched_boards_org_source_slug"),
        CheckConstraint(
            "source IN ('greenhouse','lever','ashby')", name="ck_watched_boards_source"
        ),
    )


class JobPost(Base):
    """A job posting a candidate might pursue (F5).

    From a public ATS feed or a URL the user pasted. `body_text` is cleared by the retention
    sweep like researched page text, and is nullable from the start because Greenhouse's list
    response carries no description at all (`docs/ATS_FEEDS.md`).
    """

    __tablename__ = "job_posts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: greenhouse | lever | ashby | pasted
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    #: The board's own id, or a hash of the URL for a pasted one.
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    #: **The company's own domain, or NULL.** It is NULL for every posting that arrived through
    #: a feed, because a feed's `apply_url` names the applicant tracking system and not the
    #: employer -- `job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`. It held
    #: those hosts until ADR-047, and three callers acted on them: a contact lookup searched the
    #: ATS vendor on the user's own credit, an imported lead was given the vendor as its
    #: `company_url` and researched, and the referral matcher looked for connections at
    #: "boards". NULL makes a caller ask; a vendor host makes it confident and wrong.
    company_domain: Mapped[str | None] = mapped_column(String(253))
    #: The company's identifier on the board (`vercel`, `leverdemo`, `ramp`), read from the
    #: first path segment of the apply URL. This is what a feed genuinely tells us about the
    #: employer, and it is the key `watched_boards` already uses, so it is also how a domain is
    #: resolved: `company_profiles` records `ats_type` and `ats_slug` per domain, which is this
    #: lookup in reverse.
    board_slug: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str | None] = mapped_column(String(500))
    apply_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    body_text: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = _now_col(nullable=False)
    #: open | archived. A posting is never deleted on disappearing from a feed: a candidate may
    #: have applied to it, and `applications` will point at it.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "source", "external_id", name="uq_job_posts_org_source_ext"),
        CheckConstraint(
            "source IN ('greenhouse','lever','ashby','pasted')", name="ck_job_posts_source"
        ),
        CheckConstraint("status IN ('open','archived')", name="ck_job_posts_status"),
        Index("ix_job_posts_org_fetched", "org_id", "fetched_at"),
    )


#: What kind of thing happened at a watched company. **Only `hiring` has a producer today** --
#: the daily ATS poll, which already runs (F5) -- and the rest are named because a check
#: constraint is where a vocabulary belongs and because a producer added later should not need a
#: migration. That is the honesty `mailbox_health.complaint_rate` already keeps: a column with no
#: writer is fine when it says so, and one holding a zero that means "we cannot see this" is not.
SIGNAL_KINDS: tuple[str, ...] = ("hiring", "news", "launch", "funding")

#: What has been done about a signal. `new` until somebody looks; `acted` once they have used it;
#: `dismissed` when they decided it was not worth an email.
#:
#: **There is no `sent` state, deliberately.** A signal is a reason to write, not a record of
#: writing: what was sent lives in `messages` and where a person stands lives in `leads`. A third
#: copy of that would be a third thing to keep honest, and the funnel already has one authority.
SIGNAL_STATES: tuple[str, ...] = ("new", "acted", "dismissed")


class WatchlistEntry(Base):
    """A company this workspace wants watched for a reason to write today (F11, ADR-049).

    **A watchlist entry is a company; `watched_boards` is a board.** That table is the job-hunt
    side -- an ATS feed polled for postings somebody might apply to. This is the outreach side: a
    company whose hiring would be a reason to write to *them*. They share the poll, which is the
    point: one fetch of a board can serve a candidate looking for a job and an agency looking for
    a client.

    The key is the **registrable domain**, the same key `domain_research` uses and for ADR-011's
    reason: it is the company's own identity, where `leads.domain` is whatever address a founder
    happens to write from and is routinely `gmail.com`.

    **`ats_source` and `ats_slug` are on the row rather than looked up every time.** A posting
    from a feed carries the board's slug and no company domain at all (ADR-047), so matching a
    posting to a watch through `company_profiles` would mean a watch only works once some
    workspace has researched that company. Storing the slug makes the match direct and lets a
    person say "watch Acme's Greenhouse board" in one step. The domain stays the identity; the
    slug is how the signal arrives.
    """

    __tablename__ = "watchlist"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The registrable domain. One row per company per workspace.
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    #: What to call it on screen. A domain is a true fact about a company and a poor label.
    label: Mapped[str | None] = mapped_column(String(300))
    #: Which board to read this company's hiring from, when it has one we can poll.
    ats_source: Mapped[str | None] = mapped_column(String(20))
    ats_slug: Mapped[str | None] = mapped_column(String(200))
    #: Which campaign a signal here should be acted on through, when somebody has chosen one.
    #: Nullable and `SET NULL`: deleting a campaign must not delete the watch, and a company
    #: worth watching before any campaign exists is the ordinary case.
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="SET NULL")
    )
    #: A careers page to watch by its hash, for a company with no ATS feed (F22, ADR-054).
    #: **On this row rather than in a table of its own**, because `signals.intake.record` takes
    #: a watchlist entry: it is what carries the pause, the domain and the lead match, and a
    #: second table would have needed its own copy of all three and produced two answers to
    #: "which companies do we watch".
    careers_url: Mapped[str | None] = mapped_column(String(2000))
    #: SHA-256 of that page's visible text. **The hash and never the text**: storing the page
    #: would make this a crawler with a cache -- somebody else's copy of their own site, in our
    #: database, for every company any workspace watches -- and `PRIVACY.md` could no longer say
    #: we keep no page text. NULL until the first successful fetch, which is why the first fetch
    #: never reports a change: different from nothing is not a change.
    page_hash: Mapped[str | None] = mapped_column(String(64))
    page_length: Mapped[int | None] = mapped_column(Integer)
    page_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    page_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Our own sentence about why this page is not being read -- set when the site's `robots.txt`
    #: asks us not to. Stored rather than the row being deleted, so the screen says why nothing
    #: arrives instead of showing a watch that silently never updates. Never a response body.
    page_refused_reason: Mapped[str | None] = mapped_column(String(300))
    #: Stop producing signals without forgetting the company or its history.
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: When a signal was last recorded here, so a screen can tell "nothing has happened" from
    #: "this was never wired up" -- the job `booking_webhooks.last_used_at` does for the webhook.
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "domain", name="uq_watchlist_org_domain"),
        CheckConstraint(
            "ats_source IS NULL OR ats_source IN ('greenhouse','lever','ashby')",
            name="ck_watchlist_ats_source",
        ),
        Index("ix_watchlist_org", "org_id"),
        Index("ix_watchlist_board", "org_id", "ats_source", "ats_slug"),
    )


class SignalEvent(Base):
    """Something that happened at a watched company (F11, ADR-049).

    **A signal proposes; it never creates a lead.** That is the rule this table exists under, and
    it is `contact_candidates`' rule: the send loop reads `leads`, and nothing here is in that
    table. A company hiring is a reason to write to somebody there -- it is not consent, not an
    address and not a person. Acting on one means writing to a lead the workspace already has, or
    pressing the F9 lookup button and spending their own credit.

    **Nothing here is drafted or sent automatically.** `approval_mode = review_all` already means
    a person approves every first email, and a signal does not get to be an exception. ADR-034's
    rule applies in spirit: a thing that acts on its own may only ever *restrict*, and this one
    only ever adds a row to a list somebody reads.

    The unique key `(org_id, kind, source, external_id)` is what makes a re-poll harmless -- the
    same device as `booking_events`, and needed for the same reason: the daily poll sees
    yesterday's postings again every time it runs.
    """

    __tablename__ = "signal_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The watch this came from. `SET NULL` rather than `CASCADE`: forgetting a company must not
    #: delete the record that it was hiring in March, which is what keeps a funnel auditable.
    watchlist_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.watchlist.id", ondelete="SET NULL")
    )
    #: Denormalised from the watch on purpose, because the watch may be deleted and the signal
    #: still has to say which company it was about.
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    #: One of `SIGNAL_KINDS`.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Where it came from: `greenhouse`, `lever` or `ashby` for a posting today.
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The source's own id for the thing that happened -- a posting id, so the same posting seen
    #: again tomorrow is the same signal rather than a second one.
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2000))
    #: The posting this came from, when it came from one. `SET NULL`, because a posting may be
    #: archived and the signal is still a true thing that happened.
    job_post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="SET NULL")
    )
    #: The lead this workspace already has at that company, when there is one. **Matched, never
    #: created**: a signal with no lead is the interesting case rather than the error case, and
    #: it is what the F9 lookup button is for.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    #: One of `SIGNAL_STATES`.
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    #: When the thing happened, as the source reported it -- distinct from `created_at`, which is
    #: when we noticed. A poll that has been down for a week must not make a week-old posting
    #: look like today's news.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "kind", "source", "external_id", name="uq_signal_events_org_kind_ext"
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{name}'" for name in SIGNAL_KINDS) + ")",
            name="ck_signal_events_kind",
        ),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{name}'" for name in SIGNAL_STATES) + ")",
            name="ck_signal_events_state",
        ),
        Index("ix_signal_events_org_created", "org_id", "created_at"),
        Index("ix_signal_events_org_state", "org_id", "state"),
    )


#: What a node in the graph is. Deliberately few: every kind here is something a sentence in a
#: meeting brief can be *about*, and a kind nobody could cite is a kind that makes the graph
#: bigger without making it more useful.
ENTITY_KINDS: tuple[str, ...] = ("company", "person", "role", "technology")

#: What one entity can be said to be to another. The vocabulary is small on purpose: an edge
#: exists to be read back as a sentence with a source beside it, and a predicate nobody can
#: phrase is a predicate nobody will trust.
EDGE_PREDICATES: tuple[str, ...] = (
    "hiring_for",
    "works_at",
    "uses",
    "mentioned",
)


class Entity(Base):
    """A thing this workspace knows about (F12, ADR-051).

    Per workspace, like `domain_research` and unlike `company_profiles` (ADR-038). A company's
    name and ATS slug are public facts worth pooling; *what one workspace has worked out about a
    company, and from which of its own sources*, is not -- and an entity exists to hang those
    edges on.

    `norm_key` is what makes "Acme", "Acme Inc" and "acme.example" one node. It is computed once
    by `graph.key_for` and stored, rather than recomputed per query, so a change to the
    normalisation cannot silently split a node in half between old rows and new ones.
    """

    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: One of `ENTITY_KINDS`.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: As it should be shown: the company's own capitalisation, the role's own title.
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The identity. Two rows with one key are one thing.
    norm_key: Mapped[str] = mapped_column(String(300), nullable=False)
    #: For a company, its registrable domain -- the key `domain_research` and the watchlist use,
    #: so the three agree on what a company is (ADR-011).
    domain: Mapped[str | None] = mapped_column(String(253))
    #: For a person we are already talking to. `SET NULL`, because erasing a lead must not
    #: delete what we learned about their employer.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("org_id", "kind", "norm_key", name="uq_entities_org_kind_key"),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{name}'" for name in ENTITY_KINDS) + ")",
            name="ck_entities_kind",
        ),
        Index("ix_entities_org_domain", "org_id", "domain"),
    )


class Edge(Base):
    """One claim about one entity, **with where it came from** (F12, ADR-051).

    **An edge with no provenance is refused by the database.** `ck_edges_provenance` requires a
    `source_url` or a `message_id`, so "we know Acme is hiring" cannot be stored without the
    thing that says so. That is a constraint rather than a convention because the convention is
    the part that fails: the brief these edges feed is a set of sentences somebody will repeat
    to a stranger, and a sentence whose source nobody can produce is the thing this whole system
    exists not to say. Hard rule 6 forbids inventing facts about the sender; this is the same
    rule pointed at the recipient, enforced one layer lower.

    **A model may phrase an edge and may never add one.** Nothing writes here from a model's
    output: the producers are a job posting (whose URL is the source) and an inbound message
    (whose id is). S15.3's brief reads these rows and may reword them; it cannot introduce a
    claim, because there would be nothing to put in these columns.

    `object_id` or `object_text`, never both and never neither: an edge either points at another
    node or states a value, and a row that did both would be two claims wearing one id.
    """

    __tablename__ = "edges"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: One of `EDGE_PREDICATES`.
    predicate: Mapped[str] = mapped_column(String(30), nullable=False)
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.entities.id", ondelete="CASCADE")
    )
    object_text: Mapped[str | None] = mapped_column(String(500))
    #: **Provenance.** One of these two is required by `ck_edges_provenance`.
    source_url: Mapped[str | None] = mapped_column(String(2000))
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.messages.id", ondelete="CASCADE")
    )
    #: When the thing was true, as the source reported it -- not when we noticed. A brief citing
    #: a six-month-old posting as "they are hiring" is the perishable-hook problem again
    #: (ADR-011), and this column is what lets the brief decide.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        # The slice's whole point, and the reason it is here rather than in a service: a service
        # can be bypassed by the next writer, and this cannot.
        CheckConstraint(
            "source_url IS NOT NULL OR message_id IS NOT NULL", name="ck_edges_provenance"
        ),
        CheckConstraint(
            "(object_id IS NOT NULL AND object_text IS NULL) "
            "OR (object_id IS NULL AND object_text IS NOT NULL)",
            name="ck_edges_object",
        ),
        CheckConstraint(
            "predicate IN (" + ", ".join(f"'{name}'" for name in EDGE_PREDICATES) + ")",
            name="ck_edges_predicate",
        ),
        # One claim from one source is one edge. A re-poll that saw the same posting again must
        # not add a second "hiring for Backend Engineer", which is the signal table's lesson.
        #
        # **`NULLS NOT DISTINCT`, because the nullable columns are the point.** Three of the six
        # are null in any given row -- an edge names an object *or* a text, and carries a URL
        # *or* a message id -- and under the default rule two NULLs never collide, so the index
        # would have deduplicated nothing at all. Postgres 15+; this project runs 17 in
        # development and in production, checked rather than assumed.
        Index(
            "uq_edges_claim",
            "org_id",
            "subject_id",
            "predicate",
            "object_id",
            "object_text",
            "source_url",
            "message_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_edges_org_subject", "org_id", "subject_id"),
    )


class MailboxHealth(Base):
    """One row per mailbox per day: how much it sent and how much came back (ADR-034).

    **A rollup rather than counters incremented at send time.** Counters drift — a crash between
    the send and the increment leaves them wrong for ever, and nothing would notice — while a
    rollup is recomputed from `messages` and `classifications`, which are the record. The cost is
    that the numbers lag by one pass of the `Scheduler`, which for a signal measured over days is
    not a cost at all.

    `complaint_rate` is **nullable and is never populated today**, which is deliberate rather
    than unfinished. A complaint rate needs a feed from the receiving provider; Postmaster Tools
    is a link we ask the owner to open, not an integration (ADR-029), so there is no honest
    number to put here. A column holding zero would read as "no complaints" when it means "we
    cannot see complaints", and the whole value of this table is that an operator can trust it.
    """

    __tablename__ = "mailbox_health"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    #: The IST day the counts belong to. A `date`, not a timestamp: the send window and the daily
    #: cap are both IST days, so health measured in UTC days would straddle them.
    day: Mapped[date] = mapped_column(Date, nullable=False)
    sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bounced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: See the class docstring: deliberately never written.
    complaint_rate: Mapped[float | None] = mapped_column(Float)
    #: Where the numbers came from, so a future provider feed is distinguishable from our own
    #: counting without guessing at the shape of the data.
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="messages")
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("mailbox_id", "day", name="uq_mailbox_health_mailbox_day"),
        CheckConstraint("sent >= 0 AND bounced >= 0", name="ck_mailbox_health_counts"),
        Index("ix_mailbox_health_org_day", "org_id", "day"),
    )


class WorkerHeartbeat(Base):
    """One row per worker process, rewritten every minute (§5, §18).

    This is the one piece of state the api cannot get from memory. `app.worker.health` holds the
    worker's self-knowledge in process — correctly, because a process that has died cannot write
    a row saying so — but `/ready` runs in a *different* process and has no way to see it. The
    signal it needs is not the row's presence, it is the row's **age**: a worker that has stopped
    stops refreshing `last_seen_at`, and staleness is what readiness fails on. A ready api with a
    dead worker looks perfectly healthy and sends nothing, which is the failure this exists to
    make visible.

    No `org_id`, deliberately: a process is not tenant data, it serves every org, and inventing
    an org for it would make `/ready` pick one arbitrarily. See ADR-009. `platform_operators` is
    the only other table without the column, for the same kind of reason (ADR-024).
    """

    __tablename__ = "worker_heartbeats"

    #: Stable for the life of the process. Host plus pid, so two workers on one box are two rows
    #: rather than one row fighting over itself.
    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: Which process this is: `worker` today, room for a second role later.
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="worker")
    last_seen_at: Mapped[datetime] = _now_col(nullable=False)
    started_at: Mapped[datetime] = _now_col(nullable=False)
    #: The health snapshot: cycles, last error and stall state per task. Read by `/ready` and by
    #: the admin view, never by anything that makes a decision about sending.
    info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))

    __table_args__ = (CheckConstraint("role IN ('worker')", name="ck_worker_heartbeats_role"),)


class Alert(Base):
    """Something an operator needs to know about, and acknowledge (§18).

    Written to the table first and pushed to `ALERT_WEBHOOK_URL` second. That order matters: the
    webhook is optional and allowed to fail, and an alert that exists only in a failed HTTP
    request is an alert nobody will ever see. The table is the record; the webhook is the nudge.
    """

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: NULL means "the system", not "unknown": a stalled worker or an unreachable database is
    #: nobody's tenant data, it concerns every org at once, and attributing it to one arbitrary
    #: org would hide it from the others. This is the only nullable `org_id` in the schema, and
    #: `list_alerts` returns an org's own rows together with these. See ADR-009.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE")
    )
    #: info | warning | critical. Only `critical` is expected to wake anybody.
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    #: A stable machine name — `smtp_auth_failed`, `worker_stale`, `jobs_dead`. Grouped on, and
    #: used to suppress a repeat of the same condition within the cool-down.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Plain text for a human. Never a secret and never an email body (§18).
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now_col(nullable=False)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The Supabase `sub` of whoever acknowledged it.
    acked_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','critical')", name="ck_alerts_severity"),
        Index("ix_alerts_org_created", "org_id", "created_at"),
        Index("ix_alerts_org_kind_created", "org_id", "kind", "created_at"),
    )


class Attachment(Base):
    """One optional file per campaign, attached to the emails that campaign sends.

    **The bytes are here rather than on `campaigns`** because a campaign row is read to draw
    every row of the campaigns list, and a `bytea` column would come with it every time. They
    are here rather than in S3 because one capped file per campaign is a rounding error against
    the free disk, it is already covered by the nightly dump, and an object store would add a
    second place for the truth to live. ADR-016 records the trade and when to revisit it.

    `content_type` is ours, looked up from the extension against an allowlist -- never the
    `Content-Type` the browser claimed. See `app/domain/attachment.py`.
    """

    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: So the UI can say which file this is, and an operator can confirm what went out, without
    #: moving several megabytes to answer the question.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", name="uq_attachments_campaign"),
        CheckConstraint("size_bytes > 0", name="ck_attachments_size_positive"),
        CheckConstraint("size_bytes <= 5242880", name="ck_attachments_size_within_limit"),
    )


class MailCredential(Base):
    """How one mailbox authenticates, with the secret encrypted at rest.

    Its own table rather than columns on `mailboxes`, because `mailboxes` is read on every
    send-loop cycle and a secret should not be dragged through a query that does not need it --
    the same reasoning that put attachment bytes in their own table. It also means the one table
    holding secrets is a single obvious thing to reason about.

    **`secret` is ciphertext** (`app/services/credentials.py`). No route returns it, no log line
    contains it, no event payload carries it. The API answers whether a mailbox is connected, as
    what address, and when it was last refreshed.
    """

    __tablename__ = "mail_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: google_oauth | app_password.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The address Google says this token belongs to, checked against the mailbox's `from_email`
    #: when connecting -- authorising the wrong account is easy with several signed in, and
    #: without this check it fails at send time instead.
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)
    secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the person last stood at Google's consent screen. **Not the same as
    #: `last_refreshed_at`**, which moves every hour as access tokens are minted, and not the
    #: same as `created_at`, which stops moving once the row exists and so would make a
    #: reconnected mailbox look as stale as it was before somebody fixed it.
    #:
    #: It is the only column from which "this connection lapses on Friday" can be computed, and
    #: the whole point of warning anybody is to say that before Friday rather than after.
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when a refresh fails. An OAuth client in Testing status expires refresh tokens after
    #: 7 days, so this is an expected state with a known fix, not an exception.
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("mailbox_id", name="uq_mail_credentials_mailbox"),
        CheckConstraint("kind IN ('google_oauth','app_password')", name="ck_mail_cred_kind"),
    )


class OAuthState(Base):
    """One in-flight OAuth round trip.

    The `state` nonce is the whole point. Without it anybody could hand a signed-in user a
    crafted callback URL and attach **their** mailbox to that user's org -- the user would then
    send their campaign through an attacker's account, and every reply would go there too.
    """

    __tablename__ = "oauth_states"

    id: Mapped[uuid.UUID] = _uuid_pk()
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    redirect_to: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)
    #: Single use. A replayed callback is refused rather than quietly re-running.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("state", name="uq_oauth_states_state"),
        Index("ix_oauth_states_created", "created_at"),
    )


class LeadOutcome(Base):
    """What happened to one lead, as distinct from what any one reply said.

    `docs/DATASET_DESIGN.md` is the reasoning. The short version: `classifications.label` records
    what a *message* said and drives suppressions; this records the lead's standing **result**,
    which is slower, revisable, and sometimes set by no message at all — silence past a wait
    window is an outcome, and a booked meeting is one that no reply can produce.

    **One row per lead, updated in place.** `events` remains the append-only history of how it got
    here; a second append-only table beside it would duplicate the audit spine and the two would
    eventually disagree.

    **`outcome` is nullable and that is the design.** NULL means "still watching" — a state that
    is genuinely different from every member of the enum. A `pending` value would end up in
    exports and be counted as a class, which is exactly the mistake this table exists to avoid.
    """

    __tablename__ = "lead_outcomes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Nullable so an erasure can detach the person and keep the anonymised features. A deletion
    #: request must not silently destroy the fact that *an* email of this shape got this result.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="SET NULL")
    )
    #: Denormalised **on purpose**: a campaign can be deleted, and an exported row must still be
    #: able to say what kind of campaign produced it.
    campaign_kind: Mapped[str] = mapped_column(String(20), nullable=False, default="outreach")
    #: `app.domain.outcome.Outcome`, or NULL while still being watched.
    outcome: Mapped[str | None] = mapped_column(String(30))
    #: True means "nothing had happened by `observed_until`". **Not a negative result.**
    censored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: `app.domain.outcome.Stage`, validated against the campaign's kind.
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="sent")
    #: Only meaningful for `objection`: price | timing | trust | fit | other.
    objection_kind: Mapped[str | None] = mapped_column(String(20))
    #: What makes "revisit in March" actionable rather than merely recorded.
    not_now_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: `system`, or a person's address. A human confirmation outranks a model's, and the two have
    #: to be distinguishable or the dataset cannot be weighted.
    confirmed_by: Mapped[str | None] = mapped_column(String(320))
    #: The survival triple: when the clock started, when the event happened, how long we watched.
    first_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)
    updated_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("lead_id", name="uq_lead_outcomes_lead"),
        Index("ix_lead_outcomes_org_outcome", "org_id", "outcome"),
        # Only a censored row may be `no_reply_after_window`, and nothing else may be censored.
        # Enforced here rather than trusted to the writer, because the whole dataset rests on
        # that distinction and a stray UPDATE would quietly destroy it.
        CheckConstraint(
            "(outcome = 'no_reply_after_window') = censored",
            name="ck_lead_outcomes_censoring",
        ),
    )


#: The smallest share of assignments that may ignore an experiment's weights. Zero would let a
#: weighted allocator starve an arm, and a starved arm is one whose result would have surprised
#: somebody. Enforced by `ck_experiments_exploration_floor` as well as by the validator.
MIN_EXPLORATION_SHARE = 0.1


class Experiment(Base):
    """An A/B arm definition, so a result can be attributed rather than merely observed.

    **A lead stays in its arm across a whole sequence**, because an A first email followed by a
    B follow-up makes the result uninterpretable and the recipient is the one person who can
    tell. The same reasoning as the sticky sending mailbox (ADR-044).

    **ADR-026 specified that stickiness as a deterministic hash of (lead, experiment), and
    building it showed why that cannot work.** A hash has to be taken modulo the number of arms,
    so adding a third arm to a running experiment moves most leads already in flight from one
    arm to the other -- silently, retroactively, and after their first email has gone out. Every
    message already sent would then be attributed to an arm its draft did not come from. So the
    arm is **stored on the lead** (`leads.variant`), written once, and nothing recomputes it.
    One stored fact beats a derivation that can change under rows that already exist.

    `exploration_share` is the fraction assigned uniformly at random, ignoring the weights. It
    has a **floor** rather than a free range (`ck_experiments_exploration_floor`): a weighted
    allocator with nothing held back can drive an arm to zero, and that is exactly when it stops
    producing the data that would show the operator's preferred arm was the worse one.
    """

    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: `[{"variant": "a", "weight": 1, "instruction": "..."}]`, validated through
    #: `domain.experiment.ExperimentSpec` at the door -- the same bargain `campaigns.persona`
    #: and `campaigns.settings` make, and for the same reason: a shape that will grow is worse
    #: as a table of columns, and the validation belongs with the model that reads it.
    arms: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: 0.0 to 1.0. The fraction assigned at random rather than by the deterministic hash.
    exploration_share: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: When allocation stopped. Needed to read a funnel: messages sent after this carry no arm,
    #: and without the date that looks like an allocator that quietly broke.
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        # The floor, built from the constant so the constraint and the validator cannot
        # disagree about it -- the cure adopted after `reason="one-click unsubscribe"` made
        # every opt-out a 500.
        CheckConstraint(
            f"exploration_share >= {MIN_EXPLORATION_SHARE} AND exploration_share <= 1",
            name="ck_experiments_exploration_floor",
        ),
        # **At most one active experiment per campaign**, in the database rather than in the
        # route's memory. Two would both allocate, the second assignment would overwrite the
        # first on `leads.variant`, and every message would then carry whichever label ran
        # last -- the quietest available way to make a result meaningless.
        Index(
            "uq_experiments_one_active",
            "campaign_id",
            unique=True,
            postgresql_where=text("active"),
        ),
        Index("ix_experiments_org", "org_id"),
    )


class PlatformOperator(Base):
    """Somebody who may see that the workspaces exist, and nothing inside them (ADR-024).

    **The only table here with no `org_id`, and the reason is the point.** ADR-004 puts that
    column on everything because everything else *belongs* to one tenant; this row belongs to the
    deployment. `worker_heartbeats` and `alerts.org_id` are the two existing deviations and the
    argument is the same one -- a process is not tenant data.

    `user_id` is Supabase's `sub`, as in `members`, and kept deliberately separate from it: a role
    is a position *inside* one workspace, so reusing `members.role` for a scope that sits above
    every workspace would make the word mean two different things depending on which row you were
    looking at. It also means an operator needs no membership anywhere, which is what lets the
    dependency resolve them without ever touching `provision()`.

    **Written by an `INSERT` and by nothing else.** No route creates one, and
    `tests/unit/test_platform_scope_structure.py` asserts that over the whole api and services
    tree. There is no role that could legitimately grant a scope above every org, so the only
    person who can create an operator is somebody who already has the database.
    """

    __tablename__ = "platform_operators"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Supabase `sub`. Opaque to us, exactly as in `members`.
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    #: For the audit trail and the screen. Not used to match on -- the `sub` is the identity.
    email: Mapped[str | None] = mapped_column(String(320))
    #: Why this person has it. A scope granted by hand deserves a sentence saying who and why,
    #: or in a year nobody can tell a deliberate grant from a forgotten one.
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)


class ApiToken(Base):
    """A credential for the MCP endpoint, and for nothing else (ADR-028).

    **Why this exists rather than reusing the Supabase token the dashboard already holds.** An
    MCP client needs a credential it can store and send on every call. The obvious candidate is
    a Supabase JWT, and it is the wrong one: Supabase accepts the RFC 8707 `resource` parameter
    and ignores it, issuing `aud: "authenticated"` whatever was requested (supabase/auth#2610).
    So a Supabase token cannot be shown to have been minted *for* this endpoint -- which is
    precisely the confused-deputy problem resource indicators exist to prevent. A token minted
    for anything would work here.

    So the MCP endpoint has its own credential, and the two doors each take one kind of key: a
    Supabase JWT is refused by `/mcp`, and one of these is refused everywhere else.

    **Only the hash is stored.** `sha256` rather than a password hash on purpose: this is 32
    bytes of `secrets` output, not a human-chosen password, so there is no dictionary to slow an
    attacker down and the lookup has to be deterministic to be a lookup at all. Stretching a
    256-bit random value buys nothing and would cost a table scan.

    **`org_id` is stored for display and is not the authority.** Authorisation re-resolves the
    `members` row on every call, so removing somebody from a workspace stops their tokens
    working in the same breath -- a token that outlived its membership would be an ex-colleague
    with a key. There is a test for exactly that.
    """

    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The Supabase `sub` of whoever minted it. The token acts as that person.
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: What the person called it, so a list of three tokens can be told apart when revoking one.
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    #: The first few characters, kept in clear so the UI can identify a row without holding the
    #: secret. Shown as `oa_1a2b3c4d…`; useless on its own.
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    #: `sha256` hex of the whole token. Unique, because two rows matching one secret would make
    #: "whose token is this?" depend on which came back first.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    #: NULL means no expiry. An expiring token is the default the UI offers.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Updated at most once a minute, so an idle token does not write a row per call. The point
    #: is answering "is this still in use?" before revoking it, which needs the day not the second.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        # The database refuses a scope the code has never heard of, as `ck_orgs_send_mode` does
        # for send modes. It is what makes a *write* scope unexpressible by hand: there is no
        # `mcp:propose` today, and inventing one in SQL would not make a tool appear, but it
        # would make the catalogue's story false.
        CheckConstraint("scopes <@ ARRAY['mcp:read']::text[]", name="ck_api_tokens_scopes"),
        CheckConstraint("cardinality(scopes) > 0", name="ck_api_tokens_scopes_not_empty"),
        Index("ix_api_tokens_org", "org_id"),
        # The verification query: one row by hash, on every call.
        Index("ix_api_tokens_hash", "token_hash"),
    )


#: The contact providers F9 can call. **The check constraint is built from this tuple** -- the
#: same device as `SUPPRESSION_REASONS`, and for the same reason: a literal that has to match a
#: database constraint, copied into a caller that cannot see it change, is this project's most
#: repeated defect.
CONTACT_PROVIDERS: tuple[str, ...] = ("apollo", "hunter", "snov", "mock")

#: The search providers F17's chain may call, **in the order it tries them by default**. The
#: order is the feature: the chain stops at the first provider that answers, so the order
#: decides what a workspace spends. A workspace with one key has a chain of one and behaves
#: exactly as this system did before F17.
#:
#: These are *search* providers and `CONTACT_PROVIDERS` are *contact* providers. They share one
#: table and one encryption scheme -- so there is one answer to "where are the secrets and how
#: are they protected?" and one key to rotate -- and the two sets of names are deliberately
#: disjoint, so a key saved for one family can never be handed to an adapter from the other.
SEARCH_PROVIDERS: tuple[str, ...] = ("tavily", "brave", "serper")

#: Every provider name `provider_credentials` accepts. **The check constraint is built from
#: this**, so adding a provider is one tuple and one migration rather than a literal copied into
#: a constraint, a route and an adapter registry that can each drift from the others.
ALL_PROVIDERS: tuple[str, ...] = CONTACT_PROVIDERS + SEARCH_PROVIDERS

#: How a provider's credential is held. `api_key` is a string the user pasted; `oauth` is a
#: client id and secret they pasted, which we exchange for a short-lived token per call.
#:
#: Both exist from the first migration deliberately. The owner asked whether a Connect button
#: could fetch a key instead of the user pasting one, and the answer differs per provider --
#: key-based for Hunter, client credentials the user still copies for Snov, partner-gated for
#: Apollo. Adding a Connect flow later is therefore a handler rather than a migration.
PROVIDER_CREDENTIAL_KINDS: tuple[str, ...] = ("api_key", "oauth")

#: What a proposed contact is. `proposed` until a person does something with it; `imported` once
#: it has become a lead; `dismissed` when they said no.
CONTACT_CANDIDATE_STATES: tuple[str, ...] = ("proposed", "imported", "dismissed")

#: How much the provider claims to know about the address. **`guess` is a pattern, not a
#: finding** -- several providers return `first.last@domain` constructed from a naming convention
#: and present it beside a verified one. Mailing a guess is how a bounce rate rises, so the
#: distinction is a column rather than a note inside a confidence score.
CONTACT_EMAIL_STATUSES: tuple[str, ...] = ("verified", "guess", "unknown")


class ProviderCredential(Base):
    """One contact provider's key, for one workspace, encrypted at rest (F9, ADR-041).

    **The same scheme as `mail_credentials`**: Fernet, the key in the environment, ciphertext in
    its own table, and `__repr__` never near the plaintext. Reusing the scheme rather than
    inventing a second one means there is one answer to "where are the secrets and how are they
    protected?", and one key to rotate.

    **One live row per provider per workspace, enforced by a partial unique index.** Hard rule 8
    says one account per provider per org and never rotate keys to evade a free tier; a schema
    that permits five keys for one provider is a schema in which doing that is a loop somebody
    writes later. The index is partial on `revoked_at IS NULL`, so replacing a key the provider
    reissued still works -- what is refused is *holding* several at once.

    **There is no shared key.** No `APOLLO_API_KEY` in `Settings`, no fallback to a deployment
    credential, and a test asserts both. ADR-023 removed the deployment's mailbox for the same
    reason: as long as one identity lives in the environment, every question about who may use it
    is a rule somebody has to hold in their head.
    """

    __tablename__ = "provider_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: One of `CONTACT_PROVIDERS`.
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    #: One of `PROVIDER_CREDENTIAL_KINDS`.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Ciphertext. For `oauth`, the client id and secret as one sealed JSON object -- two columns
    #: would mean two things to encrypt, two to rotate and two to forget to clear.
    secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    #: What the user called it. Never the key, and not a fragment of it either: four characters
    #: of a key are four characters an attacker does not have to guess.
    label: Mapped[str | None] = mapped_column(String(120))
    added_by: Mapped[str | None] = mapped_column(String(200))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Set when the provider refuses it. Read to decide whether a lookup can run, and **never
    #: returned by any route** -- it can quote a provider's reply, and that reaches a browser and
    #: a screenshot. The same rule as `mail_credentials.invalid_reason`.
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # A default repr would print the ciphertext in every traceback and debugger frame.
        return f"ProviderCredential(provider={self.provider!r}, kind={self.kind!r})"

    __table_args__ = (
        CheckConstraint(
            "provider IN (" + ", ".join(f"'{name}'" for name in ALL_PROVIDERS) + ")",
            name="ck_provider_credentials_provider",
        ),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{name}'" for name in PROVIDER_CREDENTIAL_KINDS) + ")",
            name="ck_provider_credentials_kind",
        ),
        Index(
            "uq_provider_credentials_live",
            "org_id",
            "provider",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class ContactCandidate(Base):
    """A hiring contact a provider proposed, waiting for a person to decide (F9, ADR-041).

    **It is not a lead and cannot be mailed.** The send loop reads `leads`; nothing here is in
    that table until somebody imports it, at which point it goes through the ordinary import
    path with its suppression check, its research and drafting, and its human approval. A row
    here is a suggestion, exactly as a `suggestions` row is.

    **No raw provider payload is stored.** It would be the fastest way to debug a shape change,
    and it is somebody's personal data arriving from a third party -- so the fields we chose are
    the fields we keep, and a shape we cannot read is reported as unreadable rather than filed
    away for later.
    """

    __tablename__ = "contact_candidates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The posting this lookup was run for, when it was run for one. Nullable: a lookup can also
    #: be run against a bare domain.
    job_post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="SET NULL")
    )
    #: Set once imported, so the same person is not proposed again next week.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    company_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(300))
    title: Mapped[str | None] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    email_norm: Mapped[str | None] = mapped_column(String(320))
    #: One of `CONTACT_EMAIL_STATUSES`.
    email_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    #: 0-100 as the provider reports it. Kept as the provider's own number rather than rescaled,
    #: because a rescaled score cannot be compared with the provider's own dashboard.
    confidence: Mapped[int | None] = mapped_column(Integer)
    #: A public profile URL the provider returned, for the person to open themselves. **Never
    #: fetched** -- hard rule 4 covers LinkedIn, and nothing here fetches any of them.
    profile_url: Mapped[str | None] = mapped_column(String(2000))
    #: One of `CONTACT_CANDIDATE_STATES`.
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "provider IN (" + ", ".join(f"'{name}'" for name in CONTACT_PROVIDERS) + ")",
            name="ck_contact_candidates_provider",
        ),
        CheckConstraint(
            "email_status IN (" + ", ".join(f"'{name}'" for name in CONTACT_EMAIL_STATUSES) + ")",
            name="ck_contact_candidates_email_status",
        ),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{name}'" for name in CONTACT_CANDIDATE_STATES) + ")",
            name="ck_contact_candidates_state",
        ),
        # One proposal per address per workspace. A second lookup for the same company must
        # update rather than pile up a duplicate the person has to dismiss twice.
        Index(
            "uq_contact_candidates_email",
            "org_id",
            "email_norm",
            unique=True,
            postgresql_where=text("email_norm IS NOT NULL"),
        ),
        Index("ix_contact_candidates_org_state", "org_id", "state"),
    )


class Connection(Base):
    """One person from the user's own LinkedIn connections export (F10, ADR-042).

    **The only way LinkedIn data enters this system.** Hard rule 4 says `linkedin_url` is stored
    and displayed and never fetched; ADR-038 added that LinkedIn data arrives only through a file
    the user uploads. This table is that file, parsed. Nothing here is fetched, enriched or
    verified against anything.

    **The export's `Email Address` column is deliberately not stored.** LinkedIn includes it for
    connections who allow it, and it is the one column that would turn a referral finder into a
    contact database -- somebody's whole address book, belonging to people who agreed to connect
    with *them* and have never heard of us. The feature does not need it: a referral is asked for
    by the user, on LinkedIn, through a link they open themselves. `parse_export` drops the
    column before anything is written, and a test asserts an address in the file reaches no row.

    `company_norm` exists because matching has to work across "Acme, Inc.", "Acme Inc" and
    "ACME" -- the same registrable-key idea as `domain_research` (ADR-011), applied to a name
    because a connections export has no domains in it.
    """

    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(300), nullable=False)
    #: The public profile URL from the export. **Stored and shown, never fetched** (hard rule 4).
    profile_url: Mapped[str | None] = mapped_column(String(2000))
    company: Mapped[str | None] = mapped_column(String(300))
    #: Lower-cased, punctuation and company suffixes removed. The matching key.
    company_norm: Mapped[str | None] = mapped_column(String(300))
    position: Mapped[str | None] = mapped_column(String(300))
    connected_on: Mapped[date | None] = mapped_column(Date)
    #: Which upload this row came from, so a re-import can replace rather than accumulate and a
    #: person can delete one upload without losing the others.
    import_batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        # One row per person per workspace. A second export of a grown network must update rather
        # than double every connection the user already had.
        Index(
            "uq_connections_profile",
            "org_id",
            "profile_url",
            unique=True,
            postgresql_where=text("profile_url IS NOT NULL"),
        ),
        Index("ix_connections_org_company", "org_id", "company_norm"),
    )


#: Where a bullet may be placed. A **fixed vocabulary**, not whatever the model writes: a heading
#: is the one line of a resume that is not a fact, so letting a proposal invent one would give it
#: a free line of prose outside the claim check.
RESUME_SECTIONS: tuple[str, ...] = ("Summary", "Experience", "Projects", "Skills", "Education")

#: `include` puts a fact in the resume, optionally rephrased **inside that one fact**; `drop`
#: records a fact the proposal deliberately left out, with its reason. Order is the order of the
#: `include` ops, so "reorder" needs no operation of its own.
RESUME_OP_KINDS: tuple[str, ...] = ("include", "drop")

#: planned -> applied -> interviewing -> closed, and `closed` is reachable from any of them.
#: **Only a person sets `applied`** (ADR-043): there is no route outside the dashboard door that
#: can write this column, and the extension's token cannot open that door.
APPLICATION_STATUSES: tuple[str, ...] = ("planned", "applied", "interviewing", "closed")


class ResumeVersion(Base):
    """One tailored resume, as the operations that produced it (F20, ADR-043).

    **Immutable, enforced by a trigger rather than by convention.** An application points at the
    exact version that was sent, and the one question somebody asks months later -- "what did
    they actually see?" -- has no answer if this row can be edited afterwards. A change makes a
    new row. The same device as `app.events`, and for the same reason.

    `rendered_text` is stored rather than recomputed because the facts it cites can be edited or
    deleted later: a version regenerated from today's facts would not be the document that was
    sent. `fact_ids` records what it rested on, so a fact that has since changed is findable.

    **The PDF is not here.** A rendering is a derived artefact that can be redone, and this row
    cannot be updated -- so it lives in `resume_renders`, which can.
    """

    __tablename__ = "resume_versions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: The posting it was tailored for, when there was one. `SET NULL` rather than cascade: a
    #: posting archived or removed must not take the document that was sent with it.
    job_post_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="SET NULL")
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    #: Name and contact details: the only lines in the document that are not facts. Supplied by
    #: the person, never by a proposal -- `ProposedEdits` has no field for it.
    header: Mapped[str] = mapped_column(Text, nullable=False, default="")
    edit_ops: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    rendered_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Every confirmed fact this document cites, in the order it cites them.
    fact_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    #: `user:<id>` for a version the person assembled, `model` for one a proposal produced and
    #: the validator accepted. A dataset distinction (ADR-026) as much as an audit one.
    created_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (Index("ix_resume_versions_org_created", "org_id", "created_at"),)


class ResumeRender(Base):
    """The PDF of one resume version, rendered by its own job (F20, ADR-043).

    Separate from `ResumeVersion` for two independent reasons, and either alone would be enough:
    the version row cannot be updated, so a PDF produced afterwards has nowhere to go on it; and
    a `bytea` column on the version would travel with every row of the versions list.

    One row per version, replaced on a re-render. A failed render leaves the previous bytes in
    place rather than truncating them, because a resume somebody is about to attach is worse
    missing than stale.
    """

    __tablename__ = "resume_renders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    resume_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.resume_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (UniqueConstraint("resume_version_id", name="uq_resume_renders_version"),)


class Application(Base):
    """A posting the candidate is pursuing, and the exact resume they sent (F20, ADR-043).

    **`applied_at` and the status cannot disagree**, by a check constraint rather than by the
    route remembering to set both -- the same device as `ck_lead_outcomes_censoring`. A funnel
    counted off a status while the date says otherwise is a funnel nobody can audit.

    **Only a person marks a row applied.** The extension holds an `ext:suggest` token, which
    ADR-039 refuses at every door but `/ext/v1`, and nothing on `/ext/v1` names this table. The
    reason is not tidiness: an extension that could mark an application sent would make the
    tracker claim the user did something they did not do, and they would stop trusting it.
    """

    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    job_post_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.job_posts.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: `SET NULL` rather than cascade, so an erased version cannot erase the record that the
    #: person applied. Which document it was is then unknown, and that is the honest answer.
    resume_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.resume_versions.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    follow_up_due: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _now_col(nullable=False)
    updated_at: Mapped[datetime] = _now_col(nullable=False, onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("org_id", "job_post_id", name="uq_applications_org_post"),
        CheckConstraint(
            "status IN ('planned','applied','interviewing','closed')",
            name="ck_applications_status",
        ),
        # Applied and interviewing mean a date exists; planned means it does not. `closed` is
        # deliberately unconstrained: a posting can be closed without ever having been applied to.
        CheckConstraint(
            "(status IN ('applied','interviewing') AND applied_at IS NOT NULL) "
            "OR (status = 'planned' AND applied_at IS NULL) "
            "OR status = 'closed'",
            name="ck_applications_applied_at",
        ),
        Index("ix_applications_org_status", "org_id", "status"),
    )


#: A pool member's share of a campaign's sends, relative to the other members. 1 is the default
#: and the only value anybody needs until a mailbox is being warmed up beside an established one.
MIN_MAILBOX_WEIGHT = 1
MAX_MAILBOX_WEIGHT = 100


class CampaignMailbox(Base):
    """One mailbox in a campaign's sending pool, with its share (F7, ADR-044).

    **`campaigns.mailbox_id` is still the campaign's primary mailbox and is always a member of
    its own pool**, whether or not it has a row here. That is deliberate: making this table the
    sole authority would mean every existing campaign, every `POST /campaigns` and every
    `PATCH` that attaches a mailbox had to write a row too, and the campaign that forgot would
    simply stop sending with nothing on screen saying why. So the rule is computed in **one**
    function, `services/mailboxes/pool.pool_for`, and read by everything -- the send loop, the
    campaign blockers and the dashboard.

    A row for the primary is therefore allowed, and is how its weight is set to anything but 1.

    **The cap stays on the mailbox** (ADR-034, and PR #67's reasoning before it). A pool spreads
    a campaign's sends over several reputations; it does not raise any one of them.
    """

    __tablename__ = "campaign_mailboxes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.campaigns.id", ondelete="CASCADE"),
        nullable=False,
    )
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.mailboxes.id", ondelete="CASCADE"),
        nullable=False,
    )
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint("campaign_id", "mailbox_id", name="uq_campaign_mailboxes_pair"),
        CheckConstraint(
            f"weight BETWEEN {MIN_MAILBOX_WEIGHT} AND {MAX_MAILBOX_WEIGHT}",
            name="ck_campaign_mailboxes_weight",
        ),
        Index("ix_campaign_mailboxes_mailbox", "org_id", "mailbox_id"),
    )


#: What a booking tool tells us happened. `cancelled` is recorded and **does not move the lead**
#: -- see ADR-045: `MEETING_BOOKED` is terminal, and a meeting that was booked was booked.
BOOKING_EVENT_KINDS: tuple[str, ...] = ("booked", "rescheduled", "cancelled")

#: How a booking was tied to a lead, in words, because the user is the one deciding whether we
#: got it right. `unmatched` is a first-class value: a booking from somebody who is not in the
#: workspace at all is kept and shown, never dropped.
BOOKING_MATCHES: tuple[str, ...] = ("email", "in_conversation", "unmatched")


class BookingWebhook(Base):
    """One workspace's booking-webhook secret, encrypted at rest (F15, ADR-045).

    **The same scheme as `mail_credentials` and `provider_credentials`**: Fernet, the key in the
    environment, ciphertext in its own table, `__repr__` never near the plaintext. One answer to
    "where are the secrets", one key to rotate.

    **There is no deployment-wide booking secret**, for ADR-023's reason: as long as one identity
    lives in the environment, every question about who may use it is a rule somebody maintains.
    A workspace that has not generated one cannot receive a booking, and the route says so.

    One live row per workspace by a partial unique index, so "regenerate" replaces rather than
    accumulates -- a second live secret would mean a revoked one still worked.
    """

    __tablename__ = "booking_webhooks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: Ciphertext of the shared secret the booking tool signs each body with.
    secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(200))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col(nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"<BookingWebhook org={self.org_id} live={self.revoked_at is None}>"

    __table_args__ = (
        Index(
            "uq_booking_webhooks_live",
            "org_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class BookingEvent(Base):
    """A meeting a booking tool says was booked (F15, ADR-045).

    **Every verified booking is stored, matched or not.** A booking from an address that is in no
    campaign is the interesting case rather than the error case -- somebody forwarded the link, or
    the lead replied from a different address -- and dropping it would repeat the job-post
    intake's expensive lesson: an empty result that is indistinguishable from nothing happening.

    `external_id` is the provider's own id for the booking, and the unique index on it is what
    makes a retried delivery harmless. Booking tools retry; a second row would be a second
    meeting in every funnel this feeds.

    **The payload is not stored.** Only the fields this system uses: who, when, and the link to
    the lead. A booking payload carries a provider's words and the invitee's own text, and
    ADR-026's argument about the dataset applies here too -- a column nobody reads is a column
    nobody is protecting.
    """

    __tablename__ = "booking_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.orgs.id", ondelete="CASCADE"), nullable=False
    )
    #: NULL for a booking we could not tie to anybody. Kept rather than refused.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.leads.id", ondelete="SET NULL")
    )
    #: Which tool sent it, as the tool named itself. Free text because there is no allowlist of
    #: booking tools: the webhook is generic on purpose (ADR-045).
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="unknown")
    #: The provider's id for this booking. Its uniqueness is what makes a retry idempotent.
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="booked")
    invitee_email_norm: Mapped[str | None] = mapped_column(String(320))
    invitee_name: Mapped[str | None] = mapped_column(String(300))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: One of `BOOKING_MATCHES`, in words, so a person reviewing a funnel can see *why* this
    #: booking was attributed to this lead rather than trusting that it was.
    matched_by: Mapped[str] = mapped_column(String(20), nullable=False, default="unmatched")
    received_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "org_id", "provider", "external_id", name="uq_booking_events_org_provider_ext"
        ),
        CheckConstraint(
            "kind IN ('booked','rescheduled','cancelled')", name="ck_booking_events_kind"
        ),
        CheckConstraint(
            "matched_by IN ('email','in_conversation','unmatched')",
            name="ck_booking_events_matched_by",
        ),
        Index("ix_booking_events_org_received", "org_id", "received_at"),
    )
