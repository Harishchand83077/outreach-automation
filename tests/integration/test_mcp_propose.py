"""Proposing a draft over MCP (ADR-030), against a real database.

Phase 3 of `docs/MCP_PLAN.md` is the first MCP tool that **writes**, and its whole safety
argument is one sentence: a proposal is the same `Draft` row the in-app drafter creates,
`pending_review`, through the same validator -- so it does not bypass human review, it enters it.

Every test here exists to hold one clause of that sentence down:

1. **The scope gate.** A read-only credential cannot reach a write, and is not even shown it.
2. **The status.** A proposal lands where a human still has to judge it, and nothing in the
   write path can move a lead or approve anything.
3. **The validator is not relaxed.** Text from somebody else's model is not text an operator
   typed, so it goes through `validate_draft` -- invention checks and all -- and not through the
   narrower `validate_rendered`.
4. **The lead's state must already admit a review.** Four times in this project a draft has
   been written and then been unapprovable because the state machine refused the transition. A
   proposal for a lead in the wrong state is refused *before* the row is written.
5. **A reply proposal obeys the same policy as an in-app one**, so a hijacked agent cannot
   write back to somebody who told us to stop.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app.db import drafts as drafts_repo
from app.db.models import Campaign, Classification, Draft, Event, Lead, Member, Message, Org
from app.db.repo import OrgContext
from app.domain.reply import ReplyLabel, policy_for
from app.services import api_tokens
from app.services.mcp import protocol, tools

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401
from .test_mcp_endpoint import MCP, _body, _call, _headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: A persona with a proof point, so a grounded claim has something to resolve against. The
#: shared `campaign` fixture deliberately has none.
PERSONA = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Arkvane",
    "offer": "a two-week prototype",
    "goal": "book a 20-minute call",
    "cta_text": "Worth a 20-minute call next week?",
    "proof_points": [
        {"id": "pp1", "text": "we build two-week prototypes for seed-stage teams"},
    ],
}

GOOD_BODY = (
    "Hi there,\n\n"
    "We build two-week prototypes for seed-stage teams, which is the sort of thing that "
    "might suit what you are doing.\n\n"
    "Worth a 20-minute call next week?\n\n"
    "Asha"
)

GOOD_SUBJECT = "Two-week prototypes"


async def _token(db, org, *, scopes: list[str] | None = None, user_id: str | None = None) -> str:
    subject = user_id or f"mcp-propose-{uuid.uuid4()}"
    existing = await db.scalar(select(Member).where(Member.user_id == subject))
    if existing is None:
        db.add(Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin"))
    minted = await api_tokens.create_token(
        db, org_id=org.id, user_id=subject, name="Test", scopes=scopes
    )
    await db.commit()
    return minted.secret


async def _proposable_campaign(db, org) -> Campaign:
    record = Campaign(org_id=org.id, name="Proposable", status="running", persona=PERSONA)
    db.add(record)
    await db.commit()
    return record


async def _lead_in(db, org, campaign, state: str, **kw) -> Lead:
    record = Lead(
        org_id=org.id,
        campaign_id=campaign.id,
        state=state,
        email_norm=kw.pop("email", f"propose-{uuid.uuid4().hex[:8]}@example.com"),
        email_raw="Propose@Example.com",
        full_name="Dev Patel",
        company_name="Acme",
        **kw,
    )
    db.add(record)
    await db.commit()
    return record


async def _draft_count(db, lead_id) -> int:
    return int(await db.scalar(select(func.count(Draft.id)).where(Draft.lead_id == lead_id)) or 0)


async def _latest_draft(db, lead_id) -> Draft | None:
    return await db.scalar(
        select(Draft).where(Draft.lead_id == lead_id).order_by(Draft.version.desc()).limit(1)
    )


def _propose(api, token, lead, **kw):
    arguments = {"lead_id": str(lead.id), "subject": GOOD_SUBJECT, "body": GOOD_BODY}
    arguments.update(kw)
    return _call(api, token, "propose_draft", arguments)


class TestTheScopeGate:
    """A read-only credential must not reach a write, and must not be offered one."""

    async def test_a_read_only_token_cannot_propose_and_writes_nothing(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE])

        response = _propose(api, token, lead)

        assert response.status_code == 200
        assert response.json()["result"]["isError"] is True
        assert await _draft_count(db, lead.id) == 0

    async def test_a_read_only_token_is_not_even_shown_the_write_tools(self, api, db, org):
        """Offering a tool the credential cannot use makes a model try it and fail.

        The list is `cacheScope: private` already, which is what makes a per-credential
        catalogue safe to serve.
        """
        token = await _token(db, org, scopes=[protocol.READ_SCOPE])

        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
        )

        names = [tool["name"] for tool in response.json()["result"]["tools"]]
        assert "list_campaigns" in names
        assert not [name for name in names if name.startswith("propose_")]

    async def test_a_propose_token_keeps_every_read_tool_too(self, api, db, org):
        """Proposing is additive. An agent that could only write would be useless and odd."""
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
        )

        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert {"list_campaigns", "get_lead", "propose_draft", "propose_reply"} <= names

    async def test_the_scope_list_refuses_one_nobody_has_heard_of(self, db, org):
        """The same device as `ck_orgs_send_mode`, one layer earlier.

        The database has the final say with a check constraint; this refuses it before the
        INSERT, so the error names the scope rather than being a constraint violation.
        """
        with pytest.raises(ValueError):
            await api_tokens.create_token(
                db, org_id=org.id, user_id="scope-probe", name="n", scopes=["mcp:send"]
            )


class TestAProposalEntersReviewRatherThanBypassingIt:
    async def test_a_grounded_proposal_is_stored_pending_review(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(
            api,
            token,
            lead,
            claims=[
                {
                    "text": "we build two-week prototypes for seed-stage teams",
                    "evidence_ref": "pp1",
                }
            ],
        )

        assert response.status_code == 200, response.text
        assert response.json()["result"]["isError"] is False

        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        assert draft.status == "pending_review"
        assert draft.created_by.startswith("mcp:")
        assert draft.subject == GOOD_SUBJECT

    async def test_a_proposal_does_not_move_the_lead(self, api, db, org):
        """The write path cannot transition anything, which is why it needs no new states.

        Approving the proposal is the same click as approving an in-app draft, taking the same
        path through `drafts.approve` -- so a proposal adds no transition the state machine has
        not already been permitting or refusing for the whole project.
        """
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        _propose(api, token, lead)

        await db.refresh(lead)
        assert lead.state == "AWAITING_APPROVAL"

    async def test_a_proposal_can_then_be_approved_through_the_normal_path(self, api, db, org):
        """The claim ADR-030 rests on: it enters review rather than bypassing it.

        If `drafts.approve` cannot accept what the proposal wrote, the feature is the fifth
        instance of this project's write-then-fail-to-transition defect.
        """
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])
        _propose(api, token, lead)
        draft = await _latest_draft(db, lead.id)
        assert draft is not None

        approved = await drafts_repo.approve(
            db, OrgContext(org_id=org.id), draft_id=draft.id, approved_by="a-human"
        )
        await db.commit()

        assert approved.status == "approved"
        await db.refresh(lead)
        assert lead.state == "APPROVED"

    async def test_a_proposal_is_judgeable(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        _propose(api, token, lead)

        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        assert draft.status in drafts_repo.JUDGEABLE_STATUSES


class TestTheValidatorIsNotRelaxedForAnAgent:
    """The decision at the heart of ADR-030.

    `validate_rendered` is deliberately narrower than `validate_draft` because an operator
    typing their own email is the authority on their own offer (ADR-012). **A model is not**, and
    a model that has just read a lead's reply may be repeating an instruction that reply planted.
    So a proposal goes through the full invention-catching set.
    """

    async def test_an_invented_statistic_is_stored_validation_failed(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(
            api,
            token,
            lead,
            body=(
                "Hi there,\n\nWe have increased revenue by 42% for 173 seed-stage teams.\n\n"
                "Worth a 20-minute call next week?\n\nAsha"
            ),
        )

        assert response.status_code == 200
        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        assert draft.status == "validation_failed"
        # The reasons travel with the row, so a reviewer sees what the check objected to.
        assert draft.validation["failures"]
        # And they are said back to the agent, which is the one party that can correct them.
        assert response.json()["result"]["structuredContent"]["accepted"] is False

    async def test_a_claim_citing_evidence_that_does_not_exist_fails(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        _propose(
            api,
            token,
            lead,
            claims=[{"text": "we did this for a household name", "evidence_ref": "pp9"}],
        )

        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        assert draft.status == "validation_failed"

    async def test_a_failed_proposal_cannot_be_approved_as_it_stands(self, api, db, org):
        """The existing rule, holding for a proposal too: approving a failed draft needs an edit.

        That is what makes storing a failed proposal safe rather than merely informative.
        """
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])
        _propose(
            api,
            token,
            lead,
            body="Hi,\n\nWe lifted revenue 42% for 173 teams.\n\nCall?\n\nAsha",
        )
        draft = await _latest_draft(db, lead.id)
        assert draft is not None and draft.status == "validation_failed"

        with pytest.raises(ValueError):
            await drafts_repo.approve(
                db, OrgContext(org_id=org.id), draft_id=draft.id, approved_by="a-human"
            )


class TestTheLeadStateMustAlreadyAdmitAReview:
    """Refused before the row is written, not after.

    Four times in this project a draft has been written and then found to be unapprovable
    because the transition was illegal -- each time after the model had been paid for. A
    proposal for a lead in a state the approval path cannot handle is refused up front, and the
    refusal names the state so the agent can say something useful.
    """

    async def test_a_lead_that_has_not_been_drafted_yet_is_refused(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "NEW")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(api, token, lead)

        result = response.json()["result"]
        assert result["isError"] is True
        assert "NEW" in result["content"][0]["text"]
        assert await _draft_count(db, lead.id) == 0

    async def test_every_permitted_state_really_is_approvable(self, api, db, org):
        """The agreement test, rather than two copies of one list.

        `PROPOSABLE_FROM` has to match what `drafts.approve` can actually do. Restating the
        states in a second place is this project's signature defect; asserting the two agree is
        the cure.
        """
        campaign = await _proposable_campaign(db, org)
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        for state in sorted(drafts_repo.PROPOSABLE_FROM["initial"]):
            lead = await _lead_in(db, org, campaign, state)

            response = _propose(api, token, lead)
            assert response.json()["result"]["isError"] is False, state

            draft = await _latest_draft(db, lead.id)
            assert draft is not None
            await drafts_repo.approve(
                db, OrgContext(org_id=org.id), draft_id=draft.id, approved_by="a-human"
            )
            await db.commit()


class TestAFollowUpContinuesTheThread:
    """Taken from the thread rather than checked, which is the stronger of the two.

    `validate_draft` would catch a follow-up subject that does not continue the thread, so this
    is not the only defence. But a refusal tells the agent to guess again at a field whose value
    is already decided, and a follow-up filed as a new conversation is a defect this project has
    already fixed once by deciding the subject at drafting time.
    """

    async def test_the_agents_subject_is_ignored_for_a_followup(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "SENT")
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                kind="initial",
                subject="Two-week prototypes",
                body_text="the first email",
                status="sent",
                # `ck_messages_out_has_idempotency_key`: an outbound row must carry one, which
                # is what makes at-most-once sending a database property rather than a habit.
                idempotency_key=f"test-{uuid.uuid4()}",
            )
        )
        await db.commit()
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(
            api,
            token,
            lead,
            kind="followup",
            subject="A COMPLETELY DIFFERENT SUBJECT",
            body="Hi there,\n\nJust following up on the below.\n\nAsha",
        )

        assert response.json()["result"]["isError"] is False, response.text
        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        assert draft.kind == "followup"
        assert draft.subject == "Re: Two-week prototypes"

    async def test_a_followup_with_nothing_to_follow_up_is_refused(self, api, db, org):
        """`SENT` with no outbound message is a data oddity, and guessing a subject is worse."""
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "SENT")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(api, token, lead, kind="followup")

        assert response.json()["result"]["isError"] is True
        assert await _draft_count(db, lead.id) == 0


class TestAReplyProposalObeysTheSamePolicy:
    """A hijacked agent must not be able to write back to somebody who told us to stop."""

    async def _replied_lead(self, db, org, campaign, label: str):
        lead = await _lead_in(db, org, campaign, "REPLIED")
        message = Message(
            org_id=org.id,
            lead_id=lead.id,
            direction="in",
            subject="Re: Two-week prototypes",
            body_text="Please take me off your list.",
            status="received",
        )
        db.add(message)
        await db.commit()
        db.add(
            Classification(
                org_id=org.id,
                message_id=message.id,
                lead_id=lead.id,
                label=label,
                confidence=0.9,
            )
        )
        await db.commit()
        return lead, message

    async def test_every_label_whose_policy_forbids_a_draft_is_refused(self, api, db, org):
        """Read from the policy table, so a label added later is covered automatically.

        `hostile` is the one that matters most: a machine-written reply to somebody threatening
        to report us as spam is how a complaint becomes a listing.

        One test looping the labels rather than a parametrised one, which is how the state
        agreement above is written too: each label needs its own lead and its own classified
        message, and building them inside the loop keeps that setup next to the assertion
        instead of spread across one fixture teardown per label.
        """
        campaign = await _proposable_campaign(db, org)
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])
        refusing = [label for label in ReplyLabel if not policy_for(label).draft_reply]
        assert refusing, "no label forbids a draft; has the policy table changed?"

        for label in refusing:
            lead, _ = await self._replied_lead(db, org, campaign, label.value)

            response = _call(
                api,
                token,
                "propose_reply",
                {"lead_id": str(lead.id), "body": "Thanks for getting back to me."},
            )

            assert response.json()["result"]["isError"] is True, label
            assert await _draft_count(db, lead.id) == 0, label

    async def test_a_reply_proposal_cannot_choose_its_own_subject(self, api, db, org):
        """Threading is ours. A model that gets creative with the subject breaks the thread.

        There is no `subject` argument at all, so the surface does not offer the mistake.
        """
        campaign = await _proposable_campaign(db, org)
        lead, _ = await self._replied_lead(db, org, campaign, ReplyLabel.QUESTION.value)
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        tool = tools.BY_NAME["propose_reply"]
        assert "subject" not in tool.input_schema.get("properties", {})

        response = _call(
            api,
            token,
            "propose_reply",
            {
                "lead_id": str(lead.id),
                "body": (
                    "Hi there,\n\nWe build two-week prototypes for seed-stage teams.\n\n"
                    "Worth a 20-minute call next week?\n\nAsha"
                ),
            },
        )

        assert response.json()["result"]["isError"] is False, response.text
        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        assert draft.kind == "reply"
        assert draft.subject == "Re: Two-week prototypes"


class TestAProposalCannotClobberADraftItHasNotRead:
    """Optimistic concurrency on the review queue.

    `save_draft` supersedes the lead's previous live draft, which is right for a regeneration
    and wrong for a proposal arriving from outside: it means the draft a reviewer has open can
    be replaced under them, and the one they approve is not the one they read. So a proposal has
    to **name** the draft it replaces, and the only way to know that id is to have read the lead.

    Three rules, each its own refusal:

    1. A live draft exists and no `replaces_draft_id` was given — refused, naming it.
    2. The id given is not the current one — refused as stale, so two agents cannot race and a
       re-run against a changed queue cannot clobber.
    3. The current draft is a person's own work, or already approved — refused outright. Your
       words are not an agent's to overwrite, and an approved draft is queued to send.
    """

    async def _lead_with_a_pending_draft(self, api, db, org, token):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        response = _propose(api, token, lead)
        assert response.json()["result"]["isError"] is False, response.text
        draft = await _latest_draft(db, lead.id)
        assert draft is not None
        return lead, draft

    async def test_a_second_proposal_without_an_id_is_refused(self, api, db, org):
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])
        lead, first = await self._lead_with_a_pending_draft(api, db, org, token)

        response = _propose(api, token, lead, body=GOOD_BODY + "\n\nPS. a second go.")

        result = response.json()["result"]
        assert result["isError"] is True
        # The refusal has to name what it would have replaced, or the agent cannot comply.
        assert str(first.id) in result["content"][0]["text"]
        assert await _draft_count(db, lead.id) == 1

    async def test_naming_the_current_draft_replaces_it(self, api, db, org):
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])
        lead, first = await self._lead_with_a_pending_draft(api, db, org, token)

        response = _propose(
            api,
            token,
            lead,
            replaces_draft_id=str(first.id),
            body=GOOD_BODY + "\n\nPS. a better go.",
        )

        assert response.json()["result"]["isError"] is False, response.text
        await db.refresh(first)
        assert first.status == "superseded"
        latest = await _latest_draft(db, lead.id)
        assert latest is not None and latest.status == "pending_review"
        assert latest.version == first.version + 1

    async def test_a_stale_id_is_refused(self, api, db, org):
        """The property that makes this concurrency control rather than a speed bump."""
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])
        lead, first = await self._lead_with_a_pending_draft(api, db, org, token)
        _propose(api, token, lead, replaces_draft_id=str(first.id), body=GOOD_BODY + "\n\nv2")
        second = await _latest_draft(db, lead.id)
        assert second is not None and second.id != first.id

        # Replaying the first id, as a re-run against a queue that has moved on would.
        response = _propose(
            api, token, lead, replaces_draft_id=str(first.id), body=GOOD_BODY + "\n\nv3"
        )

        result = response.json()["result"]
        assert result["isError"] is True
        assert str(second.id) in result["content"][0]["text"]
        await db.refresh(second)
        assert second.status == "pending_review"

    async def test_an_id_for_a_lead_with_no_draft_is_refused(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(api, token, lead, replaces_draft_id=str(uuid.uuid4()))

        assert response.json()["result"]["isError"] is True
        assert await _draft_count(db, lead.id) == 0

    async def test_a_draft_a_person_wrote_is_never_replaced(self, api, db, org):
        """Your words are not an agent's to overwrite, whatever id it names."""
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        human = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            version=1,
            subject="Written by a person",
            body="I wrote this myself.",
            status="pending_review",
            created_by="asha@example.com",
        )
        db.add(human)
        await db.commit()
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(api, token, lead, replaces_draft_id=str(human.id))

        assert response.json()["result"]["isError"] is True
        await db.refresh(human)
        assert human.status == "pending_review"
        assert await _draft_count(db, lead.id) == 1

    async def test_an_approved_draft_is_never_replaced(self, api, db, org):
        """A person has decided; it is queued to send. Not an agent's call to undo.

        **The inbound message and its classification are set up deliberately**, so that the
        approved draft is the *only* thing this proposal could be refused for. Without them the
        call is refused for having no reply to answer, the assertion passes, and the test proves
        nothing — which was true of the first version of it, and is the same trap as
        `test_a_message_still_sending_is_not_counted_as_sent`.
        """
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "REPLIED")
        message = Message(
            org_id=org.id,
            lead_id=lead.id,
            direction="in",
            subject="Re: Two-week prototypes",
            body_text="Sounds interesting, what would it cost?",
            status="received",
        )
        db.add(message)
        await db.commit()
        db.add(
            Classification(
                org_id=org.id,
                message_id=message.id,
                lead_id=lead.id,
                label=ReplyLabel.QUESTION.value,
                confidence=0.9,
            )
        )
        await db.commit()
        approved = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="reply",
            version=1,
            subject="Re: Two-week prototypes",
            body="Our answer, approved.",
            status="approved",
            created_by="system",
            approved_by="asha@example.com",
        )
        db.add(approved)
        await db.commit()
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _call(
            api,
            token,
            "propose_reply",
            {
                "lead_id": str(lead.id),
                "body": "A different answer.",
                "replaces_draft_id": str(approved.id),
            },
        )

        assert response.json()["result"]["isError"] is True
        await db.refresh(approved)
        assert approved.status == "approved"


class TestThereIsALimitOnProposalsPerLead:
    """The same reasoning as `MAX_REJECTIONS`, which exists for the same failure.

    A loop costs a day of somebody's model quota on one lead and buries the queue. The cap is
    per lead per day, so a legitimate few rounds of rewriting in one sitting is fine and nobody
    is permanently stuck, and the refusal says what to do instead rather than declining silently.
    """

    async def test_the_cap_refuses_the_next_proposal_and_says_so(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        previous: Draft | None = None
        for attempt in range(drafts_repo.PROPOSAL_DAILY_LIMIT):
            arguments = {"body": f"{GOOD_BODY}\n\nPS. attempt {attempt}."}
            if previous is not None:
                arguments["replaces_draft_id"] = str(previous.id)
            response = _propose(api, token, lead, **arguments)
            assert response.json()["result"]["isError"] is False, (attempt, response.text)
            previous = await _latest_draft(db, lead.id)

        assert previous is not None
        response = _propose(
            api,
            token,
            lead,
            replaces_draft_id=str(previous.id),
            body=f"{GOOD_BODY}\n\nPS. one too many.",
        )

        result = response.json()["result"]
        assert result["isError"] is True
        assert str(drafts_repo.PROPOSAL_DAILY_LIMIT) in result["content"][0]["text"]
        assert await _draft_count(db, lead.id) == drafts_repo.PROPOSAL_DAILY_LIMIT

    async def test_a_drafts_written_by_the_system_do_not_count_against_it(self, api, db, org):
        """The cap is on what the agent did, not on how busy the lead has been.

        Our own drafter writes `system`, and counting those would mean a lead the pipeline had
        regenerated a few times could not be proposed for at all.
        """
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        for version in range(1, drafts_repo.PROPOSAL_DAILY_LIMIT + 2):
            db.add(
                Draft(
                    org_id=org.id,
                    lead_id=lead.id,
                    kind="initial",
                    version=version,
                    subject="ours",
                    body="written by the pipeline",
                    status="superseded",
                    created_by="system",
                )
            )
        await db.commit()
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(api, token, lead)

        assert response.json()["result"]["isError"] is False, response.text


class TestTenancyAndAudit:
    async def test_a_lead_from_another_workspace_resolves_to_nothing(self, api, db, org):
        """Not a 403, which would confirm the id exists. ADR-004, asserted on the write path."""
        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()
        other_campaign = Campaign(org_id=other.id, name="Theirs", status="running", persona=PERSONA)
        db.add(other_campaign)
        await db.commit()
        their_lead = Lead(
            org_id=other.id,
            campaign_id=other_campaign.id,
            state="AWAITING_APPROVAL",
            email_norm=f"theirs-{uuid.uuid4().hex[:8]}@example.com",
            email_raw="Theirs@Example.com",
        )
        db.add(their_lead)
        await db.commit()
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        response = _propose(api, token, their_lead)

        assert response.json()["result"]["isError"] is True
        assert await _draft_count(db, their_lead.id) == 0

    async def test_a_proposal_leaves_an_audit_row(self, api, db, org):
        campaign = await _proposable_campaign(db, org)
        lead = await _lead_in(db, org, campaign, "AWAITING_APPROVAL")
        token = await _token(db, org, scopes=[protocol.READ_SCOPE, protocol.PROPOSE_SCOPE])

        _propose(api, token, lead)

        events = list(
            (
                await db.scalars(
                    select(Event).where(Event.org_id == org.id, Event.type == "mcp.tool_called")
                )
            ).all()
        )
        assert any(event.data.get("tool") == "propose_draft" for event in events)
