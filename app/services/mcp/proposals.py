"""The one thing this surface may write: a draft into the queue a human already reads (ADR-030).

**Nothing here sends, approves, or moves a lead.** A proposal is the same `Draft` row the in-app
drafter creates, `pending_review`, through the same validator, with `created_by='mcp:<user>'`.
It does not bypass review, it enters it -- which is why Phase 3 needs no new approval path, no
new state and no new send gate. `tests/unit/test_mcp_safety.py` asserts the absence
structurally: this module cannot name an approved status and cannot import the state machine, so
a tool added later cannot reach either by having the import already in the file.

Three refusals happen **before** anything is written, and each exists because of a defect this
project has already paid for:

1. **The lead's state has to admit a review.** Four times a draft has been written and then
   found unapprovable because the transition was illegal -- after a model had been paid for. The
   in-app drafter is reached through the state machine and cannot ask the wrong question; a
   proposal arriving from outside can, so it asks `PROPOSABLE_FROM` first.
2. **A reply obeys the reply policy**, via the same `refuse_reply_draft` the in-app drafter
   calls. Five labels deliberately produce no draft, and `hostile` is the one that matters: a
   machine-written answer to somebody threatening to report us as spam is how a complaint
   becomes a listing. A second copy of that rule here would be the signature defect in the worst
   possible place.
3. **A reply needs a label of record.** There is no safe default: the label decides whether a
   reply may be drafted at all, so guessing could propose an answer to somebody who asked to be
   left alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import drafts as drafts_repo
from app.db.models import Campaign, Classification, Lead, Message, Research
from app.db.repo import OrgContext
from app.domain.persona import Persona
from app.domain.reply import ReplyLabel
from app.services.drafting.from_agent import ProposalRejected, draft_from_agent
from app.services.drafting.prompts import reply_subject
from app.services.mcp import protocol
from app.services.reply.drafter import refuse_reply_draft, reply_hook

#: What a caller may ask for by name. `reply` is not here: it has its own tool, because it takes
#: no subject and needs a classified inbound message, and offering it as a value of `kind` would
#: mean one tool whose required arguments depend on another argument.
PROPOSABLE_KINDS = ("initial", "followup")


def _refused(message: str) -> protocol.McpError:
    """A tool error the agent can act on, rather than a protocol error it cannot.

    `INVALID_PARAMS` is what the router reports as a tool error, which is the class the spec
    distinguishes precisely because a model can recover from it by sending different arguments.
    """
    return protocol.McpError(protocol.INVALID_PARAMS, message)


async def _lead_for(session: AsyncSession, ctx: OrgContext, raw: Any) -> Lead:
    try:
        lead_id = uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _refused("lead_id must be a UUID.") from exc

    # **No `FOR UPDATE`.** The first version of this locked the lead row, which looked prudent
    # and is not: nothing here writes to the lead, `save_draft` already serialises the version
    # number it allocates, and the in-app drafter reads a lead exactly this way. A lock the rest
    # of the write path does not take is a lock that can only add a way to wait -- it held this
    # file's own tests at a lock wait until the timeout.
    lead = await session.scalar(select(Lead).where(Lead.id == lead_id, Lead.org_id == ctx.org_id))
    if lead is None:
        # **Not "you may not touch that lead".** A refusal that distinguishes "not yours" from
        # "does not exist" confirms the id, which is the one thing a caller holding somebody
        # else's id must not learn (ADR-004).
        raise _refused("There is no lead with that id in this workspace.")
    return lead


async def _persona_for(session: AsyncSession, lead: Lead) -> tuple[Campaign, Persona]:
    campaign = await session.get(Campaign, lead.campaign_id)
    if campaign is None or not campaign.persona:
        raise _refused(
            "That lead's campaign has no sender persona, so there is nothing a draft is "
            "allowed to assert. Fill it in on the campaign first."
        )
    try:
        return campaign, Persona.model_validate(campaign.persona)
    except ValidationError as exc:
        # Field names only, never the stored values: this sentence reaches a client, a log and
        # whatever the agent decides to show its user.
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise _refused(
            f"That lead's campaign persona cannot be read ({', '.join(fields)}), so no draft "
            "can be grounded in it."
        ) from None


async def _hooks_for(session: AsyncSession, ctx: OrgContext, lead: Lead) -> list[dict[str, Any]]:
    """The verified research hooks a claim may cite, newest research row first."""
    row = await session.scalar(
        select(Research)
        .where(Research.org_id == ctx.org_id, Research.lead_id == lead.id)
        .order_by(Research.created_at.desc())
        .limit(1)
    )
    return [dict(hook) for hook in (row.hooks or [])] if row is not None else []


async def _check_not_clobbering(
    session: AsyncSession, ctx: OrgContext, *, lead: Lead, kind: str, raw_replaces: Any
) -> None:
    """Refuse to replace a draft this caller has not read, or may not touch at all (ADR-030).

    **The problem this solves.** `save_draft` supersedes the lead's previous live draft of the
    same kind. That is right for a regeneration, which happens because a reviewer asked for one,
    and wrong for a proposal arriving from outside: the draft a reviewer has open can be replaced
    under them, so the one they approve is not the one they read.

    **The fix is optimistic concurrency rather than a lock or a ban.** A proposal has to name the
    draft it replaces, and the only way to know that id is to have read the lead first. A stale
    id is refused, so a re-run against a queue that has moved on cannot clobber, and two agents
    cannot race. Two drafts are refused outright whatever id is given: one a person wrote, whose
    words are not an agent's to overwrite, and one already approved, which is queued to send and
    not an agent's decision to undo.
    """
    replaces: uuid.UUID | None = None
    if raw_replaces not in (None, ""):
        try:
            replaces = uuid.UUID(str(raw_replaces))
        except (ValueError, TypeError, AttributeError) as exc:
            raise _refused("replaces_draft_id must be a UUID.") from exc

    current = await drafts_repo.current_draft(session, lead_id=lead.id, kind=kind)

    if current is None:
        if replaces is not None:
            # Naming a draft that is not there means the caller is working from a stale read,
            # which is exactly the state this check exists to catch.
            raise _refused(
                "There is no current draft for this lead to replace, so replaces_draft_id does "
                "not match anything. Read the lead again and propose without it."
            )
        return

    if drafts_repo.was_approved(current):
        raise _refused(
            f"Draft version {current.version} for this lead has already been approved by a "
            "person and is queued to send. Replacing it is not something to do from here."
        )
    if drafts_repo.was_written_by_a_person(current):
        raise _refused(
            f"Draft version {current.version} for this lead was written by a person, not by "
            "this system. It is not for an agent to overwrite."
        )

    if replaces is None:
        raise _refused(
            f"This lead already has draft version {current.version} ({current.status}) waiting "
            f"for review, id {current.id}. Read it first, and pass "
            "replaces_draft_id with that id if you really mean to replace it."
        )
    if replaces != current.id:
        raise _refused(
            f"replaces_draft_id is out of date: the current draft for this lead is version "
            f"{current.version}, id {current.id}. Read it and decide again rather than "
            "replacing something that has already changed."
        )


async def _check_within_the_daily_limit(
    session: AsyncSession, ctx: OrgContext, *, lead: Lead
) -> None:
    """At most `PROPOSAL_DAILY_LIMIT` agent drafts for one lead in 24 hours.

    The same reasoning as `MAX_REJECTIONS`: a loop spends a day of somebody's free-tier quota on
    one lead while every other lead waits behind it, and buries the review queue. The refusal
    says what to do instead, because a limit that reads as a malfunction is one somebody retries
    against.
    """
    since = datetime.now(UTC) - timedelta(days=1)
    already = await drafts_repo.count_agent_drafts_since(
        session, org_id=ctx.org_id, lead_id=lead.id, since=since
    )
    if already >= drafts_repo.PROPOSAL_DAILY_LIMIT:
        raise _refused(
            f"That is {already} proposals for this lead in the last 24 hours, and the limit is "
            f"{drafts_repo.PROPOSAL_DAILY_LIMIT}. Leave the current draft for a person to read, "
            "or edit it in the dashboard instead of writing another."
        )


def _check_state(lead: Lead, kind: str) -> None:
    allowed = drafts_repo.PROPOSABLE_FROM[kind]
    if lead.state not in allowed:
        raise _refused(
            f"That lead is in state {lead.state}, and a {kind} draft can only be proposed for a "
            f"lead in {' or '.join(sorted(allowed))}. Proposing one now would write a draft "
            "nobody could then approve."
        )


async def _store(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    lead: Lead,
    kind: str,
    subject: str,
    body: str,
    claims: Any,
    persona: Persona,
    hooks: list[dict[str, Any]],
) -> tuple[str, Any]:
    if not isinstance(claims, list):
        claims = []
    try:
        result = draft_from_agent(
            subject=subject,
            body=body,
            claims=[claim for claim in claims if isinstance(claim, dict)],
            persona=persona,
            hooks=hooks,
            lead_name=lead.full_name,
            lead_company=lead.company_name,
            kind=kind,
        )
    except ProposalRejected as exc:
        # Not an email at all, so there is nothing worth putting in front of a reviewer.
        raise _refused(f"That is not a usable draft: {exc}") from None

    row = await drafts_repo.save_draft(
        session,
        ctx,
        lead=lead,
        kind=kind,
        result=result,
        # Whose client did this. The router already built `actor_id` as `mcp:<user>` from the
        # membership the token resolved to, so this needs no second source of truth about who is
        # asking -- and `prompt_version` says it was an agent rather than our own drafter, so
        # together they answer "what wrote this, and for whom" without a new column.
        created_by=ctx.actor_id or "mcp:unknown",
    )
    # **Committed here rather than left to the router.** The audit write that follows is
    # deliberately wrapped and rolls back on failure -- correct for a read, where the audit is
    # the only write in flight, and destructive for a write, because the rollback would discard
    # the proposal itself and report success for a draft that no longer exists.
    await session.commit()

    failures = list(result.validation_payload()["failures"])
    structured = {
        "draft_id": str(row.id),
        "status": row.status,
        "accepted": result.accepted,
        "version": row.version,
        "kind": kind,
        "failures": failures,
        "warnings": list(result.validation_payload()["warnings"]),
    }
    if result.accepted:
        summary = (
            f"Stored draft version {row.version} for this lead and put it in the review queue. "
            "It passed every grounding and formatting check. **It has not been sent and will "
            "not be**: a person has to read it and press approve in the dashboard, and there is "
            "no tool here that can do that."
        )
    else:
        summary = (
            f"Stored draft version {row.version}, but it did not pass the checks, so it is "
            "marked as failed and cannot be approved as it stands:\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\nRewrite it so every specific claim rests on a persona proof point or a "
            "verified research hook, and propose it again."
        )
    return summary, structured


async def propose_draft(
    session: AsyncSession, ctx: OrgContext, args: dict[str, Any]
) -> tuple[str, Any]:
    """Propose a first email or a follow-up for one lead."""
    kind = str(args.get("kind") or "initial")
    if kind not in PROPOSABLE_KINDS:
        raise _refused(f"kind must be one of {', '.join(PROPOSABLE_KINDS)}.")

    lead = await _lead_for(session, ctx, args.get("lead_id"))
    _check_state(lead, kind)
    await _check_not_clobbering(
        session, ctx, lead=lead, kind=kind, raw_replaces=args.get("replaces_draft_id")
    )
    await _check_within_the_daily_limit(session, ctx, lead=lead)
    _campaign, persona = await _persona_for(session, lead)

    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "").strip()
    if not subject or not body:
        raise _refused("Both subject and body are required.")

    if kind == "followup":
        # **The thread's subject, not the agent's, and taken rather than checked.** The
        # validator would catch a follow-up subject that does not continue the thread, so this
        # is not the only defence -- but a refusal tells the agent to guess again at a field
        # whose value is already decided, and a follow-up filed as a new conversation is the
        # defect PR #67's successor fixed by deciding this at drafting time. The in-app
        # follow-up drafter inherits the subject for the same reason; this is that rule, not a
        # second one.
        ours = await session.scalar(
            select(Message)
            .where(
                Message.org_id == ctx.org_id,
                Message.lead_id == lead.id,
                Message.direction == "out",
            )
            .order_by(Message.created_at)
            .limit(1)
        )
        if ours is None or not (ours.subject or "").strip():
            raise _refused(
                "That lead has no sent email with a subject to follow up on, so there is no "
                "thread to continue."
            )
        subject = reply_subject(ours.subject)

    return await _store(
        session,
        ctx,
        lead=lead,
        kind=kind,
        subject=subject,
        body=body,
        claims=args.get("claims"),
        persona=persona,
        hooks=await _hooks_for(session, ctx, lead),
    )


async def propose_reply(
    session: AsyncSession, ctx: OrgContext, args: dict[str, Any]
) -> tuple[str, Any]:
    """Propose an answer to the newest thing this lead said.

    **No subject argument exists.** The thread's subject is ours: `Re: <their subject>`, decided
    here rather than offered as a parameter, because a model that gets creative with a subject
    breaks threading and the recipient gets a second conversation instead of an answer. The
    in-app reply drafter overwrites the model's subject for exactly this reason; here the
    mistake is simply not expressible.
    """
    lead = await _lead_for(session, ctx, args.get("lead_id"))
    _check_state(lead, "reply")
    await _check_not_clobbering(
        session, ctx, lead=lead, kind="reply", raw_replaces=args.get("replaces_draft_id")
    )
    await _check_within_the_daily_limit(session, ctx, lead=lead)

    message = await session.scalar(
        select(Message)
        .where(
            Message.org_id == ctx.org_id,
            Message.lead_id == lead.id,
            Message.direction == "in",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    if message is None:
        raise _refused("That lead has not replied, so there is nothing to answer.")

    classification = await session.scalar(
        select(Classification)
        .where(Classification.org_id == ctx.org_id, Classification.message_id == message.id)
        .order_by(Classification.created_at.desc())
        .limit(1)
    )
    if classification is None:
        # The label decides whether a reply may be drafted at all, so there is no safe default:
        # guessing could propose an answer to somebody who asked to be left alone. The same
        # refusal the in-app reply handler makes, and for the same reason.
        raise _refused(
            "That reply has not been classified yet, and the label decides whether a reply may "
            "be drafted at all. Try again once it has."
        )
    try:
        label = ReplyLabel(classification.label)
    except ValueError:
        raise _refused("That reply's recorded label is not one this system knows.") from None

    if refusal := await refuse_reply_draft(session, ctx, lead=lead, label=label):
        await session.commit()  # the policy event is a record worth keeping
        raise _refused(
            f"No reply may be drafted for this lead: {refusal}. That is the policy for a "
            f"{label.value} reply and it is not something to work around."
        )

    body = str(args.get("body") or "").strip()
    if not body:
        raise _refused("body is required.")

    _campaign, persona = await _persona_for(session, lead)
    hooks = await _hooks_for(session, ctx, lead)
    # Their own words become a citable hook, exactly as they do for the in-app reply drafter, or
    # every sentence referring to what they said would be refused as an unsupported claim.
    hooks.insert(0, dict(reply_hook(message.body_text or "")))

    return await _store(
        session,
        ctx,
        lead=lead,
        kind="reply",
        subject=reply_subject(message.subject or ""),
        body=body,
        claims=args.get("claims"),
        persona=persona,
        hooks=hooks,
    )
