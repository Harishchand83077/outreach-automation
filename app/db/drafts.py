"""Draft persistence and the review queue.

`ARCHITECTURE_TARGET.md` §7C and §13. Two rules shape this module:

- **A draft is never overwritten.** Every edit or regeneration is a new version, and the old one
  is marked `superseded`. The prototype kept one mutable column, so an edit erased what the
  model had actually produced and there was no way to answer "what did we nearly send?"
  (issue E3).
- **The review queue carries its evidence.** A reviewer who has to go and look up whether a
  claim is true will stop checking by the thirtieth draft. §20 puts review at 50-100 minutes a
  day, and it is the real throughput limit, so the evidence travels with the draft.

Approving does not send. It moves the lead to `APPROVED`, which only makes it eligible for the
send loop — which re-checks everything again before anything leaves (§9.1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Attachment, Campaign, Draft, Lead, Research
from app.db.repo import OrgContext, _page_size
from app.db.state import record_event, transition
from app.domain.attachment import should_attach
from app.domain.campaign import CampaignSettings
from app.domain.state import LeadState
from app.services.drafting.generator import DraftResult

#: A draft in one of these states is still live; anything else has been resolved.
LIVE_STATUSES = ("generated", "validated", "pending_review")

#: Statuses a **reviewer** may still act on, which is a different question from which are live.
#: `validation_failed` belongs here: the draft is shown in the review queue by design, because a
#: reviewer needs to see that a weak model produced something unusable -- and the only sensible
#: thing to do with it is to say why and have it written again. Refusing that left the lead in a
#: dead end with nothing on screen that could move it:
#:
#:     draft 452f5d21-... is validation_failed, so it cannot be rejected
#:
#: The statuses that stay closed are the ones where the decision has already been made --
#: `approved`, `rejected`, `superseded`, `sent` -- and acting on one of those means the queue on
#: screen is stale.
JUDGEABLE_STATUSES = (*LIVE_STATUSES, "validation_failed")

#: How many times one draft may be sent back before the reviewer has to do something else.
#: Each rejection is a model call, and a loop would spend a day's free-tier quota on one lead.
MAX_REJECTIONS = 5

#: The lead states a draft of each kind may be *written into* and still be approvable
#: afterwards (ADR-030). It lives here, beside `approve`, because `approve` is the thing it has
#: to agree with -- and `tests/integration/test_mcp_propose.py` asserts that agreement by
#: approving a draft from every state in it rather than by restating the list.
#:
#: Four times in this project a draft has been written and *then* found unapprovable because
#: the transition was illegal -- `NEW -> RESEARCHED`, `RESEARCHED -> AWAITING_APPROVAL`,
#: `DRAFTED -> DRAFTED` and `REPLIED -> APPROVED` -- each time after a model had been paid for.
#: The in-app drafter is reached through the state machine and cannot ask the wrong question;
#: anything that writes a draft from outside that path can, so it has to ask this first.
#:
#: `initial` admits `DRAFTED` because `approve` writes the missing `AWAITING_APPROVAL` step
#: itself. `followup` and `reply` are exactly `outbox._VALID_FROM`, because approving one of
#: those deliberately does not move the lead at all: the draft becoming `approved` is what makes
#: it sendable, and the state says where the *conversation* is.
PROPOSABLE_FROM: dict[str, frozenset[str]] = {
    "initial": frozenset({LeadState.DRAFTED.value, LeadState.AWAITING_APPROVAL.value}),
    "followup": frozenset({LeadState.SENT.value}),
    "reply": frozenset({LeadState.REPLIED.value}),
}


@dataclass
class ReviewItem:
    """One row of the review queue: the draft, plus what a reviewer needs to judge it."""

    draft: Draft
    lead: Lead
    #: Verified hooks with their source URLs, so a claim can be checked in one click.
    hooks: list[dict[str, Any]]
    research_confidence: float
    thin_research: bool
    #: The file this email will carry, or `None`. Resolved here rather than left to the UI
    #: because the rule -- campaign default unless this draft overrode it -- lives in
    #: `app/domain/attachment.py` and a second copy of it in JavaScript would eventually
    #: disagree with the one the send loop uses. A reviewer unticking a box has to be shown the
    #: same answer the send loop will reach.
    attachment_filename: str | None = None
    #: Whether it is actually attached to *this* draft, after the override.
    attached: bool = False

    @property
    def needs_individual_review(self) -> bool:
        """Whether this draft may be bulk-approved.

        Thin research or a failed validation means somebody should read this one properly. Bulk
        approval exists to make the common case fast, not to wave through the doubtful case.
        """
        return self.thin_research or bool(self.draft.validation.get("failures"))


async def next_version(session: AsyncSession, lead_id: uuid.UUID, kind: str) -> int:
    highest = await session.scalar(
        select(func.max(Draft.version)).where(Draft.lead_id == lead_id, Draft.kind == kind)
    )
    return int(highest or 0) + 1


async def save_draft(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    lead: Lead,
    kind: str,
    result: DraftResult,
    created_by: str = "system",
) -> Draft:
    """Persist a generated draft as a new version, superseding any earlier live one.

    A failed result is stored too, with `validation_failed`, because a reviewer needs to see
    what went wrong — but its status keeps it out of the approval path, so error text can never
    become email content (issue B2).
    """
    await _supersede_live(session, lead_id=lead.id, kind=kind)

    accepted = result.accepted
    draft_output = result.draft or (result.attempts[-1].draft if result.attempts else None)
    if draft_output is None:
        raise ValueError("cannot save a draft result that produced no text at all")

    row = Draft(
        org_id=ctx.org_id,
        lead_id=lead.id,
        kind=kind,
        version=await next_version(session, lead.id, kind),
        subject=draft_output.subject,
        body=draft_output.body,
        claims=[claim.model_dump() for claim in draft_output.claims],
        validation=result.validation_payload(),
        status="pending_review" if accepted else "validation_failed",
        prompt_version=result.prompt_version,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()

    await record_event(
        session,
        org_id=ctx.org_id,
        campaign_id=lead.campaign_id,
        lead_id=lead.id,
        actor="llm",
        type="draft.created",
        data={
            "kind": kind,
            "version": row.version,
            "status": row.status,
            "attempts": result.attempt_count,
        },
    )
    return row


def kind_of(draft: Draft) -> str:
    """A draft's kind as plain text, since the column is a free string."""
    return str(draft.kind)


async def _supersede_live(session: AsyncSession, *, lead_id: uuid.UUID, kind: str) -> None:
    """Retire whatever this lead's previous draft of this kind was.

    **`validation_failed` is superseded too, and that is a change.** A failed draft is kept in
    the review queue deliberately, so a reviewer can see that a weak model produced something
    unusable rather than wonder where the lead went -- but only until a newer attempt exists.
    It did not used to be retired, because it is not a *live* status, so the queue showed both
    the failure and its replacement: two rows for one lead, one of them unjudgeable and the
    other the real thing. Seen on the live system the moment regeneration started working.

    Nothing is lost. The row stays, with its failures, and `save_draft` writes a `draft.created`
    event per attempt, so the lead's timeline still shows every version and why each failed.
    """
    await session.execute(
        update(Draft)
        .where(
            Draft.lead_id == lead_id,
            Draft.kind == kind,
            Draft.status.in_(JUDGEABLE_STATUSES),
        )
        .values(status="superseded")
    )


async def approve(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    draft_id: uuid.UUID,
    approved_by: str,
    edited_subject: str | None = None,
    edited_body: str | None = None,
    now: datetime | None = None,
) -> Draft:
    """Approve a draft, optionally with edits, and move the lead to `APPROVED`.

    An edit creates a **new version** rather than mutating this one, so the record of what the
    model produced survives alongside what the human decided to send.

    This does not send. It makes the lead eligible for the send loop, which re-checks
    suppression, the window, the cap and the lead's state before anything leaves.
    """
    now = now or datetime.now(tz=UTC)
    draft = await _locked_draft(session, ctx, draft_id)

    if draft.status not in JUDGEABLE_STATUSES:
        raise ValueError(f"draft {draft_id} is {draft.status}, so it cannot be approved")

    lead = await session.get(Lead, draft.lead_id)
    if lead is None or lead.org_id != ctx.org_id:
        raise ValueError("the draft's lead is missing")

    target = draft
    edited = (edited_subject is not None and edited_subject != draft.subject) or (
        edited_body is not None and edited_body != draft.body
    )

    # **A draft that failed validation may be approved only after somebody changes it.** Not
    # because a human needs permission -- `review_all` already means a person decides, and an
    # edit to an accepted draft is unchecked too, deliberately, because the reviewer is the
    # authority on what leaves. It is because the validator says something concrete is wrong
    # with *this text*: a forbidden phrase, a claim resting on nothing, an unfilled merge field.
    # Approving it unchanged is almost always a reviewer clicking past a warning they did not
    # read, and the reasons are on screen beside the button. Requiring an edit turns "I accept
    # the risk" into "I fixed it", which is the only version worth allowing.
    if draft.status == "validation_failed" and not edited:
        raise ValueError(
            f"draft {draft_id} failed validation, so it cannot be approved as it stands. "
            "Edit the text to fix what the check objected to, or reject it with a note and it "
            "will be written again."
        )

    if edited:
        subject = (edited_subject if edited_subject is not None else draft.subject).strip()
        body = (edited_body if edited_body is not None else draft.body).strip()
        if not subject or not body:
            # The prototype's UI could approve an empty edit, and the empty text was sent
            # (part of issue B13).
            raise ValueError("an edited draft must have a non-empty subject and body")

        draft.status = "superseded"
        target = Draft(
            org_id=ctx.org_id,
            lead_id=draft.lead_id,
            kind=draft.kind,
            version=await next_version(session, draft.lead_id, draft.kind),
            subject=subject,
            body=body,
            claims=list(draft.claims),
            # Human-edited text has not been through the validator. Recorded plainly rather
            # than implied, so the audit trail does not overstate what was checked.
            validation={**dict(draft.validation), "human_edited": True},
            status="approved",
            prompt_version=draft.prompt_version,
            created_by=approved_by,
            # **The pair this points at is the most valuable thing in the dataset**: what a model
            # wrote, and what a person decided it should have said. Version numbers cannot
            # express it -- a regeneration after a rejection also makes a new version, and that
            # is a different event from a human rewriting the model's words.
            derived_from_id=draft.id,
        )
        session.add(target)
        await session.flush()
    else:
        target.status = "approved"

    target.approved_by = approved_by
    target.approved_at = now

    # A draft that failed validation left the lead in `DRAFTED`: the handler only moves it to
    # `AWAITING_APPROVAL` once a draft passes. So approving a reviewer's fixed-up version used
    # to die on `DRAFTED -> APPROVED is not a legal transition`, after the edit had been stored.
    #
    # The step is written rather than the table being widened. `DRAFTED -> APPROVED` as a legal
    # move would let anything skip review, and the two-step version is also what actually
    # happened: a person was shown this draft, and then approved it.
    if LeadState(lead.state) is LeadState.DRAFTED:
        await transition(
            session,
            org_id=ctx.org_id,
            lead_id=lead.id,
            to_state=LeadState.AWAITING_APPROVAL,
            actor="user",
            actor_id=approved_by,
            data={"draft_id": str(target.id), "reason": "a reviewer opened a failed draft"},
            now=now,
        )

    # **Only a first email moves the lead to `APPROVED`.** A reply is sent from `REPLIED` and a
    # follow-up from `SENT` -- `outbox._VALID_FROM` says so, and the send loop checks it -- so
    # moving either to `APPROVED` would make the message unsendable by the very act of
    # approving it. It never got that far: `REPLIED -> APPROVED` is not a legal transition, so
    # approving a reply raised inside the route and the operator got
    #
    #     Something went wrong. Quote the request id if you report this.
    #
    # with the draft still pending and no way to tell why. What makes a draft sendable is the
    # draft being `approved`; the lead's state says where the *conversation* is, and approving
    # our answer does not move the conversation -- sending it does.
    data = {"draft_id": str(target.id), "version": target.version, "edited": edited}
    if kind_of(draft) == "initial":
        await transition(
            session,
            org_id=ctx.org_id,
            lead_id=lead.id,
            to_state=LeadState.APPROVED,
            actor="user",
            actor_id=approved_by,
            data=data,
            now=now,
        )
    else:
        # The state does not change, but a person made a decision and the trail has to show it.
        await record_event(
            session,
            org_id=ctx.org_id,
            campaign_id=lead.campaign_id,
            lead_id=lead.id,
            actor="user",
            actor_id=approved_by,
            type="draft.approved",
            data={**data, "kind": kind_of(draft), "lead_state": lead.state},
        )

    await session.flush()
    return target


async def reject(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    draft_id: uuid.UUID,
    rejected_by: str,
    feedback: str,
    now: datetime | None = None,
) -> Draft:
    """Reject with feedback, sending the lead back to `DRAFTED` for regeneration.

    The feedback is stored on the draft and fed to the next attempt, so a reviewer's objection
    actually reaches the model rather than being lost in a UI.
    """
    now = now or datetime.now(tz=UTC)
    draft = await _locked_draft(session, ctx, draft_id)

    if draft.status not in JUDGEABLE_STATUSES:
        raise ValueError(f"draft {draft_id} is {draft.status}, so it cannot be rejected")

    # **A reviewer may not reject for ever.** Each rejection costs a model call, and a loop --
    # a lead the model simply cannot draft well, or a reviewer looking for a sentence that is
    # not in the evidence -- would spend the day's free-tier quota on one lead while every
    # other lead waits behind it. Saying so is the point: silently refusing would look like the
    # rejection button being broken again, which it has been.
    rejections = await session.scalar(
        select(func.count())
        .select_from(Draft)
        .where(
            Draft.lead_id == draft.lead_id,
            Draft.kind == draft.kind,
            Draft.status == "rejected",
        )
    )
    if int(rejections or 0) >= MAX_REJECTIONS:
        raise ValueError(
            f"this {draft.kind} has already been rejected {MAX_REJECTIONS} times. Edit the text "
            "and approve it, skip the lead, or fix the campaign's persona or template -- asking "
            "for another draft is unlikely to produce a different one."
        )

    draft.status = "rejected"
    draft.feedback = feedback.strip()[:2000]

    lead = await session.get(Lead, draft.lead_id)
    if lead is None or lead.org_id != ctx.org_id:
        raise ValueError("the draft's lead is missing")

    if LeadState(lead.state) is LeadState.AWAITING_APPROVAL:
        await transition(
            session,
            org_id=ctx.org_id,
            lead_id=lead.id,
            to_state=LeadState.DRAFTED,
            actor="user",
            actor_id=rejected_by,
            data={"draft_id": str(draft.id), "feedback": draft.feedback},
            now=now,
        )

    # **The regeneration is enqueued here, in the same transaction as the rejection.** It used
    # to be nobody's job: this function sent the lead back to `DRAFTED` and said "for
    # regeneration" in its docstring, the route recorded the feedback, and nothing wrote the
    # next draft. In production a reviewer rejected a draft, the queue went from 1 to 0, and the
    # lead sat in `DRAFTED` with no pending draft and no job -- visible only to somebody willing
    # to query the database.
    #
    # Putting it in the transaction rather than in the caller is the point. A caller can forget;
    # a caller that is added later can forget again. Here the rejection and the work it implies
    # either both land or neither does.
    from app.db.queue import JobKind, enqueue

    await enqueue(
        session,
        org_id=ctx.org_id,
        kind=JobKind.DRAFT_LEAD if draft.kind == "initial" else JobKind.DRAFT_REPLY,
        # The lead travels in the **column**, which is what every other caller sets and what
        # the FK uses to cascade a deleted lead's work away. The payload keeps it too, because
        # a job row is read by people as well as by code.
        lead_id=lead.id,
        payload={"lead_id": str(lead.id)},
        # The partial unique index covers only live statuses, so this is refused while an
        # earlier regeneration is still queued and allowed once it has finished — which is the
        # behaviour a reviewer rejecting twice in quick succession needs.
        dedupe_key=f"draft:{lead.id}",
    )

    await session.flush()
    return draft


async def latest_rejection_feedback(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    lead_id: uuid.UUID,
    kind: str = "initial",
) -> str | None:
    """The most recent reviewer objection for this lead, or `None`.

    Stored feedback that never reaches the model is a UI that lies: the reviewer types "add a
    line about Athena", watches it land in the audit trail, and gets back a draft written as if
    they had said nothing. `build_draft_prompt` has always had a `feedback` slot — it was only
    ever filled by the generator's own validation retries, never by a person.

    The newest wins. After two rounds of review the model should be told the current objection,
    not the one that was already addressed.
    """
    return await session.scalar(
        select(Draft.feedback)
        .where(
            Draft.org_id == ctx.org_id,
            Draft.lead_id == lead_id,
            Draft.kind == kind,
            Draft.status == "rejected",
            Draft.feedback.is_not(None),
        )
        .order_by(Draft.created_at.desc(), Draft.version.desc())
        .limit(1)
    )


async def review_queue(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID | None = None,
    draft_ids: list[uuid.UUID] | None = None,
    limit: int | None = None,
    after: tuple[datetime, uuid.UUID] | None = None,
    include_failed: bool = True,
) -> list[ReviewItem]:
    """Drafts awaiting a decision, each with the evidence needed to judge it.

    One query joined to research rather than N+1 lookups: a reviewer opening the queue should
    not wait, and the evidence is the whole reason the queue is usable.

    **Oldest first, and paged.** FIFO so nothing waits indefinitely while newer drafts are
    judged ahead of it. Paged because each item carries a subject, a body and its hooks — a
    hundred of them is a response measured in hundreds of kilobytes and a screen nobody can
    work through. `after` is the `(created_at, id)` of the last row the caller saw.
    """
    statuses = list(LIVE_STATUSES) + (["validation_failed"] if include_failed else [])

    stmt = (
        select(Draft, Lead, Research, Campaign, Attachment.filename)
        .join(Lead, Lead.id == Draft.lead_id)
        .join(Campaign, Campaign.id == Lead.campaign_id)
        # Outer, and only the name: whether a campaign has a file is part of the row, and the
        # bytes never are. A review queue that loaded five megabytes per draft would be a
        # review queue nobody opens twice.
        .outerjoin(Attachment, Attachment.campaign_id == Campaign.id)
        .outerjoin(Research, Research.lead_id == Draft.lead_id)
        .where(Draft.org_id == ctx.org_id, Draft.status.in_(statuses))
        # `id` breaks the tie, or two drafts created in the same millisecond page unstably and
        # one of them is served twice while the other is never seen.
        .order_by(Draft.created_at, Draft.id)
        .limit(_page_size(limit))
    )
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    if after is not None:
        moment, row_id = after
        stmt = stmt.where(
            tuple_(Draft.created_at, Draft.id) > tuple_(literal(moment), literal(row_id))
        )
    if draft_ids is not None:
        # Asking for specific drafts must return exactly those, not the first page of the
        # queue — otherwise a bulk approve silently skips anything that sorts later.
        stmt = stmt.where(Draft.id.in_(draft_ids)).limit(len(draft_ids) or 1)

    rows = (await session.execute(stmt)).all()
    return [
        ReviewItem(
            draft=draft,
            lead=lead,
            hooks=list(research.hooks) if research else [],
            research_confidence=research.confidence if research else 0.0,
            thin_research=research.thin if research else True,
            attachment_filename=filename,
            attached=bool(filename) and _attached(draft, campaign),
        )
        for draft, lead, research, campaign, filename in rows
    ]


def _attached(draft: Draft, campaign: Campaign) -> bool:
    """Whether this draft carries the campaign's file, by the same rule the send loop uses.

    Unreadable settings mean the defaults, not an exception: this is one column of a review
    screen, and a campaign whose settings cannot be parsed already has that reported properly
    through `blockers`. Failing the whole queue here would hide every other draft behind it.
    """
    try:
        settings = CampaignSettings.model_validate(campaign.settings or {})
    except ValidationError:
        settings = CampaignSettings()
    default = {
        "initial": settings.attach_initial,
        "followup": settings.attach_followups,
        "reply": False,
    }.get(draft.kind, False)
    return should_attach(draft_override=draft.attach, campaign_default=default)


async def review_queue_size(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID | None = None,
    include_failed: bool = True,
) -> int:
    """How many drafts are waiting, in total.

    A cursor answers "is there another page", which is not the question a reviewer has. "47
    drafts waiting" is what decides whether somebody starts now or after lunch, and a queue
    that only ever says "more" gives no sense of whether it is being cleared.

    One `COUNT` over an indexed status column, computed only for the page the operator is
    looking at -- not per row.
    """
    statuses = list(LIVE_STATUSES) + (["validation_failed"] if include_failed else [])
    stmt = (
        select(func.count())
        .select_from(Draft)
        .join(Lead, Lead.id == Draft.lead_id)
        .where(Draft.org_id == ctx.org_id, Draft.status.in_(statuses))
    )
    if campaign_id is not None:
        stmt = stmt.where(Lead.campaign_id == campaign_id)
    return int(await session.scalar(stmt) or 0)


async def bulk_approve(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    draft_ids: list[uuid.UUID],
    approved_by: str,
    min_confidence: float = 0.6,
    now: datetime | None = None,
) -> tuple[list[uuid.UUID], dict[str, str]]:
    """Approve several drafts, refusing the ones that need a person.

    Returns `(approved, {draft_id: reason})`. A draft with thin research, a failed validation or
    confidence below the campaign threshold is **skipped rather than approved**: bulk approval
    exists to make the easy case fast, not to wave through the doubtful one.
    """
    approved: list[uuid.UUID] = []
    skipped: dict[str, str] = {}

    # Fetched by id rather than as a page of the queue: with a default page size, any draft
    # that sorted later would be reported as "not in the review queue" and silently skipped.
    items = {item.draft.id: item for item in await review_queue(session, ctx, draft_ids=draft_ids)}

    for draft_id in draft_ids:
        item = items.get(draft_id)
        if item is None:
            skipped[str(draft_id)] = "not in the review queue"
            continue
        if item.draft.status == "validation_failed":
            skipped[str(draft_id)] = "validation failed; read this one"
            continue
        if item.thin_research:
            skipped[str(draft_id)] = "research was thin; read this one"
            continue
        if item.research_confidence < min_confidence:
            skipped[str(draft_id)] = (
                f"research confidence {item.research_confidence:.2f} is below {min_confidence:.2f}"
            )
            continue

        await approve(session, ctx, draft_id=draft_id, approved_by=approved_by, now=now)
        approved.append(draft_id)

    return approved, skipped


async def _locked_draft(session: AsyncSession, ctx: OrgContext, draft_id: uuid.UUID) -> Draft:
    result = await session.execute(
        select(Draft).where(Draft.id == draft_id, Draft.org_id == ctx.org_id).with_for_update()
    )
    draft = result.scalar_one_or_none()
    if draft is None:
        raise LookupError(f"draft {draft_id} not found in this org")
    return draft


async def set_attach(
    session: AsyncSession, ctx: OrgContext, *, draft_id: uuid.UUID, attach: bool
) -> bool | None:
    """Record whether this draft carries the campaign's attachment.

    Returns `None` when there is no such draft in this org, `False` when the draft has already
    been judged and may no longer change, and `True` when the value was written.

    Three return values rather than raising, because the caller is an HTTP route that has to
    answer 404, 409 and 200, and encoding that as exception types would put the API's status
    codes inside the repository.

    `JUDGEABLE_STATUSES` is the same gate `approve` and `reject` use. It is the right one: the
    question "may a reviewer still change their mind about this draft?" is one question, and a
    draft whose validation failed is still being decided -- which is exactly the draft somebody
    is most likely to be editing when they decide the attachment is the problem.
    """
    draft = await session.get(Draft, draft_id)
    if draft is None or draft.org_id != ctx.org_id:
        return None
    if draft.status not in JUDGEABLE_STATUSES:
        return False

    draft.attach = attach
    await session.flush()
    await record_event(
        session,
        org_id=ctx.org_id,
        lead_id=draft.lead_id,
        type="draft.attachment_toggled",
        actor="user",
        actor_id=ctx.actor_id,
        data={"draft_id": str(draft.id), "kind": draft.kind, "attach": attach},
    )
    return True
