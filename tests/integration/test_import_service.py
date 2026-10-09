"""Import preview and commit, against a real database.

Needs a database because the decisive checks are queries: has this address already been imported
into this campaign, and is it on the suppression list. Those are what stop us emailing someone
twice or emailing someone who opted out.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.db import repo
from app.db.models import Lead
from app.db.repo import OrgContext
from app.services.importer import sheet
from app.services.importer.service import commit, preview
from app.services.importer.validation import RowVerdict

pytestmark = pytest.mark.asyncio(loop_scope="session")

SHEET = (
    b"Full Name,E-mail,Company Name,Website,Title,Segment\n"
    b"Asha Rao,asha@acme.example,Acme Analytics,acme.example,Founder,SaaS\n"
    b"Ben Shah,ben@globex.example,Globex Logistics,www.globex.example,CTO,Logistics\n"
)


class FakeMx:
    def __init__(self, dead: set[str] | None = None) -> None:
        self.dead = dead or set()

    def has_mx(self, domain: str) -> bool:
        return domain not in self.dead


@pytest.fixture
def ctx(org):
    return OrgContext(org_id=org.id, actor_id="owner@example.com")


class TestPreview:
    async def test_it_maps_headers_and_validates_every_row(self, db, ctx, campaign):
        result = await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )

        assert result.mapping.is_importable
        assert result.counts["ok"] == 2
        assert result.can_commit
        assert result.file_sha256

    async def test_nothing_is_written(self, db, ctx, campaign):
        """A preview must be safe to run repeatedly on a sheet the user is still fixing."""
        await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        count = await db.scalar(select(func.count()).select_from(Lead))
        assert count == 0

    async def test_row_numbers_match_what_the_user_sees_in_excel(self, db, ctx, campaign):
        result = await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        assert [r.row_number for r in result.rows] == [2, 3], "row 1 is the header"

    async def test_an_unmapped_required_column_blocks_the_import(self, db, ctx, campaign):
        data = b"Name,Company\nAsha,Acme\n"
        result = await preview(
            db, ctx, data=data, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        assert not result.can_commit
        assert result.mapping.missing_required == ["email"]

    async def test_a_duplicate_inside_the_file_is_rejected_once(self, db, ctx, campaign):
        data = (
            b"Name,Email,Company\nAsha,asha@acme.example,Acme\nAsha Again,ASHA@acme.example,Acme\n"
        )
        result = await preview(
            db, ctx, data=data, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        # The first occurrence is kept (it warns only because this sheet has no Website column).
        assert result.rows[0].importable
        assert result.rows[1].verdict is RowVerdict.REJECT
        assert "duplicate of row 2" in " ".join(result.rows[1].reasons)

    async def test_an_address_already_in_the_campaign_is_rejected(
        self, db, ctx, campaign, lead_factory
    ):
        """B16's other half: re-uploading a corrected sheet must not silently double-contact."""
        await lead_factory(email="asha@acme.example")
        result = await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        rejected = [r for r in result.rows if r.verdict is RowVerdict.REJECT]
        assert len(rejected) == 1
        assert "already imported" in " ".join(rejected[0].reasons)

    async def test_a_suppressed_address_is_rejected(self, db, ctx, campaign):
        """Suppression is checked at import as well as at send, so an opted-out person is never
        even queued."""
        await repo.add_suppression(ctx, db, value="ben@globex.example", reason="opt_out")
        await db.commit()

        result = await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        rejected = [r for r in result.rows if r.verdict is RowVerdict.REJECT]
        assert len(rejected) == 1
        assert "suppression" in " ".join(rejected[0].reasons)

    async def test_a_suppressed_domain_rejects_everyone_at_it(self, db, ctx, campaign):
        await repo.add_suppression(ctx, db, value="acme.example", reason="complaint", kind="domain")
        await db.commit()

        result = await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        assert result.rows[0].verdict is RowVerdict.REJECT

    async def test_shared_domains_are_flagged_rather_than_rejected(self, db, ctx, campaign):
        """Several founders at one company is normal, and worth seeing before sending."""
        data = b"Name,Email,Company\nAsha,asha@acme.example,Acme\nBen,ben@acme.example,Acme\n"
        result = await preview(
            db, ctx, data=data, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        assert result.duplicate_domains == {"acme.example": [2, 3]}
        assert all(r.importable for r in result.rows)
        assert all("duplicate_domain" in r.flags for r in result.rows)

    async def test_a_dead_domain_is_rejected(self, db, ctx, campaign):
        result = await preview(
            db,
            ctx,
            data=SHEET,
            filename="leads.csv",
            campaign_id=campaign.id,
            mx=FakeMx(dead={"globex.example"}),
        )
        assert result.rows[1].verdict is RowVerdict.REJECT

    async def test_an_unreadable_file_raises_rather_than_returning_a_preview(
        self, db, ctx, campaign
    ):
        with pytest.raises(sheet.SheetError):
            await preview(
                db, ctx, data=b"", filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
            )


class TestCommit:
    async def test_importable_rows_become_leads(self, db, ctx, campaign):
        leads, _ = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        assert len(leads) == 2
        assert {lead.email_norm for lead in leads} == {
            "asha@acme.example",
            "ben@globex.example",
        }
        assert all(lead.state == "NEW" for lead in leads)
        assert all(lead.org_id == ctx.org_id for lead in leads)

    async def test_the_extra_columns_are_preserved(self, db, ctx, campaign):
        """A5: the prototype dropped every column it did not recognise, so none of it could
        ever reach research or drafting."""
        leads, _ = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        by_email = {lead.email_norm: lead for lead in leads}
        assert by_email["asha@acme.example"].extra == {"Segment": "SaaS"}
        assert by_email["ben@globex.example"].extra == {"Segment": "Logistics"}

    async def test_recognised_fields_land_in_their_own_columns(self, db, ctx, campaign):
        leads, _ = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        asha = next(lead for lead in leads if lead.email_norm == "asha@acme.example")
        assert asha.full_name == "Asha Rao"
        assert asha.company_name == "Acme Analytics"
        assert asha.company_url == "https://acme.example"
        assert asha.job_title == "Founder"
        assert asha.domain == "acme.example"
        assert asha.email_raw == "asha@acme.example"

    async def test_the_www_prefix_is_normalised_away(self, db, ctx, campaign):
        leads, _ = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()
        ben = next(lead for lead in leads if lead.email_norm == "ben@globex.example")
        assert ben.company_url == "https://globex.example"

    async def test_warning_flags_are_carried_onto_the_lead(self, db, ctx, campaign):
        """So the review queue can show why a draft may be thin."""
        data = b"Name,Email,Company\nAsha,founder@gmail.com,Acme\n"
        leads, _ = await commit(
            db, ctx, data=data, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        assert "free_mail" in leads[0].flags
        assert "no_company_url" in leads[0].flags

    async def test_rejected_rows_are_not_imported(self, db, ctx, campaign):
        data = (
            b"Name,Email,Company\n"
            b"Good,good@acme.example,Acme\n"
            b"Bad,not-an-email,Acme\n"
            b"NoCompany,nc@acme.example,\n"
        )
        leads, result = await commit(
            db, ctx, data=data, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        assert len(leads) == 1
        assert result.counts["reject"] == 2

    async def test_committing_without_a_mapped_email_column_is_refused(self, db, ctx, campaign):
        with pytest.raises(sheet.SheetError, match="unmapped required"):
            await commit(
                db,
                ctx,
                data=b"Name,Company\nAsha,Acme\n",
                filename="leads.csv",
                campaign_id=campaign.id,
                mx=FakeMx(),
            )

    async def test_commit_revalidates_rather_than_trusting_a_previous_preview(
        self, db, ctx, campaign
    ):
        """A preview is advisory. If someone opts out between preview and commit, the commit
        must see that — anything deciding who receives email is re-checked at write time.
        """
        first = await preview(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        assert first.counts["ok"] == 2

        await repo.add_suppression(ctx, db, value="ben@globex.example", reason="opt_out")
        await db.commit()

        leads, result = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        assert len(leads) == 1
        assert leads[0].email_norm == "asha@acme.example"
        assert result.counts["reject"] == 1

    async def test_importing_the_same_sheet_twice_adds_nobody(self, db, ctx, campaign):
        """The second run should be a no-op, not a second round of emails."""
        await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        leads, result = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        assert leads == []
        assert result.counts["reject"] == 2

    async def test_the_same_sheet_can_be_imported_into_a_different_campaign(
        self, db, ctx, org, campaign
    ):
        """B16: email is unique per campaign, not globally, so a later campaign can re-contact."""
        from app.db.models import Campaign

        await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=campaign.id, mx=FakeMx()
        )
        await db.commit()

        second = Campaign(org_id=org.id, name="Q2 Campaign", status="draft")
        db.add(second)
        await db.commit()

        leads, _ = await commit(
            db, ctx, data=SHEET, filename="leads.csv", campaign_id=second.id, mx=FakeMx()
        )
        await db.commit()
        assert len(leads) == 2
