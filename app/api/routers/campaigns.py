"""Campaigns and their leads.

`ARCHITECTURE_TARGET.md` §14. Every list is cursor-paginated and every list row is a *summary*.

That second rule is issue C7. The prototype's `GET /api/leads` returned each lead's full
insights, draft body and reply text, and the dashboard polled it every 2.5 seconds — so the whole
contents of the pipeline crossed the wire continuously, to anyone who asked, because there was no
authentication either. Here a list row carries what a list needs to render: who, where they are,
when they next move. The bodies live behind `GET /leads/{id}`, which is one lead at a time and
therefore one lead's worth of exposure.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, require_role
from app.api.pagination import Cursor, build_page, clamp_limit
from app.db.models import Campaign, Draft, Event, Lead, Mailbox, Message, Research
from app.db.repo import OrgContext
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.services.privacy import erase_lead

router = APIRouter(tags=["campaigns"])

#: Mirrors `ck_campaigns_status`. Checked here so a bad value is a 422 with the allowed list
#: rather than a check-constraint violation surfacing as a 500.
CAMPAIGN_STATUSES = ("draft", "running", "paused", "completed", "archived")


def _field_errors(exc: ValidationError, *, fallback: str) -> list[dict[str, str]]:
    """Pydantic errors, flattened to what a form can display.

    `ctx` is dropped deliberately: it holds the original exception object, which is not
    JSON-serialisable and turned a 422 into a 500 the first time this was written.
    """
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]) or fallback,
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors(include_url=False)
    ]


def _contains(term: str) -> str:
    """A `LIKE` pattern matching `term` anywhere, with its metacharacters neutralised.

    `%` and `_` are wildcards. Unescaped, a search for `%` matches every row — so the one
    character somebody is most likely to type when probing a search box would quietly return
    the whole table. The callers pair this with `escape="\\"`.
    """
    escaped = term.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def campaign_summary(campaign: Campaign) -> dict[str, Any]:
    persona = campaign.persona or {}
    return {
        "id": str(campaign.id),
        "name": campaign.name,
        "status": campaign.status,
        "paused": campaign.paused,
        "mailbox_id": str(campaign.mailbox_id) if campaign.mailbox_id else None,
        # Whether a persona exists is what the UI needs to know; its contents are not list data.
        "has_persona": bool(persona),
        "sender_name": persona.get("sender_name"),
        # Rendered through `CampaignSettings` rather than returned raw, so the client is shown
        # the values the worker will actually use -- defaults included -- instead of whatever
        # subset happens to be stored. A row written before a field existed reads correctly.
        "settings": CampaignSettings.model_validate(campaign.settings or {}).model_dump(
            mode="json"
        ),
        "created_at": campaign.created_at.isoformat(),
    }


def lead_summary(lead: Lead) -> dict[str, Any]:
    """What a list row needs. Deliberately no draft, insights or reply text (C7)."""
    return {
        "id": str(lead.id),
        "email": lead.email_norm,
        "full_name": lead.full_name,
        "company_name": lead.company_name,
        "job_title": lead.job_title,
        "state": lead.state,
        "next_action_at": lead.next_action_at.isoformat() if lead.next_action_at else None,
        "created_at": lead.created_at.isoformat(),
    }


@router.get("/campaigns")
async def list_campaigns(
    user: CurrentUserDep,
    session: SessionDep,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    size = clamp_limit(limit)
    stmt = (
        select(Campaign)
        .where(Campaign.org_id == user.org_id)
        .order_by(Campaign.created_at.desc(), Campaign.id.desc())
        .limit(size + 1)
    )
    position = Cursor.decode(cursor)
    if position is not None:
        stmt = stmt.where(Campaign.created_at < position.created_at)

    rows = list((await session.execute(stmt)).scalars())
    return build_page(rows, size, campaign_summary).body("campaigns")


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> dict[str, Any]:
    campaign = await _owned_campaign(session, user, campaign_id)
    counts = await _state_counts(session, user, campaign_id)
    return {
        **campaign_summary(campaign),
        "persona": campaign.persona,
        "settings": campaign.settings,
        "lead_counts": counts,
    }


@router.get("/campaigns/{campaign_id}/leads")
async def list_leads(
    campaign_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    state: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    email: Annotated[str | None, Query(max_length=320)] = None,
    name: Annotated[str | None, Query(max_length=200)] = None,
    company: Annotated[str | None, Query(max_length=200)] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> dict[str, Any]:
    """A page of leads, narrowed by any combination of filters.

    `q` searches email, name and company together — one box, for when you do not know which
    field the thing you remember is in. `email`, `name` and `company` are separate because a
    reviewer usually does: "Globex" is a company and "Rao" is a person, and a single box cannot
    tell you which it matched.

    **Filters combine as AND**, which is what somebody reading two filled-in boxes expects.

    Every one of them matches identity only. Searching bodies would turn a search box into an
    export of everything anybody has ever written to or about a lead (C7).
    """
    await _owned_campaign(session, user, campaign_id)
    size = clamp_limit(limit)

    stmt = (
        select(Lead)
        .where(Lead.org_id == user.org_id, Lead.campaign_id == campaign_id)
        .order_by(Lead.created_at.desc(), Lead.id.desc())
        .limit(size + 1)
    )
    if state:
        stmt = stmt.where(Lead.state == state)
    if q:
        pattern = _contains(q)
        stmt = stmt.where(
            Lead.email_norm.ilike(pattern, escape="\\")
            | Lead.full_name.ilike(pattern, escape="\\")
            | Lead.company_name.ilike(pattern, escape="\\")
        )
    if email:
        stmt = stmt.where(Lead.email_norm.ilike(_contains(email), escape="\\"))
    if name:
        stmt = stmt.where(Lead.full_name.ilike(_contains(name), escape="\\"))
    if company:
        stmt = stmt.where(Lead.company_name.ilike(_contains(company), escape="\\"))
    position = Cursor.decode(cursor)
    if position is not None:
        stmt = stmt.where(Lead.created_at < position.created_at)

    rows = list((await session.execute(stmt)).scalars())
    return build_page(rows, size, lead_summary).body("leads")


@router.get("/leads/{lead_id}")
async def get_lead(lead_id: uuid.UUID, user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """One lead, with its bodies and its timeline. One lead's worth of exposure at a time."""
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.org_id != user.org_id:
        # 404 rather than 403: confirming that an id exists in another org is itself a leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found.")

    research = await session.scalar(
        select(Research)
        .where(Research.org_id == user.org_id, Research.lead_id == lead.id)
        .order_by(Research.created_at.desc())
        .limit(1)
    )
    drafts = list(
        (
            await session.execute(
                select(Draft)
                .where(Draft.org_id == user.org_id, Draft.lead_id == lead.id)
                .order_by(Draft.created_at.desc())
                .limit(10)
            )
        ).scalars()
    )
    messages = list(
        (
            await session.execute(
                select(Message)
                .where(Message.org_id == user.org_id, Message.lead_id == lead.id)
                .order_by(Message.created_at)
                .limit(50)
            )
        ).scalars()
    )
    timeline = list(
        (
            await session.execute(
                select(Event)
                .where(Event.org_id == user.org_id, Event.lead_id == lead.id)
                .order_by(Event.at.desc())
                .limit(50)
            )
        ).scalars()
    )

    return {
        **lead_summary(lead),
        "notes": lead.notes,
        "linkedin_url": lead.linkedin_url,
        "company_url": lead.company_url,
        "research": {
            "hooks": list(research.hooks or []) if research else [],
            "confidence": research.confidence if research else 0.0,
            "thin": research.thin if research else True,
        },
        "drafts": [
            {
                "id": str(draft.id),
                "kind": draft.kind,
                "version": draft.version,
                "status": draft.status,
                "subject": draft.subject,
                "body": draft.body,
                "created_at": draft.created_at.isoformat(),
            }
            for draft in drafts
        ],
        "messages": [
            {
                "id": str(message.id),
                "direction": message.direction,
                "kind": message.kind,
                "status": message.status,
                "subject": message.subject,
                "body_text": message.body_text,
                "created_at": message.created_at.isoformat(),
            }
            for message in messages
        ],
        "timeline": [
            {
                "type": event.type,
                "at": event.at.isoformat(),
                "actor": event.actor,
                "data": event.data,
            }
            for event in timeline
        ],
    }


async def _owned_campaign(
    session: SessionDep, user: CurrentUserDep, campaign_id: uuid.UUID
) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found.")
    return campaign


async def _state_counts(
    session: SessionDep, user: CurrentUserDep, campaign_id: uuid.UUID
) -> dict[str, int]:
    from sqlalchemy import func

    rows = await session.execute(
        select(Lead.state, func.count())
        .where(Lead.org_id == user.org_id, Lead.campaign_id == campaign_id)
        .group_by(Lead.state)
    )
    return {state: count for state, count in rows.all()}


@router.delete("/leads/{lead_id}", status_code=status.HTTP_200_OK)
async def erase_lead_endpoint(
    lead_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    keep_suppressed: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """Erase one lead and everything personal attached to it (§17, issue D6).

    This is the data-subject request route, and it is a real delete rather than a flag: the
    lead, its drafts, its messages, its research and its events all go.

    **`keep_suppressed` defaults to true and should almost always stay true.** It leaves a
    hashed suppression behind so the address stays unreachable without being stored. Without
    it, erasing somebody who had opted out makes them contactable again on the next import of
    the same sheet — a privacy request producing the exact harm it was meant to prevent.

    Restricted to `admin` and above. It is the one irreversible action in the API, and a
    reviewer who may approve drafts has no reason to need it.
    """
    ctx = OrgContext(org_id=user.org_id, actor_id=user.email or user.user_id)
    try:
        result = await erase_lead(session, ctx, lead_id, keep_suppressed=keep_suppressed)
    except LookupError as exc:
        # 404 rather than 403, as everywhere else: confirming an id exists elsewhere is a leak.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found.") from exc

    return {
        "erased": True,
        "lead_id": str(result.lead_id),
        "rows_removed": result.rows,
        "detail": {
            "drafts": result.drafts,
            "messages": result.messages,
            "research": result.research,
            "events": result.events,
        },
        "suppression_kept": result.suppression_kept,
    }


@router.post("/mailboxes", status_code=status.HTTP_201_CREATED)
async def create_mailbox(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Register the mailbox a campaign sends from.

    **No credentials are stored here.** `secret_ref` names which set of SMTP/IMAP credentials in
    the environment to use, and the credentials themselves stay in `.env` where the operator put
    them. A mailbox row is the *policy* — who it is from, how fast it may send, whether it is
    paused — not the login.

    `from_email` should match the authenticated SMTP account: it is what recipients see and what
    SPF, DKIM and DMARC are published for.
    """
    from_name = str(payload.get("from_name") or "").strip()
    from_email = str(payload.get("from_email") or "").strip().lower()
    if not from_name or not from_email:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "from_name and from_email are both required."
        )
    if "@" not in from_email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "from_email is not an address.")

    # `payload.get("daily_cap") or 100` would turn an explicit 0 into 100 and accept it, because
    # 0 is falsy — and the database's `daily_cap > 0` check would then never be the thing that
    # caught it. Absent means "use the default"; present means "use exactly this".
    raw_cap = payload.get("daily_cap")
    try:
        daily_cap = 100 if raw_cap is None else int(raw_cap)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "daily_cap must be a whole number."
        ) from exc
    if daily_cap < 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "daily_cap must be positive.")

    mailbox = Mailbox(
        org_id=user.org_id,
        from_name=from_name,
        from_email=from_email,
        reply_to=str(payload.get("reply_to") or "").strip().lower() or from_email,
        secret_ref=str(payload.get("secret_ref") or "default"),
        daily_cap=daily_cap,
        paused=False,
    )
    session.add(mailbox)
    await session.commit()
    return {
        "id": str(mailbox.id),
        "from_name": mailbox.from_name,
        "from_email": mailbox.from_email,
        "daily_cap": mailbox.daily_cap,
    }


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Create a campaign, with its sender persona.

    **The persona is validated here, not at drafting time.** It is the list of things a draft is
    allowed to assert about us — everything else must come from cited research about the
    recipient (hard rule 6). A malformed persona discovered while generating would either block
    the lead or, worse, quietly produce a weaker draft, so it is rejected at the door.

    A campaign starts `draft` and paused-by-omission: it has no mailbox until one is attached,
    and `status` only becomes `running` when somebody says so. Nothing is sent as a side effect
    of creating anything.
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required.")

    persona_in = payload.get("persona") or {}
    try:
        persona = Persona.model_validate(persona_in)
    except ValidationError as exc:
        # The field errors are returned: this is the owner's own form, and "persona is invalid"
        # with no indication of which field would make it unusable.
        #
        # Only `loc`, `msg` and `type` are passed on. Pydantic's raw error dicts carry a `ctx`
        # holding the original exception *object*, which is not JSON-serialisable — returning
        # them whole turned a 422 into a 500, hiding the very message the caller needed.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "The persona is not valid.",
                "errors": _field_errors(exc, fallback="persona"),
            },
        ) from exc

    mailbox_id: uuid.UUID | None = None
    if raw_mailbox := payload.get("mailbox_id"):
        try:
            mailbox_id = uuid.UUID(str(raw_mailbox))
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "mailbox_id is not a uuid."
            ) from exc
        owned = await session.scalar(
            select(Mailbox.id).where(Mailbox.id == mailbox_id, Mailbox.org_id == user.org_id)
        )
        if owned is None:
            # 404, as everywhere: a mailbox in another org must look like one that is not there.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")

    campaign = Campaign(
        org_id=user.org_id,
        name=name,
        status="draft",
        mailbox_id=mailbox_id,
        persona=persona.model_dump(mode="json"),
        settings=dict(payload.get("settings") or {}),
        paused=False,
    )
    session.add(campaign)
    await session.commit()
    return campaign_summary(campaign)


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    payload: Annotated[dict[str, Any], Body()],
) -> dict[str, Any]:
    """Change a campaign's name, status, pause flag, mailbox, persona or sending rules.

    **Attaching a mailbox after the fact is the reason this exists.** Creation allowed a campaign
    with no mailbox, and nothing could add one afterwards — so a campaign created without one
    could never send and could never be repaired, only abandoned.

    `status` is not a free field: `running` is what lets the send loop consider this campaign's
    leads, so it is set deliberately and never as a side effect of editing a name.

    `settings` is validated through `CampaignSettings` and merged into what is already stored.
    **`SEND_MODE` is not among them and cannot be set here or anywhere else in the API** -- it
    comes from the environment and is checked in one function (hard rule 1). `GET
    /admin/sending-config` reports it; nothing changes it.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found.")

    if "name" in payload:
        name = str(payload["name"] or "").strip()
        if not name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name cannot be empty.")
        campaign.name = name

    if "status" in payload:
        new_status = str(payload["status"] or "").strip()
        if new_status not in CAMPAIGN_STATUSES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"status must be one of: {', '.join(CAMPAIGN_STATUSES)}.",
            )
        campaign.status = new_status

    if "paused" in payload:
        campaign.paused = bool(payload["paused"])

    if "mailbox_id" in payload:
        raw = payload["mailbox_id"]
        if raw in (None, ""):
            campaign.mailbox_id = None
        else:
            try:
                mailbox_id = uuid.UUID(str(raw))
            except ValueError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY, "mailbox_id is not a uuid."
                ) from exc
            owned = await session.scalar(
                select(Mailbox.id).where(Mailbox.id == mailbox_id, Mailbox.org_id == user.org_id)
            )
            if owned is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found.")
            campaign.mailbox_id = mailbox_id

    if "persona" in payload:
        try:
            campaign.persona = Persona.model_validate(payload["persona"]).model_dump(mode="json")
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "message": "The persona is not valid.",
                    "errors": _field_errors(exc, fallback="persona"),
                },
            ) from exc

    if "settings" in payload:
        # **Validated here so the worker never has to cope with an unreadable row.** `SendLoop`
        # parses `campaigns.settings` on every cycle; until this route existed the only way to
        # change a send window was raw SQL, and one `UPDATE` that put weekday *names* in
        # `send_days` stopped sending for every mailbox with the reason visible only as a
        # traceback in the worker log.
        #
        # **Merged, not replaced.** A form that posts one field must not reset the rest to
        # their defaults -- silently narrowing a send window is exactly the kind of change
        # nobody notices until mail stops going out.
        incoming = payload["settings"]
        if not isinstance(incoming, dict):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "settings must be an object.")
        merged = {**(campaign.settings or {}), **incoming}
        try:
            validated = CampaignSettings.model_validate(merged)
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "message": "These settings are not valid.",
                    "errors": _field_errors(exc, fallback="settings"),
                },
            ) from exc
        # Stored fully rendered, so the row is always complete and always parseable. `SEND_MODE`
        # is not in this model and cannot arrive this way: an unknown key is rejected by
        # `extra="forbid"` above.
        campaign.settings = validated.model_dump(mode="json")

    await session.commit()
    return campaign_summary(campaign)


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(
    campaign_id: uuid.UUID,
    session: SessionDep,
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Delete a campaign.

    **A campaign that has sent anything is archived instead of deleted, unless `force`.** Its
    messages are the record of what left the building, and its leads' suppressions and opt-outs
    hang off that history. Quietly destroying it to tidy a list would erase the evidence that
    somebody was contacted — and, worse, the reason they must not be contacted again.

    An untouched campaign — created by mistake, or to try the form — is simply deleted, together
    with any leads that never went anywhere.

    `force=true` deletes regardless, and each lead goes through the erasure path, so every
    opted-out address is preserved as a hash. That is the one way this is safe to allow.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found.")

    sent = await session.scalar(
        select(func.count())
        .select_from(Message)
        .join(Lead, Message.lead_id == Lead.id)
        .where(Lead.campaign_id == campaign_id, Message.direction == "out")
    )
    sent = int(sent or 0)

    if sent and not force:
        campaign.status = "archived"
        campaign.paused = True
        await session.commit()
        return {
            "deleted": False,
            "archived": True,
            "messages_sent": sent,
            "detail": (
                f"This campaign has sent {sent} message(s), so it was archived rather than "
                "deleted: its history is the record of who was contacted. Pass force=true to "
                "delete it anyway — every lead is erased through the privacy path, so opt-outs "
                "survive as hashes."
            ),
        }

    ctx = OrgContext(org_id=user.org_id, actor_id=user.email or user.user_id)
    lead_ids = list(
        (await session.execute(select(Lead.id).where(Lead.campaign_id == campaign_id))).scalars()
    )
    for lead_id in lead_ids:
        # Through the erasure path, not a bare delete: it is what preserves the hashed
        # suppression for anyone who had opted out.
        await erase_lead(session, ctx, lead_id)

    await session.delete(campaign)
    await session.commit()
    return {"deleted": True, "archived": False, "leads_erased": len(lead_ids)}
