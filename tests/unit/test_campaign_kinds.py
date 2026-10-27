"""Two kinds, one engine.

`docs/CAMPAIGN_KINDS.md` is the analysis. The tests that matter are the ones proving the seam is
in the right place: an existing `outreach` campaign must behave **identically**, and a `job_hunt`
one must be unable to claim anything the candidate did not write down.

The second is the one with teeth. An email claiming three years of Kubernetes the candidate does
not have is not a weak email -- it is a lie told in their name, which they will be asked about in
an interview.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.kinds import (
    KINDS,
    CampaignKind,
    defaults_for,
    load_sender,
    opt_out_line_for,
)
from app.domain.persona import Persona


def agency(**over) -> dict:
    base = {
        "sender_name": "Asha Rao",
        "sender_role": "Founder",
        "agency_name": "Acme Software",
        "offer": "fixed-scope data pipeline projects",
        "goal": "book a 20-minute call",
        "cta_text": "Worth a short call?",
        "proof_points": [{"id": "pp1", "text": "We shipped a pipeline for Globex in six weeks"}],
    }
    base.update(over)
    return base


def candidate(**over) -> dict:
    base = {
        "kind": "job_hunt",
        "full_name": "Priya Nair",
        "headline": "Backend engineer, 4 years",
        "target_role": "backend engineering roles at early-stage startups",
        "facts": [
            {"id": "pp1", "text": "Four years building Django services at Globex"},
            {"id": "pp2", "text": "Ran the migration from MySQL to Postgres for 40M rows"},
        ],
        "goal": "a short conversation about backend openings",
        "cta_text": "Open to a quick chat?",
    }
    base.update(over)
    return base


class TestNothingChangesForExistingCampaigns:
    def test_a_persona_with_no_kind_is_outreach(self):
        """Every row in the database today has no `kind`. All of them must still load."""
        sender = load_sender(agency(), kind=CampaignKind.OUTREACH)
        assert isinstance(sender, Persona)
        assert sender.sender_name == "Asha Rao"

    def test_outreach_defaults_are_todays_values(self):
        """The migration changes no behaviour because the defaults are what they already were."""
        defaults = defaults_for(CampaignKind.OUTREACH)
        assert defaults.daily_cap == 100
        assert defaults.max_followups == 1

    def test_an_outreach_persona_keeps_its_drafting_requirements(self):
        sender = load_sender(agency(agency_name=""), kind=CampaignKind.OUTREACH)
        missing = dict(sender.missing_for_drafting())
        assert "agency_name" in missing


class TestACandidateIsNotAnAgency:
    def test_a_candidate_profile_needs_no_agency(self):
        """ADR-013's bug, which would come straight back if the requirement were shared.

        Requiring an agency from somebody who has none forces them to type something that
        passes -- and a field filled in to satisfy a validator is a field the drafter will
        quote.
        """
        sender = load_sender(candidate(), kind=CampaignKind.JOB_HUNT)
        assert dict(sender.missing_for_drafting()) == {}

    def test_a_candidate_with_no_facts_cannot_draft(self):
        """With nothing to cite, a model has only invention left."""
        sender = load_sender(candidate(facts=[]), kind=CampaignKind.JOB_HUNT)
        assert "facts" in dict(sender.missing_for_drafting())

    def test_the_candidates_name_signs_the_email(self):
        assert load_sender(candidate(), kind=CampaignKind.JOB_HUNT).sender_name == "Priya Nair"

    def test_job_hunt_sends_less_by_default(self):
        """A personal Gmail has no domain reputation and no warm-up history."""
        assert defaults_for(CampaignKind.JOB_HUNT).daily_cap == 25
        assert (
            defaults_for(CampaignKind.JOB_HUNT).daily_cap
            < defaults_for(CampaignKind.OUTREACH).daily_cap
        )

    def test_the_opt_out_wording_differs(self):
        """A job enquiry borrowing sales language reads as insincere."""
        assert opt_out_line_for(CampaignKind.JOB_HUNT) != opt_out_line_for(CampaignKind.OUTREACH)

    def test_both_kinds_still_offer_an_opt_out(self):
        """Different register, identical obligation. Neither kind may ship without one."""
        for kind in KINDS:
            assert opt_out_line_for(kind).strip()


class TestTheGroundingRuleIsTheSameMachinery:
    def test_candidate_facts_become_citable_proof_points(self):
        """The whole point of the mapping.

        `evidence_corpus`, `resolve_evidence` and `_check_claims_resolve` then work untouched,
        so a job_hunt draft inventing experience fails the same check, with the same message, as
        an outreach draft inventing a statistic. One rule, one place, two kinds.
        """
        sender = load_sender(candidate(), kind=CampaignKind.JOB_HUNT)
        ids = {point.id for point in sender.proof_points}
        assert ids == {"pp1", "pp2"}
        assert any("Postgres" in point.text for point in sender.proof_points)

    def test_a_fact_id_must_have_the_citable_shape(self):
        """An id the claim checker cannot resolve is evidence that cannot be cited."""
        with pytest.raises(ValidationError):
            load_sender(
                candidate(facts=[{"id": "fact-one", "text": "Four years of Django"}]),
                kind=CampaignKind.JOB_HUNT,
            )

    def test_nothing_outside_the_profile_enters_the_corpus(self):
        """The closed set is what makes "only what they wrote down" enforceable."""
        sender = load_sender(candidate(), kind=CampaignKind.JOB_HUNT)
        assert sender.agency_name == ""
        texts = " ".join(point.text for point in sender.proof_points)
        assert "Kubernetes" not in texts


class TestKindAndModeAreOrthogonal:
    def test_every_kind_supports_both_content_modes(self):
        """Four combinations, one pipeline. `mode` already *is* researched vs fixed draft."""
        for kind in KINDS:
            assert set(defaults_for(kind).modes) == {"research", "template"}

    def test_an_unknown_kind_is_refused_rather_than_guessed(self):
        with pytest.raises(ValueError):
            defaults_for("fundraising")  # type: ignore[arg-type]
