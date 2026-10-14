"""The merge-field renderer for `template`-mode campaigns.

Pure functions over pure data, so every rule here is tested without a database. The rules that
earn their own tests are the ones whose failure mode is a real email to a real person:

- an unresolved placeholder never renders as a gap or as its own braces, it fails the draft
- a spreadsheet cell cannot introduce a placeholder, because substitution is one pass
- a template that uses some other tool's syntax is refused when it is written, not when it is
  received
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.template import (
    CampaignTemplates,
    EmailTemplate,
    FollowupTemplate,
    build_context,
    first_name_of,
    last_name_of,
    placeholders_in,
    render,
    suspect_placeholders_in,
    tidy,
)

PERSONA = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Arkvane",
    "booking_url": "https://cal.example.com/asha",
    "offer": "a two-week prototype",
    "proof_points": [{"id": "pp1", "text": "secret"}],
}


def context_for(**kwargs: object) -> dict[str, str]:
    base: dict[str, object] = {
        "full_name": "Sanket Yadav",
        "email": "sanket@zenith.example",
        "company_name": "Zenith Logistics",
        "job_title": "Founder",
        "persona": PERSONA,
    }
    base.update(kwargs)
    return build_context(**base)  # type: ignore[arg-type]


class TestNames:
    def test_the_first_word_is_the_first_name(self):
        assert first_name_of("Sanket Yadav") == "Sanket"

    def test_an_honorific_is_not_a_first_name(self):
        """ "Hi Dr," is a worse opening than no name at all."""
        assert first_name_of("Dr. Meera Iyer") == "Meera"
        assert first_name_of("Shri Ram Prasad") == "Ram"

    def test_a_one_word_name_has_no_last_name(self):
        """Returning the same word twice would sign the email "Hi Madhavan Madhavan"."""
        assert first_name_of("Madhavan") == "Madhavan"
        assert last_name_of("Madhavan") == ""

    def test_a_comma_separated_name_still_parses(self):
        assert first_name_of("Yadav, Sanket") == "Yadav"

    def test_nothing_is_not_a_name(self):
        assert first_name_of(None) == ""
        assert first_name_of("   ") == ""


class TestTheContext:
    def test_lead_fields_and_derived_fields_are_present(self):
        context = context_for()
        assert context["first_name"] == "Sanket"
        assert context["company"] == "Zenith Logistics"
        assert context["email"] == "sanket@zenith.example"

    def test_a_sheet_column_wins_over_our_guess(self):
        """Somebody who mapped a first_name column knows their own list better than we do."""
        context = context_for(extra={"first_name": "Sanny"})
        assert context["first_name"] == "Sanny"

    def test_an_empty_sheet_column_does_not_blank_our_guess(self):
        """The other half of the same rule, and the one that would have shipped broken: an
        unfilled column must not beat a value we could derive."""
        context = context_for(extra={"first_name": "   "})
        assert context["first_name"] == "Sanket"

    def test_a_sheet_header_becomes_a_field_name(self):
        context = context_for(extra={"Funding Stage": "Series A"})
        assert context["funding_stage"] == "Series A"

    def test_only_safe_persona_fields_are_exposed(self):
        """`proof_points` is the list of claims the *drafter* may make, not a merge field, and
        a template that could read arbitrary persona keys would be a way to leak them."""
        context = context_for()
        assert "proof_points" not in context
        assert context["sender_name"] == "Asha Rao"

    def test_control_characters_never_survive_a_cell(self):
        """A carriage return in a cell is two headers in a mailer that does not fold them."""
        context = context_for(company_name="Zenith\r\nBcc: someone@example.com")
        assert "\r" not in context["company"]
        assert "\n" not in context["company"]

    def test_a_number_is_rendered_as_text(self):
        context = context_for(extra={"headcount": 25})
        assert context["headcount"] == "25"


class TestRendering:
    def test_a_placeholder_is_replaced(self):
        text, missing = render("Hi {{first_name}}, about {{company}}", context_for())
        assert text == "Hi Sanket, about Zenith Logistics"
        assert missing == []

    def test_whitespace_inside_the_braces_is_tolerated(self):
        text, _ = render("Hi {{ first_name }}", context_for())
        assert text == "Hi Sanket"

    def test_a_missing_field_is_reported_and_left_visible(self):
        """Neither "Hi ," nor a silent drop: the reviewer has to be able to see the hole, and
        the draft has to be unsendable. `missing` is what makes the second part true."""
        text, missing = render("Hi {{first_name}} at {{industry}}", context_for())
        assert missing == ["industry"]
        assert "{{industry}}" in text

    def test_a_fallback_is_used_when_there_is_no_value(self):
        text, missing = render("Hi {{first_name|there}}", build_context(full_name=None))
        assert text == "Hi there"
        assert missing == []

    def test_an_empty_fallback_is_an_explicit_decision(self):
        """`{{job_title|}}` means the operator wants nothing there, which is not the same as
        forgetting to handle it. `render` substitutes and nothing more -- the gap it leaves is
        tidied by `tidy`, which is where that concern belongs."""
        text, missing = render("Role: {{job_title|}}", build_context())
        assert text == "Role: "
        assert missing == []


class TestWhitespaceAfterSubstitution:
    """The visible tell of a template is a double space or a dangling line, and the one thing a
    personalised email must not look like is a template."""

    def test_a_gap_left_by_an_empty_value_is_closed(self):
        assert tidy("Hi {{x}} there".replace("{{x}}", "")) == "Hi there"

    def test_trailing_whitespace_goes_line_by_line(self):
        assert tidy("Role: \nCompany: \n") == "Role:\nCompany:"

    def test_paragraph_breaks_are_the_operators_formatting(self):
        assert tidy("Hi,\n\nFirst line.\n\nRegards") == "Hi,\n\nFirst line.\n\nRegards"

    def test_a_rendered_email_is_tidied(self):
        template = EmailTemplate(subject="Hi", body="Role: {{job_title|}}\nHi {{first_name}}")
        rendered = template.render_for(context_for(job_title=None))
        assert rendered.body == "Role:\nHi Sanket"

    def test_a_cell_cannot_introduce_a_placeholder(self):
        """The template is the operator's, the values are a spreadsheet's. One substitution
        pass makes a value inert by construction rather than by escaping it."""
        context = context_for(company_name="{{booking_url}}")
        text, missing = render("About {{company}}", context)
        assert text == "About {{booking_url}}"
        assert missing == []

    def test_nothing_is_evaluated(self):
        """No expressions and no attribute access. These are not merge fields and must survive
        as the characters they are."""
        for hostile in ("{{ 7*7 }}", "{{self.__class__}}", "${company}", "{%for x in y%}"):
            text, _ = render(hostile, context_for())
            assert text == hostile

    def test_the_fields_a_template_needs_can_be_listed(self):
        assert placeholders_in("Hi {{first_name}} at {{company|your company}}") == {
            "first_name",
            "company",
        }


class TestNearMissSyntaxIsRefused:
    @pytest.mark.parametrize(
        "body",
        [
            "Hi {first_name},",
            "Hi {{first name}},",
            "Hi %FIRST_NAME%,",
            "Hi ${first_name},",
        ],
    )
    def test_another_tools_syntax_is_refused_when_written(self, body):
        """Every one of these is a shape operators arrive with. Accepting them means emailing
        them verbatim, and the first person to find out is a recipient."""
        with pytest.raises(ValidationError, match="not a merge field"):
            EmailTemplate(subject="Quick question", body=body)

    def test_a_real_placeholder_is_not_a_suspect(self):
        assert suspect_placeholders_in("Hi {{first_name|there}}, about {{company}}") == []

    def test_a_literal_brace_is_still_refused_rather_than_guessed(self):
        """`{json}` in a cold email is almost always a broken merge field. Refusing is the safe
        reading; an operator who means it can write it a different way."""
        assert suspect_placeholders_in("send me {json}")


class TestTheTemplateModel:
    def test_a_subject_may_not_be_multi_line(self):
        with pytest.raises(ValidationError, match="single line"):
            EmailTemplate(subject="Hi\nBcc: someone@example.com", body="Hello")

    def test_an_empty_body_is_refused(self):
        with pytest.raises(ValidationError):
            EmailTemplate(subject="Hi", body="")

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(ValidationError):
            EmailTemplate(subject="Hi", body="Hello", bdy="typo")

    def test_rendering_reports_the_reason_a_reviewer_needs(self):
        template = EmailTemplate(subject="{{company}}", body="Hi {{first_name}}")
        rendered = template.render_for(build_context())
        assert not rendered.ok
        assert "company" in rendered.reason
        assert "first_name" in rendered.reason
        assert "fallback" in rendered.reason, "the reason has to say how to fix it"

    def test_a_complete_render_is_ok(self):
        template = EmailTemplate(
            subject="{{company}} and {{agency_name}}",
            body="Hi {{first_name}},\n\n{{offer}}?\n\n{{sender_name}}",
        )
        rendered = template.render_for(context_for())
        assert rendered.ok
        assert rendered.subject == "Zenith Logistics and Arkvane"
        assert "Hi Sanket," in rendered.body
        assert rendered.body.endswith("Asha Rao")


class TestCampaignTemplates:
    def test_every_field_across_every_template_is_listed(self):
        templates = CampaignTemplates(
            initial=EmailTemplate(subject="Hi {{first_name}}", body="About {{company}}"),
            followups=[FollowupTemplate(subject="Re", body="{{industry}}?", after_days=3)],
        )
        assert templates.fields == {"first_name", "company", "industry"}

    def test_unknown_fields_are_what_the_sheet_must_supply(self):
        templates = CampaignTemplates(
            initial=EmailTemplate(subject="Hi {{first_name}}", body="{{industry}} is busy")
        )
        assert templates.unknown_fields() == {"industry"}
        assert templates.unknown_fields({"Industry"}) == set(), "a header is matched as a slug"

    def test_a_persona_field_is_always_known(self):
        templates = CampaignTemplates(
            initial=EmailTemplate(subject="Hi", body="{{sender_name}} from {{agency_name}}")
        )
        assert templates.unknown_fields() == set()

    def test_asking_for_a_followup_that_was_never_written_is_not_an_error(self):
        """`max_followups` and the number of templates are set in two places. The smaller has
        to win, or the campaign tries to send an email nobody wrote."""
        templates = CampaignTemplates(
            initial=EmailTemplate(subject="Hi", body="Hello"),
            followups=[FollowupTemplate(subject="Re", body="Following up")],
        )
        assert templates.for_followup(0) is not None
        assert templates.for_followup(1) is None
        assert templates.for_followup(-1) is None

    def test_more_than_five_followups_is_refused(self):
        with pytest.raises(ValidationError):
            CampaignTemplates(
                initial=EmailTemplate(subject="Hi", body="Hello"),
                followups=[FollowupTemplate(subject=f"{n}", body="x") for n in range(6)],
            )
