"""The campaign persona and settings.

These are the structural fix for A3 and A4. The prototype asked the model for "our product and
traction" while supplying no facts about the sender — so the model invented them and they were
emailed to real people — and hardcoded the whole thing to funding and venture capital.
"""

from __future__ import annotations

from datetime import date, time

import pytest
from pydantic import ValidationError

from app.domain.campaign import DEFAULT_SEND_DAYS, CampaignSettings
from app.domain.persona import SPAM_TRIGGERS, Persona, ProofPoint

MINIMAL = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Acme Software",
    "offer": "Fixed-scope web and data projects, delivered to a deadline",
    "goal": "book a 20-minute call",
    "booking_url": "https://cal.example.com/asha",
}


class TestPersonaBasics:
    def test_a_minimal_persona_is_valid(self):
        persona = Persona(**MINIMAL)
        assert persona.sender_name == "Asha Rao"
        assert persona.max_words == 120

    def test_the_sender_must_be_named(self):
        """The one field no email can do without: it signs the message and it is in the footer,
        so a recipient can always tell who wrote to them. That is a compliance requirement."""
        with pytest.raises(ValidationError):
            Persona(**{**MINIMAL, "sender_name": ""})

    def test_everything_else_about_the_sender_is_optional(self):
        """**Deliberately relaxed (ADR-013).** `agency_name`, `offer` and `goal` were required
        because the *research* drafter needs them -- it writes on our behalf, so it has to be
        told who that is. A `template`-mode campaign never calls a model, and somebody
        job-hunting has no agency and no offer, so requiring them meant typing something to get
        past a form. A persona filled in to satisfy a validator is a persona the drafter would
        go on to quote, which is the opposite of what hard rule 6 is for."""
        persona = Persona(sender_name="Harish Chand")
        assert persona.agency_name == ""
        assert persona.offer == ""
        assert persona.booking_url is None

    def test_unknown_fields_are_refused(self):
        """A typo'd field would otherwise be silently ignored and quietly do nothing."""
        with pytest.raises(ValidationError):
            Persona(**MINIMAL, sendr_name="typo")


class TestWhatModelWrittenDraftingNeeds:
    """The requirement is real; it belongs to the drafter that has it rather than to the data.

    As two model validators it applied to every persona that exists, so a template-mode campaign
    was refused for not naming an agency it does not have. `missing_for_drafting()` is asked by
    `POST /campaigns` for a research campaign, and by `handle_draft_lead` before a model is
    called at all.
    """

    def test_a_complete_persona_needs_nothing(self):
        assert Persona(**MINIMAL).missing_for_drafting() == []

    def test_a_goal_needs_a_concrete_next_step(self):
        """Asking for a call with nowhere to book it is not a usable campaign."""
        without = {k: v for k, v in MINIMAL.items() if k != "booking_url"}
        missing = dict(Persona(**without).missing_for_drafting())
        assert "cta_text" in missing
        assert "next step" in missing["cta_text"]

        # Either one satisfies it.
        assert (
            Persona(**without, cta_text="reply with a time that suits you").missing_for_drafting()
            == []
        )

    def test_each_missing_field_says_why_it_matters(self):
        """ "agency_name is required" sends somebody back to type anything that passes. What the
        field is *for* is the only version that produces a usable answer."""
        missing = dict(Persona(sender_name="Harish Chand").missing_for_drafting())
        assert set(missing) == {"sender_role", "agency_name", "offer", "goal", "cta_text"}
        for field, why in missing.items():
            assert len(why) > 20, (field, why)

    def test_a_template_persona_is_not_judged_by_this(self):
        """It is never asked: nothing calls `missing_for_drafting()` for a template campaign,
        because no model writes its emails."""
        persona = Persona(sender_name="Harish Chand")
        assert persona.sender_name == "Harish Chand"

    def test_a_bad_booking_url_is_refused(self):
        with pytest.raises(ValidationError):
            Persona(**{**MINIMAL, "booking_url": "not-a-url"})


class TestProofPoints:
    def test_citations_use_stable_ids(self):
        """A draft cites `pp1`, the validator resolves it, and the message stores the citation —
        so reordering the list must not change what a past draft claimed."""
        persona = Persona(
            **MINIMAL,
            proof_points=[
                {"id": "pp1", "text": "Shipped a logistics dashboard for a 40-person firm"},
                {"id": "pp2", "text": "Average project delivered in 6 weeks"},
            ],
        )
        assert persona.proof_point("pp1") is not None
        assert "logistics" in persona.proof_point("pp1").text
        assert persona.proof_point("pp99") is None

    def test_ids_must_be_unique(self):
        with pytest.raises(ValidationError, match="unique"):
            Persona(
                **MINIMAL,
                proof_points=[
                    {"id": "pp1", "text": "First"},
                    {"id": "pp1", "text": "Second"},
                ],
            )

    def test_ids_must_follow_the_citation_format(self):
        with pytest.raises(ValidationError):
            ProofPoint(id="proof-1", text="Something")

    def test_the_next_id_never_reuses_a_retired_one(self):
        """Deleting pp2 must not let a new point become pp2 and inherit old citations."""
        persona = Persona(
            **MINIMAL,
            proof_points=[{"id": "pp1", "text": "A"}, {"id": "pp3", "text": "C"}],
        )
        assert persona.next_proof_point_id() == "pp4"

    def test_the_first_id_is_pp1(self):
        assert Persona(**MINIMAL).next_proof_point_id() == "pp1"

    def test_control_characters_are_stripped_out(self):
        """Proof point text reaches an email header's vicinity; CR/LF has no business there."""
        with pytest.raises(ValidationError):
            ProofPoint(id="pp1", text="Line one\r\nBcc: someone@example.com")


class TestForbiddenPhrases:
    def test_spam_triggers_are_always_forbidden(self):
        persona = Persona(**MINIMAL)
        assert "act now" in persona.forbidden_phrases
        assert SPAM_TRIGGERS <= persona.forbidden_phrases

    def test_a_campaign_can_forbid_more(self):
        persona = Persona(**MINIMAL, avoid=["Synergy", "LEVERAGE"])
        assert "synergy" in persona.forbidden_phrases
        assert "leverage" in persona.forbidden_phrases

    def test_the_avoid_list_is_normalised_and_deduplicated(self):
        persona = Persona(**MINIMAL, avoid=["  Synergy ", "synergy", "SYNERGY", ""])
        assert persona.avoid == ["synergy"]


class TestCampaignSettings:
    def test_the_defaults_are_the_locked_product_rules(self):
        settings = CampaignSettings()
        assert settings.timezone == "Asia/Kolkata"
        assert tuple(settings.send_days) == DEFAULT_SEND_DAYS, "weekdays only"
        assert settings.window_start == time(10, 0)
        assert settings.window_end == time(17, 30)
        assert settings.daily_cap == 100
        assert settings.followup_after_days == 3
        assert settings.max_followups == 1
        assert settings.approval_mode == "review_all"

    def test_auto_send_cannot_be_enabled_through_settings(self):
        """Designed for, deliberately not enabled. Turning it on must be a reviewed code
        change, not a settings edit."""
        with pytest.raises(ValidationError, match="review_all"):
            CampaignSettings(approval_mode="auto_send_if_confidence")

    def test_an_unknown_timezone_fails_here_rather_than_at_send_time(self):
        with pytest.raises(ValidationError, match="unknown timezone"):
            CampaignSettings(timezone="Mars/Olympus_Mons")

    def test_asia_kolkata_is_actually_available(self):
        """Windows ships no tz database, which is why tzdata is a hard dependency."""
        assert CampaignSettings().zone.key == "Asia/Kolkata"

    def test_the_window_must_be_a_real_interval(self):
        with pytest.raises(ValidationError, match="must be before"):
            CampaignSettings(window_start=time(18, 0), window_end=time(10, 0))
        with pytest.raises(ValidationError, match="must be before"):
            CampaignSettings(window_start=time(10, 0), window_end=time(10, 0))

    def test_send_days_cannot_be_empty(self):
        with pytest.raises(ValidationError, match="never send"):
            CampaignSettings(send_days=[])

    def test_send_days_are_sorted_and_deduplicated(self):
        assert CampaignSettings(send_days=[4, 0, 0, 2]).send_days == [0, 2, 4]

    def test_weekends_are_not_send_days(self):
        settings = CampaignSettings()
        assert settings.is_send_day(date(2026, 10, 9)) is True  # Friday
        assert settings.is_send_day(date(2026, 10, 10)) is False  # Saturday
        assert settings.is_send_day(date(2026, 10, 11)) is False  # Sunday
        assert settings.is_send_day(date(2026, 10, 12)) is True  # Monday

    def test_a_listed_holiday_is_not_a_send_day(self):
        diwali = date(2026, 11, 8)
        settings = CampaignSettings(holidays=[diwali])
        assert settings.is_send_day(diwali) is False

    def test_the_window_length_is_computed_for_pacing(self):
        """Sends are spread across the window rather than bursting at the start."""
        assert CampaignSettings().window_seconds == int(7.5 * 3600)

    def test_a_cap_of_zero_is_refused(self):
        with pytest.raises(ValidationError):
            CampaignSettings(daily_cap=0)

    def test_followups_can_be_switched_off(self):
        """max_followups=0 is legitimate: some campaigns are a single touch."""
        assert CampaignSettings(max_followups=0).max_followups == 0
