"""Draft persistence and the review queue, against a real database.

Two things need a database: that an edit creates a new version rather than overwriting (the
prototype kept one mutable column, so an edit erased what the model actually produced — issue
E3), and that approving moves the lead through the state machine in the same transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db import drafts as drafts_repo
from app.db.models import Draft, Event, Research
from app.db.repo import OrgContext
from app.services.drafting.generator import DraftAttempt, DraftResult
from app.services.drafting.schema import DraftOutput
from app.services.drafting.validate import ValidationReport

pytestmark = pytest.mark.asyncio(loop_scope="session")

GOOD_DRAFT = DraftOutput.model_validate(
    {
        "subject": "Your pipeline hiring",
        "body": (
            "Hi Ben,\n\nI saw you are hiring backend engineers for your data pipeline.\n\n"
            "We build fixed-scope pipeline projects to a deadline.\n\n"
            "Worth a short call? https://cal.example.com/asha\n\nAsha"
        ),
        "claims": [{"text": "They are hiring", "evidence_ref": "hook:111"}],
    }
)


def passing_result() -> DraftResult:
    report = ValidationReport()
    return DraftResult(
        draft=GOOD_DRAFT,
        attempts=[DraftAttempt(draft=GOOD_DRAFT, report=report)],
        claims_checked=True,
    )


def failing_result() -> DraftResult:
    report = ValidationReport(failures=["the body is 300 words; the limit is 120"])
    return DraftResult(attempts=[DraftAttempt(draft=GOOD_DRAFT, report=report)])


@pytest.fixture
def ctx(org):
    return OrgContext(org_id=org.id, actor_id="reviewer@example.com")


async def _research(db, org, lead, *, thin=False, confidence=0.8, hooks=None):
    row = Research(
        org_id=org.id,
        lead_id=lead.id,
        hooks=hooks
        if hooks is not None
        else [
            {
                "id": "hook:111",
                "text": "They are hiring backend engineers",
                "snippet": "hiring two backend engineers",
                "source_url": "https://globex.example/careers",
            }
        ],
        confidence=confidence,
        thin=thin,
    )
    db.add(row)
    await db.flush()
    return row


class TestSaving:
    async def test_a_passing_draft_is_stored_pending_review(self, db, ctx, lead_factory):
        lead = await lead_factory(state="DRAFTED")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        assert row.status == "pending_review"
        assert row.version == 1
        assert row.subject == "Your pipeline hiring"
        assert row.claims[0]["evidence_ref"] == "hook:111"

    async def test_a_failed_draft_is_stored_but_cannot_be_approved(self, db, ctx, lead_factory):
        """B2: a reviewer must see what went wrong, but the text must never be sendable."""
        lead = await lead_factory(state="DRAFTED")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=failing_result()
        )
        await db.commit()

        assert row.status == "validation_failed"
        assert row.validation["failures"]

        with pytest.raises(ValueError, match="cannot be approved"):
            await drafts_repo.approve(db, ctx, draft_id=row.id, approved_by="reviewer@example.com")

    async def test_a_result_with_no_text_at_all_is_refused(self, db, ctx, lead_factory):
        lead = await lead_factory(state="DRAFTED")
        with pytest.raises(ValueError, match="no text"):
            await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=DraftResult())

    async def test_regenerating_supersedes_the_previous_version(self, db, ctx, lead_factory):
        lead = await lead_factory(state="DRAFTED")
        first = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        second = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await db.refresh(first)

        assert first.status == "superseded"
        assert second.version == 2
        assert second.status == "pending_review"

    async def test_an_event_records_the_creation(self, db, ctx, lead_factory):
        lead = await lead_factory(state="DRAFTED")
        await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=passing_result())
        await db.commit()

        event = await db.scalar(
            select(Event).where(Event.lead_id == lead.id, Event.type == "draft.created")
        )
        assert event is not None
        assert event.actor == "llm"

    async def test_each_kind_versions_independently(self, db, ctx, lead_factory):
        lead = await lead_factory(state="DRAFTED")
        initial = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        followup = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="followup", result=passing_result()
        )
        await db.commit()

        assert initial.version == 1
        assert followup.version == 1, "a follow-up is not version 2 of the initial"


class TestApproval:
    async def test_approving_moves_the_lead_to_approved(self, db, ctx, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        approved = await drafts_repo.approve(
            db, ctx, draft_id=row.id, approved_by="reviewer@example.com"
        )
        await db.commit()
        await db.refresh(lead)

        assert approved.status == "approved"
        assert approved.approved_by == "reviewer@example.com"
        assert approved.approved_at is not None
        assert lead.state == "APPROVED"

    async def test_approving_does_not_send(self, db, ctx, lead_factory):
        """It only makes the lead eligible; the send loop re-checks everything (§9.1)."""
        from app.db.models import Message

        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await drafts_repo.approve(db, ctx, draft_id=row.id, approved_by="r@example.com")
        await db.commit()

        count = await db.scalar(select(func.count()).select_from(Message))
        assert count == 0, "no message may exist yet"

    async def test_an_edit_creates_a_new_version_rather_than_overwriting(
        self, db, ctx, lead_factory
    ):
        """E3: the prototype's single mutable column meant an edit erased what the model
        produced, so 'what did we nearly send?' was unanswerable."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        original = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        original_body = original.body

        edited = await drafts_repo.approve(
            db,
            ctx,
            draft_id=original.id,
            approved_by="reviewer@example.com",
            edited_body=original_body.replace("Worth a short call?", "Free for a quick call?"),
        )
        await db.commit()
        await db.refresh(original)

        assert edited.id != original.id
        assert edited.version == 2
        assert edited.status == "approved"
        assert original.status == "superseded"
        assert original.body == original_body, "the model's text is preserved"
        assert "Free for a quick call?" in edited.body

    async def test_a_human_edit_is_marked_as_unvalidated(self, db, ctx, lead_factory):
        """Edited text has not been through the validator, and the audit trail must not imply
        that it has."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        edited = await drafts_repo.approve(
            db,
            ctx,
            draft_id=row.id,
            approved_by="r@example.com",
            edited_subject="A better subject",
        )
        await db.commit()
        assert edited.validation["human_edited"] is True

    async def test_an_empty_edit_is_refused(self, db, ctx, lead_factory):
        """The prototype's UI could approve an empty edit, and the empty text was sent."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        with pytest.raises(ValueError, match="non-empty"):
            await drafts_repo.approve(
                db, ctx, draft_id=row.id, approved_by="r@example.com", edited_body="   "
            )

    async def test_approving_twice_is_refused(self, db, ctx, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await drafts_repo.approve(db, ctx, draft_id=row.id, approved_by="r@example.com")
        await db.commit()

        with pytest.raises(ValueError, match="cannot be approved"):
            await drafts_repo.approve(db, ctx, draft_id=row.id, approved_by="r@example.com")

    async def test_another_org_cannot_approve_our_draft(self, db, ctx, lead_factory):
        import uuid

        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        other = OrgContext(org_id=uuid.uuid4())
        with pytest.raises(LookupError):
            await drafts_repo.approve(db, other, draft_id=row.id, approved_by="x@example.com")


class TestRejection:
    async def test_rejecting_sends_the_lead_back_for_regeneration(self, db, ctx, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        rejected = await drafts_repo.reject(
            db,
            ctx,
            draft_id=row.id,
            rejected_by="r@example.com",
            feedback="too salesy, drop the second paragraph",
        )
        await db.commit()
        await db.refresh(lead)

        assert rejected.status == "rejected"
        assert "too salesy" in rejected.feedback
        assert lead.state == "DRAFTED", "ready to regenerate"

    async def test_the_feedback_is_stored_so_it_reaches_the_next_attempt(
        self, db, ctx, lead_factory
    ):
        """A reviewer's objection must reach the model, not be lost in a UI."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await drafts_repo.reject(
            db, ctx, draft_id=row.id, rejected_by="r@example.com", feedback="mention the pipeline"
        )
        await db.commit()

        stored = await db.scalar(select(Draft).where(Draft.id == row.id))
        assert stored.feedback == "mention the pipeline"


class TestReviewQueue:
    async def test_it_carries_the_evidence_with_each_draft(self, db, ctx, org, lead_factory):
        """A reviewer who has to look up whether a claim is true will stop checking by the
        thirtieth draft. §20 puts review at 50-100 minutes a day."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=passing_result())
        await db.commit()

        items = await drafts_repo.review_queue(db, ctx)
        assert len(items) == 1
        item = items[0]
        assert item.hooks[0]["source_url"] == "https://globex.example/careers"
        assert item.research_confidence == pytest.approx(0.8)
        assert item.lead.id == lead.id

    async def test_resolved_drafts_do_not_appear(self, db, ctx, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await drafts_repo.approve(db, ctx, draft_id=row.id, approved_by="r@example.com")
        await db.commit()

        assert await drafts_repo.review_queue(db, ctx) == []

    async def test_a_thin_research_draft_is_flagged_for_individual_review(
        self, db, ctx, org, lead_factory
    ):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead, thin=True, confidence=0.1, hooks=[])
        await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=passing_result())
        await db.commit()

        item = (await drafts_repo.review_queue(db, ctx))[0]
        assert item.thin_research is True
        assert item.needs_individual_review is True

    async def test_a_lead_with_no_research_row_is_treated_as_thin(self, db, ctx, lead_factory):
        """Absent research is not the same as good research."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=passing_result())
        await db.commit()

        item = (await drafts_repo.review_queue(db, ctx))[0]
        assert item.thin_research is True

    async def test_another_orgs_drafts_are_invisible(self, db, ctx, org, lead_factory):
        import uuid

        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=passing_result())
        await db.commit()

        other = OrgContext(org_id=uuid.uuid4())
        assert await drafts_repo.review_queue(db, other) == []


class TestBulkApprove:
    async def test_confident_drafts_are_approved_together(self, db, ctx, org, lead_factory):
        ids = []
        for _ in range(3):
            lead = await lead_factory(state="AWAITING_APPROVAL")
            await _research(db, org, lead, confidence=0.9)
            row = await drafts_repo.save_draft(
                db, ctx, lead=lead, kind="initial", result=passing_result()
            )
            ids.append(row.id)
        await db.commit()

        approved, skipped = await drafts_repo.bulk_approve(
            db, ctx, draft_ids=ids, approved_by="r@example.com"
        )
        await db.commit()

        assert len(approved) == 3
        assert skipped == {}

    async def test_a_thin_research_draft_is_skipped_not_approved(self, db, ctx, org, lead_factory):
        """Bulk approval exists to make the easy case fast, not to wave through the doubtful."""
        good_lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, good_lead, confidence=0.9)
        good = await drafts_repo.save_draft(
            db, ctx, lead=good_lead, kind="initial", result=passing_result()
        )

        thin_lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, thin_lead, thin=True, confidence=0.1, hooks=[])
        thin = await drafts_repo.save_draft(
            db, ctx, lead=thin_lead, kind="initial", result=passing_result()
        )
        await db.commit()

        approved, skipped = await drafts_repo.bulk_approve(
            db, ctx, draft_ids=[good.id, thin.id], approved_by="r@example.com"
        )
        await db.commit()

        assert approved == [good.id]
        assert "thin" in skipped[str(thin.id)]

    async def test_low_confidence_is_skipped(self, db, ctx, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead, confidence=0.3)
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        approved, skipped = await drafts_repo.bulk_approve(
            db, ctx, draft_ids=[row.id], approved_by="r@example.com", min_confidence=0.6
        )
        assert approved == []
        assert "confidence" in skipped[str(row.id)]

    async def test_a_validation_failed_draft_is_skipped(self, db, ctx, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead, confidence=0.9)
        row = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=failing_result()
        )
        await db.commit()

        approved, skipped = await drafts_repo.bulk_approve(
            db, ctx, draft_ids=[row.id], approved_by="r@example.com"
        )
        assert approved == []
        assert "validation failed" in skipped[str(row.id)]

    async def test_requested_drafts_are_found_regardless_of_queue_size(
        self, db, ctx, org, lead_factory
    ):
        """A page-limited lookup would report a later-sorting draft as 'not in the queue' and
        silently skip it, which is the worst possible failure for a bulk action."""
        ids = []
        for index in range(8):
            lead = await lead_factory(state="AWAITING_APPROVAL")
            await _research(db, org, lead, confidence=0.9)
            row = await drafts_repo.save_draft(
                db, ctx, lead=lead, kind="initial", result=passing_result()
            )
            if index >= 6:  # only the last two, which sort last by created_at
                ids.append(row.id)
        await db.commit()

        approved, skipped = await drafts_repo.bulk_approve(
            db, ctx, draft_ids=ids, approved_by="r@example.com"
        )
        await db.commit()

        assert sorted(approved) == sorted(ids)
        assert skipped == {}


class TestRejectingQueuesARegeneration:
    """Rejecting a draft must produce another one, or the lead quietly disappears.

    Found in production. A reviewer rejected a draft with "add one more line about athena", the
    audit trail recorded the feedback correctly, the lead went back to `DRAFTED` -- and
    **nothing regenerated it**. The review queue went from 1 to 0 and the lead sat in `DRAFTED`
    with no pending draft and no job, which took a database query to even notice.

    `reject` did its half: the draft is `rejected`, the feedback is stored, the lead returns to
    `DRAFTED`. Its docstring said "for regeneration". Nothing enqueued it. The same shape as the
    earlier defect where research never queued drafting -- a correct callee, a correct caller,
    and nothing checking that they agreed.

    The job is now enqueued **inside `reject`**, in the same transaction, so the two cannot
    disagree again: either the rejection and its regeneration both land, or neither does.
    """

    async def test_a_rejection_enqueues_the_regeneration(self, db, ctx, org, lead_factory):
        from app.db.models import Job

        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        draft = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        await drafts_repo.reject(
            db, ctx, draft_id=draft.id, rejected_by="reviewer@example.com", feedback="too long"
        )
        await db.commit()

        job = await db.scalar(select(Job).where(Job.lead_id == lead.id, Job.kind == "draft_lead"))
        assert job is not None, "nothing will ever write the next draft"
        assert job.status == "queued"

    async def test_the_reviewers_words_are_carried_to_the_next_draft(
        self, db, ctx, org, lead_factory
    ):
        """Storing the feedback is not the same as telling the model about it."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        draft = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()

        await drafts_repo.reject(
            db,
            ctx,
            draft_id=draft.id,
            rejected_by="reviewer@example.com",
            feedback="add one more line about athena",
        )
        await db.commit()

        carried = await drafts_repo.latest_rejection_feedback(db, ctx, lead_id=lead.id)
        assert carried == "add one more line about athena"

    async def test_nothing_is_carried_when_nothing_was_rejected(self, db, ctx, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        await drafts_repo.save_draft(db, ctx, lead=lead, kind="initial", result=passing_result())
        await db.commit()

        assert await drafts_repo.latest_rejection_feedback(db, ctx, lead_id=lead.id) is None

    async def test_one_leads_feedback_never_steers_another(self, db, ctx, org, lead_factory):
        mine = await lead_factory(state="AWAITING_APPROVAL")
        theirs = await lead_factory(state="AWAITING_APPROVAL")
        for lead in (mine, theirs):
            await _research(db, org, lead)
        other = await drafts_repo.save_draft(
            db, ctx, lead=theirs, kind="initial", result=passing_result()
        )
        await drafts_repo.save_draft(db, ctx, lead=mine, kind="initial", result=passing_result())
        await db.commit()

        await drafts_repo.reject(
            db, ctx, draft_id=other.id, rejected_by="r@example.com", feedback="theirs only"
        )
        await db.commit()

        assert await drafts_repo.latest_rejection_feedback(db, ctx, lead_id=mine.id) is None

    async def test_the_newest_rejection_wins(self, db, ctx, org, lead_factory):
        """Two rounds of review: the model is told the most recent objection, not the first."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        await _research(db, org, lead)
        first = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await drafts_repo.reject(
            db, ctx, draft_id=first.id, rejected_by="r@example.com", feedback="too long"
        )
        await db.commit()

        second = await drafts_repo.save_draft(
            db, ctx, lead=lead, kind="initial", result=passing_result()
        )
        await db.commit()
        await drafts_repo.reject(
            db, ctx, draft_id=second.id, rejected_by="r@example.com", feedback="too formal"
        )
        await db.commit()

        assert await drafts_repo.latest_rejection_feedback(db, ctx, lead_id=lead.id) == "too formal"
