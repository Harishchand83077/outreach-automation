"""Erasure and retention (§17, issue D6).

The test that matters most is `test_erasing_an_opted_out_lead_leaves_them_unreachable`. Every
other property here is bookkeeping next to it: a naive erasure deletes the lead *and* the opt-out
that came with them, and the next import of the same sheet mails the person who asked to be left
alone. That is a privacy feature producing the exact harm it exists to prevent, and it is an easy
mistake to make, because deleting everything looks more thorough than deleting most things.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db import outbox
from app.db.models import Draft, Event, Lead, Message, Research, Suppression
from app.db.repo import OrgContext
from app.services.privacy import erase_lead, hashed_email, run_retention
from app.services.privacy.erasure import EMAIL_HASH
from app.services.privacy.retention import clear_old_evidence, erase_expired_leads

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 12, 11, 0, tzinfo=UTC)


@pytest.fixture
def ctx(org) -> OrgContext:
    return OrgContext(org_id=org.id, actor_id="owner@example.com")


async def furnish(db, lead, *, org) -> None:
    """Give a lead the full set of attached personal data."""
    db.add(Draft(org_id=org.id, lead_id=lead.id, kind="initial", subject="Hello", body="Body"))
    db.add(
        Message(
            org_id=org.id,
            lead_id=lead.id,
            direction="out",
            status="sent",
            subject="Hello",
            body_text="Body",
            # An outbound row must carry one: it is what makes the outbox at-most-once (ADR-002).
            idempotency_key=f"test-{uuid.uuid4()}",
        )
    )
    db.add(Research(org_id=org.id, lead_id=lead.id, person_notes={"role": "founder"}, hooks=[]))
    await db.commit()


class TestErasure:
    async def test_it_removes_the_lead_and_everything_attached(self, db, ctx, lead_factory, org):
        lead = await lead_factory()
        await furnish(db, lead, org=org)

        result = await erase_lead(db, ctx, lead.id)
        await db.commit()

        assert result.drafts == 1
        assert result.messages == 1
        assert result.research == 1
        assert await db.get(Lead, lead.id) is None
        for model, column in (
            (Draft, Draft.lead_id),
            (Message, Message.lead_id),
            (Research, Research.lead_id),
        ):
            remaining = await db.scalar(
                select(func.count()).select_from(model).where(column == lead.id)
            )
            assert remaining == 0, f"{model.__name__} survived the erasure"

    async def test_erasing_an_opted_out_lead_leaves_them_unreachable(
        self, db, ctx, lead_factory, org
    ):
        """The whole point. Forgetting somebody must not mean contacting them again."""
        lead = await lead_factory(email="quiet@example.com")
        db.add(
            Suppression(org_id=org.id, kind="email", value="quiet@example.com", reason="opt_out")
        )
        await db.commit()

        await erase_lead(db, ctx, lead.id)
        await db.commit()

        assert await outbox.is_suppressed(db, org_id=org.id, email="quiet@example.com")

    async def test_the_address_itself_is_not_kept(self, db, ctx, lead_factory, org):
        lead = await lead_factory(email="forgetme@example.com")
        await erase_lead(db, ctx, lead.id)
        await db.commit()

        values = (await db.execute(select(Suppression.value))).scalars().all()
        assert "forgetme@example.com" not in values
        assert hashed_email("forgetme@example.com") in values

    async def test_the_digest_matches_whatever_the_capitalisation(self, db, ctx, lead_factory, org):
        """`is_suppressed` normalises before comparing; the digest must be built the same way,
        or it would only match when the caller happened to use the same case."""
        lead = await lead_factory(email="mixed@example.com")
        await erase_lead(db, ctx, lead.id)
        await db.commit()

        assert await outbox.is_suppressed(db, org_id=org.id, email="MiXeD@Example.COM")

    async def test_erasing_twice_is_not_an_error_the_second_time(self, db, ctx, lead_factory):
        lead = await lead_factory()
        await erase_lead(db, ctx, lead.id)
        await db.commit()

        with pytest.raises(LookupError):
            await erase_lead(db, ctx, lead.id)

    async def test_another_orgs_lead_cannot_be_erased(self, db, lead_factory):
        lead = await lead_factory()
        # Captured before the rollback: a rollback expires every loaded attribute, and reading
        # `lead.id` afterwards would try to reload it outside the async context.
        lead_id = lead.id
        stranger = OrgContext(org_id=uuid.uuid4(), actor_id="rival@example.com")

        with pytest.raises(LookupError):
            await erase_lead(db, stranger, lead_id)
        await db.rollback()
        assert await db.get(Lead, lead_id) is not None

    async def test_keep_suppressed_false_leaves_nothing_behind(self, db, ctx, lead_factory, org):
        """For a lead imported by mistake and never contacted: no opt-out to honour, and no
        reason to retain even a digest."""
        lead = await lead_factory(email="mistake@example.com")
        await erase_lead(db, ctx, lead.id, keep_suppressed=False)
        await db.commit()

        count = await db.scalar(
            select(func.count()).select_from(Suppression).where(Suppression.kind == EMAIL_HASH)
        )
        assert count == 0

    async def test_the_append_only_events_trigger_does_not_block_erasure(
        self, db, ctx, lead_factory, org
    ):
        """`events` refuses DELETE by trigger. Erasure uses the one escape hatch the migration
        left for exactly this, so a lawful request is not defeated by an audit guarantee."""
        lead = await lead_factory()
        db.add(Event(org_id=org.id, lead_id=lead.id, actor="system", type="imported", data={}))
        await db.commit()

        await erase_lead(db, ctx, lead.id)
        await db.commit()

        remaining = await db.scalar(
            select(func.count()).select_from(Event).where(Event.lead_id == lead.id)
        )
        assert remaining == 0


class TestRetention:
    async def test_it_erases_leads_that_finished_long_ago(self, db, lead_factory):
        old = await lead_factory(state="NO_RESPONSE")
        old.state_changed_at = NOW - timedelta(days=400)
        await db.commit()

        erased = await erase_expired_leads(db, older_than=timedelta(days=365), now=NOW)
        await db.commit()

        assert erased == 1
        assert await db.get(Lead, old.id) is None

    async def test_it_leaves_leads_that_are_still_in_flight(self, db, lead_factory):
        """Age alone is not a reason to drop somebody from a thread they are still replying to."""
        busy = await lead_factory(state="IN_CONVERSATION")
        busy.state_changed_at = NOW - timedelta(days=400)
        await db.commit()

        erased = await erase_expired_leads(db, older_than=timedelta(days=365), now=NOW)
        await db.commit()

        assert erased == 0
        assert await db.get(Lead, busy.id) is not None

    async def test_it_leaves_recently_finished_leads(self, db, lead_factory):
        recent = await lead_factory(state="NO_RESPONSE")
        recent.state_changed_at = NOW - timedelta(days=30)
        await db.commit()

        assert await erase_expired_leads(db, older_than=timedelta(days=365), now=NOW) == 0

    async def test_a_retained_lead_is_still_suppressed_afterwards(self, db, lead_factory, org):
        """Retention goes through the same path as a request, so this holds there too — and it
        is the case most likely to be got wrong, because nobody is watching when it runs."""
        lead = await lead_factory(state="OPTED_OUT", email="gone@example.com")
        lead.state_changed_at = NOW - timedelta(days=400)
        await db.commit()

        await erase_expired_leads(db, older_than=timedelta(days=365), now=NOW)
        await db.commit()

        assert await outbox.is_suppressed(db, org_id=org.id, email="gone@example.com")

    async def test_it_is_bounded_per_sweep(self, db, lead_factory):
        """A database left untended for a year is cleared over several ticks, not in one
        transaction that locks the table."""
        for _ in range(4):
            lead = await lead_factory(state="NO_RESPONSE")
            lead.state_changed_at = NOW - timedelta(days=400)
        await db.commit()

        erased = await erase_expired_leads(db, older_than=timedelta(days=365), batch=2, now=NOW)
        await db.commit()
        assert erased == 2

    async def test_old_evidence_text_is_cleared_but_the_finding_is_kept(self, db, org):
        from app.db.models import DomainResearch

        row = DomainResearch(
            org_id=org.id,
            domain="example.com",
            findings={"hooks": ["raised a seed round"]},
            sources=[{"url": "https://example.com"}],
            evidence_text="a great deal of page text",
            fetched_at=NOW - timedelta(days=90),
            expires_at=NOW + timedelta(days=30),
        )
        db.add(row)
        await db.commit()

        cleared = await clear_old_evidence(db, older_than=timedelta(days=30), now=NOW)
        await db.commit()
        await db.refresh(row)

        assert cleared == 1
        assert row.evidence_text is None
        assert row.findings == {"hooks": ["raised a seed round"]}, "the finding must survive"

    async def test_the_sweep_reports_what_it_did(self, session_factory, db, lead_factory):
        lead = await lead_factory(state="NO_RESPONSE")
        lead.state_changed_at = NOW - timedelta(days=400)
        await db.commit()

        report = await run_retention(
            session_factory,
            lead_retention=timedelta(days=365),
            evidence_retention=timedelta(days=30),
            now=NOW,
        )
        assert report.leads_erased == 1
        assert bool(report) is True
