"""Undoing an import, and the one rule that makes it a different operation from erasure.

"I uploaded the wrong sheet" had no answer. The only way back was `DELETE /leads/{id}`, one lead
at a time, and **that is the wrong tool**: it is the erasure path for a data-subject request, so
it deliberately leaves a hashed suppression behind. Undoing a mistaken import through it would
permanently block every address in the sheet, so re-uploading the corrected one would skip exactly
the people it was meant to reach -- with nothing on screen saying why.

Nobody in a mistaken upload asked not to be contacted. So an undo suppresses nothing, and the
test that proves it is the most important one in this file.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.db.models import Draft, Job, Lead, Message, Suppression
from app.db.repo import OrgContext
from app.services.importer.undo import undo_import

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def batch(db, org, campaign):
    """Four leads from one upload, and one from another, so the batch boundary is tested."""
    batch_id = uuid.uuid4()
    other = uuid.uuid4()
    made: list[Lead] = []
    for n in range(4):
        lead = Lead(
            org_id=org.id,
            campaign_id=campaign.id,
            email_norm=f"batch{n}@example.com",
            email_raw=f"batch{n}@example.com",
            full_name=f"Lead {n}",
            state="NEW",
            import_batch_id=batch_id,
        )
        db.add(lead)
        made.append(lead)
    stranger = Lead(
        org_id=org.id,
        campaign_id=campaign.id,
        email_norm="other@example.com",
        email_raw="other@example.com",
        full_name="Another upload",
        state="NEW",
        import_batch_id=other,
    )
    db.add(stranger)
    await db.commit()
    return batch_id, made, stranger


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org):
    from app.db.models import Mailbox

    row = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(row)
    await db.commit()
    return row


def ctx_for(org) -> OrgContext:
    return OrgContext(org_id=org.id, actor_id="owner@example.com")


class TestUndoingAnImport:
    async def test_it_removes_the_leads_of_that_batch(self, db, org, campaign, batch):
        batch_id, _made, _stranger = batch

        report = await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        assert report.removed == 4
        assert report.total_kept == 0
        left = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.import_batch_id == batch_id)
        )
        assert left == 0

    async def test_it_leaves_another_upload_alone(self, db, org, campaign, batch):
        batch_id, _made, stranger = batch

        await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        assert await db.get(Lead, stranger.id) is not None

    async def test_it_suppresses_nothing(self, db, org, campaign, batch):
        """**The point of the whole file.** Erasure leaves a hashed suppression so an opt-out
        survives being forgotten. An undo must not, or the corrected sheet would import and
        silently skip every address in it."""
        batch_id, _made, _stranger = batch
        before = await db.scalar(select(func.count()).select_from(Suppression))

        await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        after = await db.scalar(select(func.count()).select_from(Suppression))
        assert after == before, "an undo is not an opt-out"

    async def test_the_research_and_drafts_go_with_the_lead(self, db, org, campaign, batch):
        batch_id, made, _stranger = batch
        db.add(
            Draft(
                org_id=org.id,
                lead_id=made[0].id,
                kind="initial",
                version=1,
                subject="Hi",
                body="Hello",
                status="pending_review",
            )
        )
        await db.commit()

        await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        left = await db.scalar(
            select(func.count()).select_from(Draft).where(Draft.lead_id == made[0].id)
        )
        assert left == 0, "a draft for a lead that no longer exists is an orphan"

    async def test_queued_work_goes_with_the_lead(self, db, org, campaign, batch):
        """Otherwise the research jobs run against deleted leads. That is already handled as a
        permanent failure rather than a retry, but a queue full of them is noise that hides
        something real."""
        batch_id, made, _stranger = batch
        db.add(
            Job(
                org_id=org.id,
                kind="research_lead",
                lead_id=made[1].id,
                payload={"lead_id": str(made[1].id)},
            )
        )
        await db.commit()

        await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        left = await db.scalar(
            select(func.count()).select_from(Job).where(Job.lead_id == made[1].id)
        )
        assert left == 0


class TestWhatAnUndoWillNotDo:
    async def test_an_emailed_lead_is_kept_and_counted(self, db, org, campaign, batch, mailbox):
        """The record of what we sent to whom is the one thing here that must not be editable: a
        reply can still arrive, an opt-out has to be honourable, a bounce attributable."""
        batch_id, made, _stranger = batch
        db.add(
            Message(
                org_id=org.id,
                lead_id=made[0].id,
                mailbox_id=mailbox.id,
                direction="out",
                status="sent",
                kind="initial",
                subject="Hi",
                body_text="Hello",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                idempotency_key=str(uuid.uuid4()),
            )
        )
        made[0].state = "SENT"
        await db.commit()

        report = await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        assert report.removed == 3
        assert report.kept == {"already emailed": 1}
        assert await db.get(Lead, made[0].id) is not None
        assert "already emailed" in report.summary()

    async def test_a_message_still_in_flight_counts_as_emailed(
        self, db, org, campaign, batch, mailbox
    ):
        """`sending` means claimed but unresolved, and ADR-002 is explicit that we never assume
        such a message did not leave. Deleting the lead would delete the only record of it."""
        batch_id, made, _stranger = batch
        db.add(
            Message(
                org_id=org.id,
                lead_id=made[2].id,
                mailbox_id=mailbox.id,
                direction="out",
                status="sending",
                kind="initial",
                subject="Hi",
                body_text="Hello",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                idempotency_key=str(uuid.uuid4()),
            )
        )
        await db.commit()

        report = await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        assert report.kept == {"already emailed": 1}
        assert await db.get(Lead, made[2].id) is not None

    async def test_a_lead_in_conversation_is_kept(self, db, org, campaign, batch):
        """No outbound message in the test, so this is the *state* rule rather than the message
        one: something happened to this person, and an undo does not get to unsay it."""
        batch_id, made, _stranger = batch
        made[3].state = "REPLIED"
        await db.commit()

        report = await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=batch_id)
        await db.commit()

        assert report.removed == 3
        assert report.kept == {"in REPLIED": 1}

    async def test_an_unknown_batch_removes_nothing(self, db, org, campaign):
        report = await undo_import(db, ctx_for(org), campaign_id=campaign.id, batch_id=uuid.uuid4())

        assert report.removed == 0
        assert report.total_kept == 0
