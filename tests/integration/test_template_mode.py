"""A `template`-mode campaign, end to end: import, render, review queue -- and no model.

The research drafter and this one hand back the same `DraftResult`, which is what lets a rendered
email travel the path a researched one does: stored as a draft row, approved by a person, sent by
the send loop under the same cap, window and suppression check. These tests assert both halves of
that -- that the path is the same, and that the model is never asked.

The failure these exist to prevent is a mode flag that lies: a campaign set to `template` that
quietly researches the company and asks a model to write the email anyway.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import Campaign, Draft, Job, Lead
from app.db.queue import JobKind
from app.domain.persona import Persona
from app.domain.state import LeadState, is_allowed
from app.domain.template import EmailTemplate
from app.services.drafting.from_template import draft_from_template

pytestmark = pytest.mark.asyncio(loop_scope="session")

PERSONA = Persona(
    sender_name="Asha Rao",
    sender_role="Founder",
    agency_name="Arkvane",
    offer="a two-week prototype",
    goal="book a 20-minute call",
    booking_url="https://cal.example.com/asha",
)

TEMPLATES = {
    "initial": {
        "subject": "{{company}} and a prototype",
        "body": (
            "Hi {{first_name|there}},\n\n"
            "I build data pipelines for logistics companies.\n\n"
            "Worth a short call?\n\n"
            "{{sender_name}}"
        ),
    },
    "followups": [
        {"subject": "Re: {{company}}", "body": "Following up.\n\n{{sender_name}}", "after_days": 3}
    ],
}


@pytest_asyncio.fixture(loop_scope="session")
async def template_campaign(db, org):
    campaign = Campaign(
        org_id=org.id,
        name="Template campaign",
        status="running",
        mode="template",
        persona=PERSONA.model_dump(mode="json"),
        settings={},
        templates=TEMPLATES,
    )
    db.add(campaign)
    await db.commit()
    return campaign


class TestRenderingOneLead:
    def test_a_complete_lead_renders(self):
        result = draft_from_template(
            template=EmailTemplate.model_validate(TEMPLATES["initial"]),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
            full_name="Sanket Yadav",
            email="sanket@zenith.example",
            company_name="Zenith Logistics",
        )
        assert result.accepted
        assert result.draft is not None
        assert result.draft.subject == "Zenith Logistics and a prototype"
        assert result.draft.body.startswith("Hi Sanket,")
        assert result.draft.body.endswith("Asha Rao")
        assert result.prompt_version == "template-v1"
        assert result.claims_checked is False, (
            "no claim-check model is called, and the stored row must not say one was"
        )

    def test_a_missing_field_is_not_accepted_and_says_which(self):
        """The whole point. `Hi {{first_name}},` reaching a recipient is the most recognisable
        failure of mail-merge, so the draft is stored, visible, and unsendable."""
        result = draft_from_template(
            template=EmailTemplate(subject="{{company}}", body="Hi {{first_name}},"),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
            full_name=None,
            company_name=None,
        )
        assert not result.accepted
        assert result.draft is None, "an unrendered draft must not be sendable"
        failures = " ".join(result.attempts[0].report.failures)
        assert "company" in failures
        assert "first_name" in failures

    def test_a_fallback_rescues_a_thin_row(self):
        result = draft_from_template(
            template=EmailTemplate(
                subject="A question", body="Hi {{first_name|there}},\n\nShort note."
            ),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
            full_name=None,
        )
        assert result.accepted
        assert result.draft is not None
        assert "Hi there," in result.draft.body

    def test_a_spam_trigger_in_the_operators_own_text_still_fails(self):
        """The rules about *invention* do not apply to text a person typed. The rules about
        deliverability apply to everybody."""
        result = draft_from_template(
            template=EmailTemplate(subject="A question", body="Act now, {{first_name|there}}."),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
        )
        assert not result.accepted
        assert "act now" in " ".join(result.attempts[0].report.failures)

    def test_a_second_link_is_refused(self):
        result = draft_from_template(
            template=EmailTemplate(
                subject="A question",
                body="Hi,\n\nhttps://cal.example.com/asha and https://other.example/x",
            ),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
        )
        assert not result.accepted

    def test_a_long_body_is_a_warning_not_a_failure(self):
        """`max_words` is advice to a model. An operator who writes 200 words has decided to."""
        result = draft_from_template(
            template=EmailTemplate(subject="A question", body="word " * 200),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
        )
        assert result.accepted
        assert any("words" in w for w in result.attempts[0].report.warnings)

    def test_a_sheet_column_fills_a_placeholder(self):
        result = draft_from_template(
            template=EmailTemplate(subject="A question", body="You work in {{industry}}."),
            persona=PERSONA,
            lead_id=__import__("uuid").uuid4(),
            extra={"Industry": "logistics"},
        )
        assert result.accepted
        assert result.draft is not None
        assert "You work in logistics." in result.draft.body


class TestTheStateMachineAllowsIt:
    def test_a_template_lead_goes_straight_to_drafted(self):
        """There is nothing to research, and routing it through ENRICHING and RESEARCHED to keep
        the path uniform would record research that never happened."""
        assert is_allowed(LeadState.NEW, LeadState.DRAFTED)

    def test_the_research_path_is_unchanged(self):
        assert is_allowed(LeadState.NEW, LeadState.ENRICHING)
        assert is_allowed(LeadState.ENRICHING, LeadState.RESEARCHED)
        assert is_allowed(LeadState.RESEARCHED, LeadState.DRAFTED)

    def test_drafted_still_cannot_skip_approval(self):
        """A template-mode draft is still approved by a person. `approval_mode` is
        `review_all`, and nothing about writing the email differently changes that."""
        assert not is_allowed(LeadState.DRAFTED, LeadState.SENT)
        assert not is_allowed(LeadState.DRAFTED, LeadState.APPROVED)


class TestTheHandlerRendersWithoutAModel:
    async def test_a_lead_reaches_the_review_queue_and_no_model_is_called(
        self, db, org, template_campaign, session_factory, monkeypatch
    ):
        from app.core.settings import config
        from app.db import queue
        from app.worker import handlers
        from app.worker.context import WorkerContext

        lead = Lead(
            org_id=org.id,
            campaign_id=template_campaign.id,
            email_norm="sanket@zenith.example",
            email_raw="sanket@zenith.example",
            full_name="Sanket Yadav",
            company_name="Zenith Logistics",
            state="NEW",
        )
        db.add(lead)
        await db.flush()
        lead_id = lead.id
        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_LEAD,
            lead_id=lead_id,
            payload={"lead_id": str(lead_id)},
        )
        await db.commit()
        assert job is not None

        def no_router(*args, **kwargs):
            raise AssertionError("a template-mode campaign must not call a model")

        ctx = WorkerContext(
            session_factory=session_factory,
            settings=config,
            poller=object(),  # type: ignore[arg-type]
            sender=object(),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(ctx, "router_for", no_router, raising=False)

        async with session_factory() as session, session.begin():
            claimed = await session.get(Job, job.id)
            assert claimed is not None
            await handlers.handle_draft_lead(ctx, session, claimed)

        async with session_factory() as session:
            draft = await session.scalar(select(Draft).where(Draft.lead_id == lead_id))
            assert draft is not None
            assert draft.subject == "Zenith Logistics and a prototype"
            assert draft.body.startswith("Hi Sanket,")
            assert draft.status == "pending_review"

            refreshed = await session.get(Lead, lead_id)
            assert refreshed is not None
            assert refreshed.state == LeadState.AWAITING_APPROVAL.value

    async def test_an_unreadable_template_fails_the_job_and_leaves_the_lead_recoverable(
        self, db, org, session_factory, monkeypatch
    ):
        """The route refuses to create such a campaign, so this means somebody edited the row --
        the way a hand-edited `send_days` once stopped sending for every mailbox.

        Failing is the deliberate choice over the two alternatives. Skipping the lead reaches a
        terminal state, so fixing the template later would mean hand-recovering every lead it
        touched. Storing an empty failed draft puts a row in front of a reviewer that no editing
        can rescue. Failing retries, eventually goes `dead`, and the scheduler already alerts on
        dead jobs -- while the lead stays `NEW` and needs only a re-queue."""
        from app.core.settings import config
        from app.db import queue
        from app.worker import handlers
        from app.worker.context import WorkerContext

        campaign = Campaign(
            org_id=org.id,
            name="Hand-edited",
            status="running",
            mode="template",
            persona=PERSONA.model_dump(mode="json"),
            templates={"initial": {"subject": "Hi"}},  # no body
        )
        db.add(campaign)
        await db.flush()
        lead = Lead(
            org_id=org.id,
            campaign_id=campaign.id,
            email_norm="broken@example.com",
            email_raw="broken@example.com",
            full_name="Broken Row",
            state="NEW",
        )
        db.add(lead)
        await db.flush()
        lead_id = lead.id
        job = await queue.enqueue(
            db,
            org_id=org.id,
            kind=JobKind.DRAFT_LEAD,
            lead_id=lead_id,
            payload={"lead_id": str(lead_id)},
        )
        await db.commit()
        assert job is not None

        ctx = WorkerContext(
            session_factory=session_factory,
            settings=config,
            poller=object(),  # type: ignore[arg-type]
            sender=object(),  # type: ignore[arg-type]
        )

        with pytest.raises(ValueError, match="not readable"):
            async with session_factory() as session, session.begin():
                claimed = await session.get(Job, job.id)
                assert claimed is not None
                await handlers.handle_draft_lead(ctx, session, claimed)

        async with session_factory() as session:
            refreshed = await session.get(Lead, lead_id)
            assert refreshed is not None
            assert refreshed.state == "NEW", (
                "the lead must stay re-queueable once the template is fixed"
            )
            draft = await session.scalar(select(Draft).where(Draft.lead_id == lead_id))
            assert draft is None, "no unsalvageable row is put in front of a reviewer"

    async def test_the_failure_names_the_field_and_never_the_stored_text(
        self, db, org, session_factory
    ):
        """The message reaches the logs, so it names which field is wrong and never what is in
        it -- the same rule the send loop's per-mailbox reasons follow."""
        from app.db.models import Campaign as C
        from app.worker import handlers

        campaign = C(
            org_id=org.id,
            name="Named fields",
            status="running",
            mode="template",
            persona=PERSONA.model_dump(mode="json"),
            templates={"initial": {"subject": "Hi", "body": "x" * 9000}},
        )
        lead = Lead(
            org_id=org.id,
            campaign_id=campaign.id,
            email_norm="x@example.com",
            email_raw="x@example.com",
            state="NEW",
        )
        with pytest.raises(ValueError) as caught:
            handlers._render_for(campaign, lead, PERSONA)
        assert "initial.body" in str(caught.value)
        assert "xxxx" not in str(caught.value), "a reason must never echo stored content"
