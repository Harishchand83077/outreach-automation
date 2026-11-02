"""What a tailored resume may say, and what it may not (F20, ADR-043).

The acceptance criterion for this slice is one sentence: *a tailored resume with a number or
skill not in confirmed facts is rejected.* These are the tests that make it true, and most of
them exist because the obvious implementation lets exactly one of these through.
"""

from __future__ import annotations

import pytest

from app.db.models import RESUME_SECTIONS
from app.services.resume.ops import OpError, parse_ops
from app.services.resume.validate import (
    KnownFact,
    check_rendered,
    check_rewording,
    render,
    validate_ops,
)

FACT = KnownFact(
    fact_id="pp1",
    statement="Built the payments API at Acme in Python, handling 2000 requests a minute.",
    confirmed=True,
)
SKILL = KnownFact(fact_id="pp2", statement="Comfortable with Postgres and Docker.", confirmed=True)
PROPOSED = KnownFact(fact_id="pp3", statement="Led a team of four.", confirmed=False)

FACTS = {fact.fact_id: fact for fact in (FACT, SKILL, PROPOSED)}


def _include(fact_id: str, text: str | None = None, section: str = "Experience") -> dict:
    row: dict = {"kind": "include", "fact_id": fact_id, "section": section}
    if text is not None:
        row["text"] = text
    return row


class TestANewClaimIsRefused:
    def test_a_number_the_fact_does_not_contain_is_refused(self) -> None:
        """The headline criterion. 42% is the number nobody can be asked about in an interview
        and answer, because it never happened."""
        report = validate_ops(
            parse_ops([_include("pp1", "Built the payments API, improving latency by 42%.")]),
            FACTS,
        )

        assert not report.passed
        assert any("42" in message for message in report.messages)

    def test_a_skill_the_fact_does_not_contain_is_refused(self) -> None:
        report = validate_ops(
            parse_ops([_include("pp1", "Built the payments API in Python and Kubernetes.")]),
            FACTS,
        )

        assert not report.passed
        assert any("kubernetes" in message.lower() for message in report.messages)

    def test_a_different_employer_is_refused(self) -> None:
        """An employer is the invention nobody notices until a reference is taken up."""
        report = validate_ops(
            parse_ops([_include("pp1", "Built the payments API at Globex in Python.")]), FACTS
        )

        assert not report.passed
        assert any("globex" in message.lower() for message in report.messages)

    def test_a_new_skill_as_the_very_first_word_is_refused(self) -> None:
        """Position in the sentence is deliberately not used.

        Exempting the first token -- the obvious way to let a rewording begin with a capital --
        leaves unchecked the one bullet somebody would actually write: the one that opens on a
        technology they want to be seen to have.
        """
        report = validate_ops(
            parse_ops([_include("pp2", "Kubernetes and Postgres, day to day.")]), FACTS
        )

        assert not report.passed
        assert any("kubernetes" in message.lower() for message in report.messages)

    def test_an_all_capitals_acronym_is_checked(self) -> None:
        report = validate_ops(parse_ops([_include("pp2", "Postgres, Docker and AWS.")]), FACTS)

        assert not report.passed


class TestWhatIsAllowed:
    def test_the_fact_unchanged_is_always_allowed(self) -> None:
        report = validate_ops(parse_ops([_include("pp1")]), FACTS)

        assert report.passed
        assert report.cited == ["pp1"]

    def test_lower_case_rewording_inside_the_fact_is_allowed(self) -> None:
        report = validate_ops(
            parse_ops([_include("pp1", "Built the payments API at Acme in Python.")]), FACTS
        )

        assert report.passed, report.messages

    def test_a_number_written_with_separators_is_the_same_number(self) -> None:
        """2,000 and 2000 are one quantity. Refusing the comma would teach somebody that the
        validator is arbitrary, which is how a guard stops being read."""
        assert check_rewording("Handling 2,000 requests a minute at Acme.", FACT) == []

    def test_dropping_a_fact_asserts_nothing(self) -> None:
        report = validate_ops(
            parse_ops([_include("pp1"), {"kind": "drop", "fact_id": "pp2", "reason": "not asked"}]),
            FACTS,
        )

        assert report.passed
        assert report.cited == ["pp1"]


class TestOnlyConfirmedFacts:
    def test_a_proposed_fact_is_refused_by_name(self) -> None:
        """ "That fact is still a proposal" is actionable; "no such fact" sends somebody looking
        for a typo that is not there."""
        report = validate_ops(parse_ops([_include("pp3")]), FACTS)

        assert not report.passed
        assert any("proposal" in message for message in report.messages)

    def test_a_fact_that_does_not_exist_is_refused(self) -> None:
        report = validate_ops(parse_ops([_include("pp99")]), FACTS)

        assert not report.passed
        assert any("pp99" in message for message in report.messages)

    def test_the_same_fact_twice_is_refused(self) -> None:
        ops = parse_ops([_include("pp1"), _include("pp1", section="Skills")])
        report = validate_ops(ops, FACTS)

        assert not report.passed


class TestTheVocabulary:
    def test_a_heading_outside_the_list_is_refused(self) -> None:
        """A heading is the one line of a resume that is not a fact, so a proposal able to write
        one has a free line of prose outside the claim check."""
        with pytest.raises(OpError) as caught:
            parse_ops([_include("pp1", section="Why I am the best candidate")])

        assert "sections are" in str(caught.value)

    def test_a_heading_in_the_wrong_case_is_accepted_and_canonicalised(self) -> None:
        """A heading that varies by row reads as two sections."""
        assert parse_ops([_include("pp1", section="experience")])[0].section == "Experience"

    def test_an_unknown_operation_kind_is_refused(self) -> None:
        with pytest.raises(OpError):
            parse_ops([{"kind": "rewrite", "fact_id": "pp1"}])

    def test_a_rewording_longer_than_a_bullet_is_refused(self) -> None:
        with pytest.raises(OpError):
            parse_ops([_include("pp1", "Acme " * 200)])


class TestTheRenderedDocument:
    def test_every_line_is_a_header_a_heading_or_a_bullet(self) -> None:
        ops = parse_ops([_include("pp1"), _include("pp2", section="Skills")])
        text = render(ops, FACTS, header="Asha Rao\nasha@example.com")

        assert check_rendered(text, ops, FACTS, header="Asha Rao\nasha@example.com").passed
        assert "Experience" in text
        assert "Skills" in text

    def test_a_smuggled_line_is_refused_and_quoted_back(self) -> None:
        """The check that holds when a second producer appears: it reads the artefact, not the
        plan that was supposed to have produced it."""
        ops = parse_ops([_include("pp1")])
        text = render(ops, FACTS) + "- Five years of Kubernetes.\n"

        report = check_rendered(text, ops, FACTS)

        assert not report.passed
        assert any("Kubernetes" in message for message in report.messages)

    def test_sections_come_out_in_their_canonical_order(self) -> None:
        ops = parse_ops([_include("pp2", section="Skills"), _include("pp1", section="Summary")])
        text = render(ops, FACTS)

        assert text.index("Summary") < text.index("Skills")
        assert list(RESUME_SECTIONS)[:2] == ["Summary", "Experience"]
