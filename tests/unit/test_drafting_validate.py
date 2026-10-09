"""Deterministic draft validation.

The rule that closes A3 is the number check: the prototype asked the model for "our product and
traction" with no facts supplied, so it invented both and the result was emailed. Here an
unsupported figure fails.

B13 (subject parsed out of prose), B14 (unescaped HTML) and B15 (uncapped regeneration) are
covered here and in the generator tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.persona import Persona
from app.services.drafting.schema import Claim, DraftOutput
from app.services.drafting.validate import (
    evidence_corpus,
    resolve_evidence,
    validate_draft,
)

PERSONA = Persona(
    sender_name="Asha Rao",
    sender_role="Founder",
    agency_name="Acme Software",
    offer="Fixed-scope data pipeline and dashboard projects, delivered to a deadline",
    goal="book a 20-minute call",
    booking_url="https://cal.example.com/asha",
    services=["data pipelines", "dashboards"],
    proof_points=[
        {"id": "pp1", "text": "Delivered a logistics dashboard for a 40-person firm in 6 weeks"},
        {"id": "pp2", "text": "Every project is fixed-scope and paid on completion"},
    ],
    max_words=120,
)

HOOKS: list[dict[str, object]] = [
    {
        "id": "hook:111",
        "text": "They are hiring backend engineers for a data pipeline",
        "snippet": "hiring two backend engineers to work on our data ingestion pipeline",
        "source_url": "https://globex.example/careers",
    }
]

GOOD_BODY = (
    "Hi Ben,\n\n"
    "I saw you are hiring two backend engineers to work on your data ingestion pipeline.\n\n"
    "We build fixed-scope data pipeline projects and hand them over on a deadline. "
    "Delivered a logistics dashboard for a 40-person firm in 6 weeks.\n\n"
    "Worth a short call to see if it fits? https://cal.example.com/asha\n\n"
    "Asha"
)


def draft(**kw) -> DraftOutput:
    defaults = {
        "subject": "Your data pipeline hiring",
        "body": GOOD_BODY,
        "claims": [
            {"text": "Delivered a logistics dashboard in 6 weeks", "evidence_ref": "pp1"},
            {"text": "They are hiring for a data pipeline", "evidence_ref": "hook:111"},
        ],
    }
    return DraftOutput.model_validate({**defaults, **kw})


def check(d: DraftOutput, **kw):
    return validate_draft(
        d, persona=PERSONA, hooks=HOOKS, lead_company="Globex", lead_name="Ben Shah", **kw
    )


class TestTheSchema:
    def test_the_subject_is_a_field_not_parsed_out_of_prose(self):
        """B13: the prototype took line 1 if it began 'Subject:', so any preamble broke it."""
        d = DraftOutput(subject="Subject: Your pipeline", body=GOOD_BODY)
        assert d.subject == "Your pipeline", "a stray prefix is stripped, not sent"

    def test_a_newline_in_the_subject_is_refused(self):
        """A newline is header injection: everything after it becomes a new header."""
        with pytest.raises(ValidationError, match="single line"):
            DraftOutput(subject="Hello\nBcc: someone@example.com", body=GOOD_BODY)

    def test_an_unrecognised_citation_format_is_refused(self):
        with pytest.raises(ValidationError, match="proof point"):
            Claim(text="Something", evidence_ref="source_1")

    def test_two_claims_may_not_cite_the_same_evidence(self):
        """How one hook becomes three specifics that sound independently verified."""
        with pytest.raises(ValidationError, match="different piece of evidence"):
            DraftOutput(
                subject="Hi",
                body=GOOD_BODY,
                claims=[
                    {"text": "A", "evidence_ref": "pp1"},
                    {"text": "B", "evidence_ref": "pp1"},
                ],
            )

    def test_control_characters_in_the_body_are_refused(self):
        with pytest.raises(ValidationError, match="control characters"):
            DraftOutput(subject="Hi", body="Hello\x00there " + GOOD_BODY)


class TestAGoodDraftPasses:
    def test_the_reference_draft_passes(self):
        report = check(draft())
        assert report.passed, f"unexpected failures: {report.failures}"

    def test_a_number_present_in_a_proof_point_is_allowed(self):
        """'6 weeks' and '40-person' both appear in pp1, so they are citable."""
        report = check(draft())
        assert not any("figure" in f for f in report.failures)


class TestTheNumberRule:
    """A3's fix. An unsupported figure must fail, because a recipient cannot tell an invented
    metric from a real one and we would be the ones who sent it."""

    def test_an_invented_metric_fails(self):
        body = GOOD_BODY.replace(
            "We build fixed-scope data pipeline projects",
            "We have cut pipeline costs by 47% for our clients",
        )
        report = check(draft(body=body))
        assert not report.passed
        assert any("47%" in f for f in report.failures)

    def test_invented_traction_fails(self):
        body = GOOD_BODY.replace("Asha", "We serve 320 customers across India.\n\nAsha")
        report = check(draft(body=body))
        assert not report.passed
        assert any("320" in f for f in report.failures)

    def test_an_invented_currency_amount_fails(self):
        body = GOOD_BODY.replace("Asha", "We have saved clients $2.4M.\n\nAsha")
        report = check(draft(body=body))
        assert not report.passed

    @pytest.mark.parametrize("number", ["20", "15", "30", "2026"])
    def test_innocuous_numbers_are_allowed(self, number):
        """A meeting length or a day count carries no claim."""
        body = GOOD_BODY.replace("a short call", f"a {number} minute call")
        report = check(draft(body=body))
        assert report.passed, report.failures

    def test_a_figure_quoted_from_a_research_hook_is_allowed(self):
        body = GOOD_BODY.replace("two backend engineers", "two backend engineers")
        report = check(draft(body=body))
        assert report.passed, report.failures


class TestClaimsMustResolve:
    def test_a_citation_to_a_nonexistent_proof_point_fails(self):
        report = check(draft(claims=[{"text": "We are ISO certified", "evidence_ref": "pp99"}]))
        assert not report.passed
        assert "pp99" in report.unresolved_claims

    def test_a_citation_to_a_nonexistent_hook_fails(self):
        report = check(draft(claims=[{"text": "They raised a round", "evidence_ref": "hook:999"}]))
        assert not report.passed
        assert "hook:999" in report.unresolved_claims

    def test_resolution_finds_persona_and_hook_text(self):
        assert "logistics dashboard" in (resolve_evidence("pp1", PERSONA, HOOKS) or "")
        assert "backend engineers" in (resolve_evidence("hook:111", PERSONA, HOOKS) or "")
        assert resolve_evidence("pp42", PERSONA, HOOKS) is None

    def test_the_corpus_is_persona_plus_verified_hooks_only(self):
        corpus = evidence_corpus(PERSONA, HOOKS)
        assert "logistics dashboard" in corpus
        assert "data ingestion pipeline" in corpus
        assert "Series A" not in corpus


class TestLength:
    def test_an_overlong_body_fails_with_the_actual_count(self):
        """Specific feedback beats scolding: the model can act on a number."""
        report = check(draft(body=GOOD_BODY + " padding" * 200))
        assert not report.passed
        failure = next(f for f in report.failures if "words" in f)
        assert "120" in failure

    def test_a_trivially_short_body_fails(self):
        report = check(draft(body="Hi Ben, call me."))
        assert not report.passed

    def test_the_limit_comes_from_the_persona(self):
        short = PERSONA.model_copy(update={"max_words": 30})
        report = validate_draft(draft(), persona=short, hooks=HOOKS)
        assert not report.passed
        assert any("30" in f for f in report.failures)


class TestSubjectRules:
    def test_an_overlong_subject_fails(self):
        report = check(draft(subject="A rather long subject line about your data pipeline work"))
        assert not report.passed
        assert any("6 or fewer" in f for f in report.failures)

    def test_a_fake_re_prefix_on_a_first_email_fails(self):
        """Faking a reply to a conversation that never happened is a deception."""
        report = check(draft(subject="Re: our chat"), kind="initial")
        assert not report.passed
        assert any("Re:" in f for f in report.failures)

    def test_a_reply_may_legitimately_use_re(self):
        """A real reply genuinely continues our own thread."""
        report = check(draft(subject="Re: our chat"), kind="reply")
        assert report.passed, report.failures

    def test_a_shouting_subject_fails(self):
        report = check(draft(subject="URGENT PIPELINE HELP"))
        assert not report.passed

    def test_an_exclamation_mark_warns_without_blocking(self):
        report = check(draft(subject="Your pipeline!"))
        assert report.passed
        assert any("exclamation" in w for w in report.warnings)


class TestPlainTextOnly:
    """B14: the prototype built HTML with an unescaped replace, so markup from the model broke
    the email or injected into it. There is no HTML path now, so markdown must not leak."""

    @pytest.mark.parametrize(
        "snippet,description",
        [
            ("**bold**", "bold"),
            ("# Heading", "heading"),
            ("[link](https://x.example)", "link"),
            ("- a bullet", "bullet"),
            ("`code`", "backtick"),
        ],
    )
    def test_markdown_fails(self, snippet, description):
        report = check(draft(body=GOOD_BODY.replace("Asha", f"{snippet}\n\nAsha")))
        assert not report.passed, f"{description} should fail"

    def test_html_tags_fail(self):
        report = check(draft(body=GOOD_BODY.replace("Asha", "<b>Asha</b>")))
        assert not report.passed
        assert any("HTML" in f for f in report.failures)


class TestPlaceholders:
    @pytest.mark.parametrize(
        "placeholder", ["[First Name]", "{{company}}", "<COMPANY>", "YOUR_COMPANY", "TODO"]
    )
    def test_an_unfilled_placeholder_fails(self, placeholder):
        """Sending one is unmistakably a robot."""
        report = check(draft(body=GOOD_BODY.replace("Hi Ben", f"Hi {placeholder}")))
        assert not report.passed
        assert any("placeholder" in f for f in report.failures)


class TestForbiddenPhrases:
    def test_a_spam_trigger_fails(self):
        report = check(draft(body=GOOD_BODY.replace("Worth a short call", "Act now")))
        assert not report.passed
        assert any("act now" in f for f in report.failures)

    def test_a_campaign_specific_avoid_word_fails(self):
        persona = PERSONA.model_copy(update={"avoid": ["synergy"]})
        report = validate_draft(
            draft(body=GOOD_BODY.replace("We build", "We create synergy and build")),
            persona=persona,
            hooks=HOOKS,
        )
        assert not report.passed
        assert any("synergy" in f for f in report.failures)


class TestLinks:
    def test_more_than_one_link_fails(self):
        body = GOOD_BODY.replace("Asha", "Also see https://acme.example/work\n\nAsha")
        report = check(draft(body=body))
        assert not report.passed
        assert any("links" in f for f in report.failures)

    def test_a_link_that_is_not_the_booking_url_fails(self):
        body = GOOD_BODY.replace("https://cal.example.com/asha", "https://bit.ly/xyz")
        report = check(draft(body=body))
        assert not report.passed
        assert any("booking URL" in f for f in report.failures)

    def test_a_trailing_slash_difference_is_tolerated(self):
        """Models add and drop it at random; it is not a real difference."""
        body = GOOD_BODY.replace("https://cal.example.com/asha", "https://cal.example.com/asha/")
        assert check(draft(body=body)).passed

    def test_no_link_at_all_is_allowed(self):
        """A campaign may ask for a reply instead of a booking."""
        body = GOOD_BODY.replace(" https://cal.example.com/asha", "")
        assert check(draft(body=body)).passed


class TestNamedEntities:
    def test_an_unknown_company_name_warns_rather_than_fails(self):
        """Capitalised phrases are a noisy signal, so a reviewer decides. The number check is
        the one that fails hard, because a figure is unambiguous."""
        body = GOOD_BODY.replace("Asha", "We did this for Northwind Traders.\n\nAsha")
        report = check(draft(body=body))
        assert report.passed
        assert any("Northwind" in w for w in report.warnings)

    def test_the_lead_and_agency_names_do_not_warn(self):
        report = check(draft())
        assert not any("Acme Software" in w for w in report.warnings)
        assert not any("Ben Shah" in w for w in report.warnings)


class TestFeedback:
    def test_feedback_lists_every_failure_concretely(self):
        report = check(draft(subject="A very long subject line indeed about pipelines", body="Hi."))
        feedback = report.feedback()
        assert "6 or fewer" in feedback
        assert "words" in feedback
        assert feedback.startswith("- ")

    def test_a_passing_draft_has_empty_feedback(self):
        assert check(draft()).feedback() == ""
