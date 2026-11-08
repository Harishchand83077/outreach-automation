"""Nothing reaches a drafting prompt until a person approves it (F16, ADR-059).

The unit tests cover the arithmetic. These cover the property the whole feature rests on: this
is the only path by which the product's own behaviour changes on the strength of what it
observed, and the gate is a person rather than a threshold.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import Draft, DraftLesson
from app.db.repo import OrgContext
from app.services import learning

from .test_api_settings import api, keypair, member, owner  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

BOILERPLATE = "I hope this finds you well."


def _removal(rows):
    """The lesson about the deleted sentence.

    Picked by kind rather than by position: deleting a sentence also shortens the body, so a
    single edit legitimately produces two patterns and "the first one" is whichever happened to
    be commoner. Asserting on position would be a test about ordering pretending to be a test
    about removals.
    """
    return next(row for row in rows if row.kind.startswith("removed:"))


async def _edited_pair(db, org, lead, *, before: str, after: str) -> Draft:
    """A model draft and the human edit derived from it, as `drafts.approve` writes them."""
    original = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind="initial",
        version=1,
        subject="A question",
        body=before,
        status="superseded",
        created_by="model",
    )
    db.add(original)
    await db.flush()
    edit = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind="initial",
        version=2,
        subject="A question",
        body=after,
        status="approved",
        created_by="person",
        derived_from_id=original.id,
    )
    db.add(edit)
    await db.commit()
    return edit


class TestItCountsWhatReviewersDid:
    async def test_three_edits_removing_the_same_line_become_a_proposal(
        self, db, org, lead_factory
    ):
        for _ in range(3):
            lead = await lead_factory()
            await _edited_pair(
                db,
                org,
                lead,
                before=f"{BOILERPLATE} We build backends for Indian startups.",
                after="We build backends for Indian startups.",
            )

        found = await learning.propose(db, OrgContext(org_id=org.id))
        await db.commit()
        assert any("hope this finds you well" in row.detail for row in found)
        assert all(row.state == "proposed" for row in found)

    async def test_two_edits_are_not_enough(self, db, org, lead_factory):
        for _ in range(2):
            lead = await lead_factory()
            await _edited_pair(
                db,
                org,
                lead,
                before=f"{BOILERPLATE} We build backends.",
                after="We build backends.",
            )

        found = await learning.propose(db, OrgContext(org_id=org.id))
        await db.commit()
        assert not any("hope this finds you well" in row.detail for row in found)

    async def test_a_regeneration_is_not_an_edit(self, db, org, lead_factory):
        """`derived_from_id` is set only when a reviewer approved with changes. A regeneration
        after a rejection also makes a new version, and it is a *different event* -- ADR-026
        added the column precisely because version numbers cannot tell them apart."""
        lead = await lead_factory()
        for version in (1, 2, 3):
            db.add(
                Draft(
                    org_id=org.id,
                    lead_id=lead.id,
                    kind="initial",
                    version=version,
                    subject="A question",
                    body=f"{BOILERPLATE} Attempt {version}.",
                    status="superseded",
                    created_by="model",
                )
            )
        await db.commit()

        assert await learning.pairs(db, OrgContext(org_id=org.id)) == []


class TestNothingReachesAPromptUnapproved:
    async def test_a_proposal_is_not_an_instruction(self, db, org, lead_factory):
        """**The safety property of the whole feature.** A proposal is an observation nobody
        has agreed with yet."""
        for _ in range(3):
            lead = await lead_factory()
            await _edited_pair(
                db, org, lead, before=f"{BOILERPLATE} Hello there.", after="Hello there."
            )
        await learning.propose(db, OrgContext(org_id=org.id))
        await db.commit()

        assert await learning.instructions(db, OrgContext(org_id=org.id)) == []

    async def test_an_approved_lesson_becomes_an_instruction(self, db, org, lead_factory):
        for _ in range(3):
            lead = await lead_factory()
            await _edited_pair(
                db, org, lead, before=f"{BOILERPLATE} Hello there.", after="Hello there."
            )
        ctx = OrgContext(org_id=org.id)
        waiting = await learning.propose(db, ctx)
        await learning.decide(
            db, ctx, lesson_id=_removal(waiting).id, approve=True, decided_by="owner@example.com"
        )
        await db.commit()

        lines = await learning.instructions(db, ctx)
        assert len(lines) == 1
        assert "do not write it" in lines[0]

    async def test_a_dismissed_lesson_is_never_proposed_again(self, db, org, lead_factory):
        """Somebody looked and said no. Asking every week trains them to stop reading."""
        for _ in range(3):
            lead = await lead_factory()
            await _edited_pair(
                db, org, lead, before=f"{BOILERPLATE} Hello there.", after="Hello there."
            )
        ctx = OrgContext(org_id=org.id)
        waiting = await learning.propose(db, ctx)
        await learning.decide(
            db, ctx, lesson_id=_removal(waiting).id, approve=False, decided_by="owner@example.com"
        )
        await db.commit()

        again = await learning.propose(db, ctx)
        await db.commit()
        assert all(row.state == "proposed" for row in again)
        assert not any("hope this finds you well" in row.detail for row in again)

    async def test_another_workspaces_edits_are_not_counted(self, db, org, lead_factory):
        """ADR-004: one workspace's reviewers do not get to teach another's drafter."""
        from app.db.models import Campaign, Lead, Org

        other = Org(name="Theirs")
        db.add(other)
        await db.flush()
        campaign = Campaign(
            org_id=other.id, name="Theirs", status="running", persona={"sender_name": "X"}
        )
        db.add(campaign)
        await db.flush()
        # A lead per pair: drafts are unique on (lead, kind, version), so three edits to one
        # lead would be three version-1 rows.
        for _ in range(3):
            lead = Lead(
                org_id=other.id,
                campaign_id=campaign.id,
                email_norm=f"{uuid.uuid4().hex[:8]}@example.com",
                email_raw="X@example.com",
            )
            db.add(lead)
            await db.flush()
            await _edited_pair(db, other, lead, before=f"{BOILERPLATE} Theirs.", after="Theirs.")

        found = await learning.propose(db, OrgContext(org_id=org.id))
        await db.commit()
        assert found == []


class TestTheRoutes:
    async def test_recompute_needs_admin(self, api, member):
        assert api.post("/api/v1/admin/lessons/recompute", headers=member).status_code == 403

    async def test_the_list_shows_the_evidence(self, api, db, org, owner, lead_factory):
        """A pattern without its evidence is an assertion, and this one is about to become an
        instruction to a model."""
        for _ in range(3):
            lead = await lead_factory()
            await _edited_pair(
                db, org, lead, before=f"{BOILERPLATE} Hello there.", after="Hello there."
            )

        api.post("/api/v1/admin/lessons/recompute", headers=owner)
        body = api.get("/api/v1/admin/lessons", headers=owner).json()
        assert body["proposed"]
        assert body["proposed"][0]["evidence"]

    async def test_approving_moves_it_to_approved(self, api, db, org, owner, lead_factory):
        for _ in range(3):
            lead = await lead_factory()
            await _edited_pair(
                db, org, lead, before=f"{BOILERPLATE} Hello there.", after="Hello there."
            )
        api.post("/api/v1/admin/lessons/recompute", headers=owner)
        listed = api.get("/api/v1/admin/lessons", headers=owner).json()
        lesson_id = listed["proposed"][0]["id"]

        decided = api.post(
            f"/api/v1/admin/lessons/{lesson_id}", json={"approve": True}, headers=owner
        )
        assert decided.status_code == 200
        assert decided.json()["state"] == "approved"

        row = await db.scalar(select(DraftLesson).where(DraftLesson.id == uuid.UUID(lesson_id)))
        assert row is not None and row.decided_by


def test_the_prompt_takes_lessons_and_the_handler_passes_them() -> None:
    """The producer check this project adds after finding five things built and never wired:
    `fit.score_fit` had no caller, `messages.variant` had no writer, `search` was passed to
    nothing. A lesson nobody reads is the same defect."""
    import inspect

    from app.services.drafting import generator, prompts
    from app.worker import handlers

    assert "lessons" in inspect.signature(prompts.build_draft_prompt).parameters
    assert "lessons" in inspect.signature(generator.generate_draft).parameters
    assert "learning.instructions" in inspect.getsource(handlers.handle_draft_lead)
