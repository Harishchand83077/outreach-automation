"""A model may phrase a brief line and may never add one (S15.3, ADR-052).

`apply_phrasing` is pure, so the rule is testable without a model -- which is what makes it a
rule rather than a prompt. Every test here is the behaviour of a model that is trying to be
helpful in the way models are: tidying a sentence and slipping in the obvious extra fact.
"""

from __future__ import annotations

import uuid

import pytest

from app.domain import wording
from app.services.brief.build import SECTIONS, Brief, Line, NoProvenance, render
from app.services.brief.phrase import PhrasedLine, Phrasing, apply_phrasing


def _brief(*texts: str) -> Brief:
    return Brief(
        lead_id=uuid.uuid4(),
        header={"note": "From your own import."},
        lines=[
            Line(
                line_id=f"g{index}",
                section="what_they_are_doing",
                text=text,
                source_url=f"https://acme.example/{index}",
            )
            for index, text in enumerate(texts)
        ],
    )


def _said(**pairs: str) -> Phrasing:
    return Phrasing(lines=[PhrasedLine(line_id=key, text=value) for key, value in pairs.items()])


class TestALineNeedsASource:
    def test_a_line_with_neither_a_url_nor_a_message_is_refused(self) -> None:
        with pytest.raises(NoProvenance):
            Line(line_id="x", section="what_we_found", text="They raised a Series A")

    def test_a_message_alone_is_enough(self) -> None:
        line = Line(
            line_id="m0",
            section="the_conversation",
            text="They wrote: happy to talk",
            message_id=uuid.uuid4(),
        )
        assert line.source_url is None

    def test_a_section_outside_the_five_is_refused(self) -> None:
        """A model cannot invent a section either, because there is nowhere to put one."""
        with pytest.raises(NoProvenance):
            Line(
                line_id="x",
                section="why_they_will_buy",
                text="They need us",
                source_url="https://acme.example/",
            )

    def test_there_are_exactly_five(self) -> None:
        assert len(SECTIONS) == 5


class TestAModelMayPhrase:
    def test_a_rewording_in_the_lines_own_words_is_taken(self) -> None:
        brief = _brief("Acme is hiring for Backend Engineer")
        out, refusals = apply_phrasing(brief, _said(g0="Acme is hiring a Backend Engineer"))
        assert refusals == []
        assert out.lines[0].text == "Acme is hiring a Backend Engineer"

    def test_the_source_is_untouched_by_a_rewording(self) -> None:
        """A rewording is a change of words and never a change of provenance."""
        brief = _brief("Acme is hiring for Backend Engineer")
        before = brief.lines[0].source_url
        out, _ = apply_phrasing(brief, _said(g0="Acme is hiring a Backend Engineer"))
        assert out.lines[0].source_url == before

    def test_a_line_the_model_ignores_keeps_its_own_wording(self) -> None:
        """Silence is not deletion: omitting the awkward line would be the quietest way to
        mislead, so a line nobody mentioned stands exactly as it was read."""
        brief = _brief("Acme is hiring for Backend Engineer", "Acme is hiring for SRE")
        out, refusals = apply_phrasing(brief, _said(g0="Acme wants a backend engineer"))
        assert refusals == []
        assert out.lines[1].text == "Acme is hiring for SRE"
        assert len(out.lines) == 2


class TestAndMayNeverAdd:
    def test_an_invented_number_is_refused_and_the_original_stands(self) -> None:
        brief = _brief("Acme is hiring for Backend Engineer")
        out, refusals = apply_phrasing(
            brief, _said(g0="Acme is hiring for 3 Backend Engineer roles")
        )
        assert out.lines[0].text == "Acme is hiring for Backend Engineer"
        assert len(refusals) == 1
        assert "3" in refusals[0]

    def test_an_invented_named_thing_is_refused(self) -> None:
        """The failure this is actually for: a model that knows what Acme probably uses."""
        brief = _brief("Acme is hiring for Backend Engineer")
        out, refusals = apply_phrasing(
            brief, _said(g0="Acme is hiring a Backend Engineer to work on Kubernetes")
        )
        assert out.lines[0].text == "Acme is hiring for Backend Engineer"
        assert "kubernetes" in refusals[0].lower()

    def test_a_line_id_that_was_never_offered_is_discarded(self) -> None:
        """The structural half: a claim a model invented has no id, so there is nothing to
        attach it to. A made-up id is the attempt, and it is reported."""
        brief = _brief("Acme is hiring for Backend Engineer")
        out, refusals = apply_phrasing(
            brief, _said(g0="Acme is hiring a Backend Engineer", z9="Acme raised a Series B")
        )
        assert [line.text for line in out.lines] == ["Acme is hiring a Backend Engineer"]
        assert any("z9" in refusal for refusal in refusals)

    def test_the_same_id_twice_keeps_the_first(self) -> None:
        brief = _brief("Acme is hiring for Backend Engineer")
        out, refusals = apply_phrasing(
            brief,
            Phrasing(
                lines=[
                    PhrasedLine(line_id="g0", text="Acme is hiring a Backend Engineer"),
                    PhrasedLine(line_id="g0", text="Acme is hiring urgently"),
                ]
            ),
        )
        assert out.lines[0].text == "Acme is hiring a Backend Engineer"
        assert len(refusals) == 1

    def test_a_number_already_in_the_line_may_be_kept(self) -> None:
        """The check refuses what is *new*, not every number: otherwise a line about three
        open roles could not be reworded at all."""
        brief = _brief("Acme is hiring for 3 Backend Engineer roles")
        out, refusals = apply_phrasing(brief, _said(g0="Acme has 3 Backend Engineer roles open"))
        assert refusals == []
        assert "3" in out.lines[0].text


class TestTheSharedCheck:
    """`domain.wording` was extracted from the resume validator because a second caller
    appeared. These assert the shared function answers both callers' question."""

    def test_a_quantity_written_two_ways_is_one_number(self) -> None:
        numbers, names = wording.invented("saved 2000 hours", source="saved 2,000 hours")
        assert numbers == [] and names == []

    def test_position_in_the_sentence_is_not_used(self) -> None:
        """Exempting the first word would leave unchecked the one line somebody would write:
        the one opening on a technology that is not in the source."""
        _, names = wording.invented("Kubernetes is their stack", source="they are hiring")
        assert "kubernetes" in names

    def test_the_resume_validator_asks_the_same_function(self) -> None:
        from app.services.resume import validate

        assert validate._numbers("2,000") == wording.numbers("2,000")
        assert validate._named("AWS") == wording.named("AWS")


class TestRendering:
    def test_every_rendered_line_carries_its_source(self) -> None:
        """The property that makes the rule checkable from outside: a sentence that arrives
        without a source did not come from here.

        Asserted per line against that line's own URL rather than by scanning the text for
        brackets -- "there is a bracket somewhere" is the kind of proxy this project has now
        caught seven times.
        """
        brief = _brief("Acme is hiring for Backend Engineer", "Acme is hiring for SRE")
        brief.lines.append(
            Line(
                line_id="m0",
                section="the_conversation",
                text="They wrote: happy to talk",
                message_id=uuid.uuid4(),
            )
        )
        text = render(brief)
        for line in brief.lines:
            where = line.source_url or f"message {line.message_id}"
            assert f"- {line.text}  [{where}]" in text

    def test_all_five_sections_are_named_even_when_empty(self) -> None:
        """A missing section reads as a brief that forgot; an empty one reads as a company we
        do not know that about yet, which is the truth."""
        text = render(_brief())
        assert text.count("(nothing citable yet)") == 5
