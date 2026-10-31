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


class Experiment(Base):
    """An A/B arm definition, so a result can be attributed rather than merely observed.

    Assignment is a deterministic hash of (lead, experiment) so a lead stays in its arm across a
    whole sequence — an A first email followed by a B follow-up makes the result uninterpretable.
    `exploration_share` is the slice assigned at random instead, which is what makes any of this
    causal rather than correlational.
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
    #: `["a", "b"]` — the arm names this experiment may assign.
    arms: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: 0.0 to 1.0. The fraction assigned at random rather than by the deterministic hash.
    exploration_share: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _now_col(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "exploration_share >= 0 AND exploration_share <= 1",
            name="ck_experiments_exploration_share",
        ),
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
