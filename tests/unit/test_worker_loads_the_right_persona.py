"""The worker must load a campaign's sender with the campaign's own kind.

**Written because it did not, and that broke job-hunt campaigns completely.** `_persona()` ran
`Persona.model_validate(campaign.persona)` for every campaign, with no `kind`. A `job_hunt`
campaign stores a `CandidateProfile` -- `full_name`, `facts`, `followup_angle`, and a literal
`kind` field -- so validating it as an outreach `Persona` raised on every single lead:

    ValidationError: 7 validation errors for Persona
      sender_name  Field required
      kind         Extra inputs are not permitted

Every `research_lead` job died, used up its five attempts and landed in `dead`. No research means
no hooks, no hooks means no draft, and an empty review queue reads as "the system is doing
nothing" rather than as a crash -- which is exactly how this survived in production with leads
imported and a campaign running.

`models.py` already said the rule: *"The kind also decides which schema validates `persona`"*. The
API route obeyed it via `load_sender()`; the worker was the one place that did not. So this is the
project's signature defect again -- a correct producer, a correct consumer, and nothing checking
they agreed.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.db.models import Campaign
from app.domain.persona import Persona
from app.worker.handlers import PermanentFailure, _persona

CANDIDATE = {
    "kind": "job_hunt",
    "full_name": "Priya Nair",
    "headline": "Backend engineer, 4 years",
    "target_role": "backend engineering roles at early-stage startups",
    "facts": [{"id": "pp1", "text": "Four years building Django services at Globex"}],
    "goal": "a short conversation about backend openings",
    "cta_text": "Open to a quick chat?",
    "followup_angle": "",
}

AGENCY = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Acme Software",
    "offer": "fixed-scope data pipeline projects",
    "goal": "book a 20-minute call",
    "cta_text": "Worth a short call?",
    "proof_points": [{"id": "pp1", "text": "We shipped a pipeline for Globex in six weeks"}],
}


def campaign(persona: dict, *, kind: str) -> Campaign:
    """A campaign row as the worker receives it. Never saved -- this needs no database."""
    return Campaign(id=uuid.uuid4(), org_id=uuid.uuid4(), name="x", kind=kind, persona=persona)


def test_the_old_code_would_have_failed_here() -> None:
    """Pins the defect itself, so the tests below cannot pass for an unrelated reason.

    If `CandidateProfile` ever grows a `sender_name` and drops `extra="forbid"`, this stops
    raising -- and the tests below would then pass whether or not the fix is present. Better to
    fail here, loudly, than to keep reporting coverage for a bug that changed shape.
    """
    with pytest.raises(ValidationError):
        Persona.model_validate(CANDIDATE)


def test_a_job_hunt_campaign_loads_without_raising() -> None:
    sender = _persona(campaign(CANDIDATE, kind="job_hunt"))

    # The compiled persona signs the email with the candidate's own name.
    assert sender.sender_name == "Priya Nair"


def test_a_job_hunt_campaign_can_be_drafted_for() -> None:
    """The handlers' actual question: is there enough here to ask a model to write?

    It has to be answered for the *profile* -- a candidate has no agency and no offer, so asking
    the compiled persona would start demanding an agency name from a job seeker.
    """
    sender = _persona(campaign(CANDIDATE, kind="job_hunt"))

    assert sender.missing_for_drafting() == []


def test_a_candidate_with_no_facts_is_refused_for_the_candidate_s_reason() -> None:
    """With nothing to cite, a model has only invention left. That is hard rule 6."""
    profile = {**CANDIDATE, "facts": []}

    missing = dict(_persona(campaign(profile, kind="job_hunt")).missing_for_drafting())

    assert "facts" in missing


def test_an_outreach_campaign_is_unchanged() -> None:
    """The half that already worked, pinned so the fix cannot quietly alter it."""
    sender = _persona(campaign(AGENCY, kind="outreach"))

    assert isinstance(sender, Persona)
    assert sender.sender_name == "Asha Rao"
    assert sender.missing_for_drafting() == []


def test_a_campaign_with_no_kind_is_read_as_outreach() -> None:
    """Every row written before kinds existed has `kind` defaulted, not set. All must still load."""
    sender = _persona(campaign(AGENCY, kind=""))

    assert sender.sender_name == "Asha Rao"


def test_a_campaign_with_no_persona_still_fails_permanently() -> None:
    """Nothing may be asserted without one, and retrying an empty column changes nothing."""
    with pytest.raises(PermanentFailure):
        _persona(campaign({}, kind="job_hunt"))


def test_a_persona_that_matches_no_schema_fails_permanently_rather_than_retrying() -> None:
    """A blob that is neither shape is a bad payload, and five attempts at it waste an hour.

    This is what the defect *should* have looked like from the queue's point of view: dead
    immediately, with the reason on the row, instead of five identical crashes.
    """
    with pytest.raises(PermanentFailure):
        _persona(campaign({"nonsense": True}, kind="job_hunt"))
