"""An existing campaign must behave identically after the kind column lands.

**This is the test the migration exists to pass.** A new column with a default is easy to add and
easy to convince yourself is harmless; the only thing that makes that true is checking it against
a campaign built the way campaigns were built before the column existed.

So these build a campaign the old way -- no `kind`, a plain `Persona`, settings as they were --
and assert that everything the send path reads from it is unchanged.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain.kinds import CampaignKind, defaults_for, load_sender, opt_out_line_for
from app.domain.persona import Persona
from app.services.mail.message import FooterIdentity, build_footer

pytestmark = pytest.mark.asyncio(loop_scope="session")

OLD_PERSONA = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Acme Software",
    "offer": "fixed-scope data pipeline projects",
    "goal": "book a 20-minute call",
    "cta_text": "Worth a short call next week?",
    "proof_points": [{"id": "pp1", "text": "Shipped a pipeline for Globex in six weeks"}],
}


class TestACampaignFromBeforeTheColumn:
    async def test_it_defaults_to_outreach(self, db, org):
        """Written without a `kind`, exactly as every existing row was."""
        from app.db.models import Campaign

        campaign = Campaign(org_id=org.id, name=f"Old {uuid.uuid4().hex[:6]}", persona=OLD_PERSONA)
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        assert campaign.kind == "outreach"

    async def test_its_persona_still_loads_unchanged(self, db, org):
        sender = load_sender(OLD_PERSONA, kind="outreach")

        assert isinstance(sender, Persona)
        assert sender.sender_name == "Asha Rao"
        assert sender.agency_name == "Acme Software"
        assert [point.id for point in sender.proof_points] == ["pp1"]

    def test_outreach_defaults_match_the_product_rules(self):
        """100 a day and one follow-up: the locked rules, copied rather than re-decided."""
        defaults = defaults_for(CampaignKind.OUTREACH)
        assert defaults.daily_cap == 100
        assert defaults.max_followups == 1

    def test_the_outreach_footer_is_byte_identical(self):
        """The opt-out line is the one piece of the sent email the kind touches.

        For outreach it must be exactly the sentence that was there before, because every email
        already sent carries it and a quiet change would make the archive inconsistent.
        """
        identity = FooterIdentity(sender_name="Asha Rao", agency_name="Acme Software")
        before = build_footer(identity)
        after = build_footer(
            FooterIdentity(
                sender_name="Asha Rao",
                agency_name="Acme Software",
                opt_out_line=opt_out_line_for(CampaignKind.OUTREACH),
            )
        )
        assert before == after


class TestAJobHuntCampaignDiffers:
    async def test_it_can_be_stored_with_its_own_kind(self, db, org):
        from app.db.models import Campaign

        campaign = Campaign(
            org_id=org.id,
            name=f"Hunt {uuid.uuid4().hex[:6]}",
            kind="job_hunt",
            persona={
                "kind": "job_hunt",
                "full_name": "Priya Nair",
                "headline": "Backend engineer, 4 years",
                "target_role": "backend roles at early-stage startups",
                "facts": [{"id": "pp1", "text": "Four years of Django at Globex"}],
                "cta_text": "Open to a quick chat?",
            },
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        assert campaign.kind == "job_hunt"

    async def test_an_unknown_kind_is_refused_by_the_database(self, db, org):
        """The check constraint, not just the API.

        A kind nobody implemented would otherwise silently get outreach's cap, which is four
        times what a candidate should be sending at.
        """
        from sqlalchemy.exc import IntegrityError

        from app.db.models import Campaign

        db.add(
            Campaign(
                org_id=org.id,
                name=f"Bad {uuid.uuid4().hex[:6]}",
                kind="fundraising",
                persona=OLD_PERSONA,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    def test_its_footer_asks_differently(self):
        job_hunt = build_footer(
            FooterIdentity(
                sender_name="Priya Nair",
                agency_name="",
                opt_out_line=opt_out_line_for(CampaignKind.JOB_HUNT),
            )
        )
        assert "no thanks" not in job_hunt
        # Still a working opt-out: the obligation does not change with the register.
        assert "follow up" in job_hunt
