"""What a reviewer changed, described without a model (F16, ADR-059).

`domain/edits.py` is arithmetic over two strings, so every rule here is a test with no database
and no fake model. That is the point of the module: the output of this feature goes into a
drafting prompt, so an observation that is merely plausible would become a standing instruction
nobody wrote.
"""

from __future__ import annotations

from app.domain import edits


def _describe(before: str, after: str, *, subject: str = "Same", after_subject: str | None = None):
    return edits.describe(
        before_subject=subject,
        before_body=before,
        after_subject=after_subject if after_subject is not None else subject,
        after_body=after,
    )


def _kinds(observations) -> set[str]:
    return {one.kind for one in observations}


class TestWhatItSees:
    def test_a_removed_sentence_is_reported(self) -> None:
        """The clearest signal a reviewer gives: they read it and decided it should not go."""
        found = _describe(
            "I hope this finds you well. We build backends for Indian startups.",
            "We build backends for Indian startups.",
        )
        assert any(one.kind.startswith("removed:") for one in found)
        assert any("hope this finds you well" in one.detail for one in found)

    def test_a_shorter_subject_is_reported(self) -> None:
        found = _describe(
            "Body.",
            "Body.",
            subject="A quick question about your hiring plans",
            after_subject="Your hiring",
        )
        assert "subject_shortened" in _kinds(found)

    def test_a_substantially_shorter_body_is_reported(self) -> None:
        found = _describe(" ".join(["word"] * 100), " ".join(["word"] * 60))
        assert "body_shortened" in _kinds(found)

    def test_a_trivial_length_change_is_not_reported(self) -> None:
        """A two-word trim is a typo fix, not an opinion about length. Reporting it would bury
        the observations that matter under ones nobody meant."""
        found = _describe(" ".join(["word"] * 100), " ".join(["word"] * 98))
        assert "body_shortened" not in _kinds(found)

    def test_an_identical_draft_produces_nothing(self) -> None:
        assert _describe("Same body.", "Same body.") == []

    def test_a_very_short_removal_is_ignored(self) -> None:
        """Below three words, "the" and "a" drown everything that matters."""
        found = _describe("Hello. We build backends.", "We build backends.")
        assert not any(one.kind.startswith("removed:") for one in found)

    def test_a_reordered_sentence_is_not_a_removal(self) -> None:
        """It is still in the email, so nobody decided against it."""
        found = _describe(
            "We build backends for startups. I saw your hiring post.",
            "I saw your hiring post. We build backends for startups.",
        )
        assert not any(one.kind.startswith("removed:") for one in found)


class TestRecurring:
    def _pair(self, draft_id: str, *kinds: str):
        return (draft_id, [edits.Observation(kind, f"detail for {kind}") for kind in kinds])

    def test_three_drafts_make_a_pattern(self) -> None:
        found = edits.recurring(
            [
                self._pair("a", "body_shortened"),
                self._pair("b", "body_shortened"),
                self._pair("c", "body_shortened"),
            ]
        )
        assert [one.kind for one in found] == ["body_shortened"]
        assert found[0].occurrences == 3

    def test_two_is_not_yet_a_pattern(self) -> None:
        """Once is a Tuesday, and twice is two Tuesdays."""
        found = edits.recurring(
            [self._pair("a", "body_shortened"), self._pair("b", "body_shortened")]
        )
        assert found == []

    def test_one_draft_cannot_vote_twice(self) -> None:
        """**Counted per draft, not per occurrence.** One reviewer removing the same sentence
        three times while editing one email is one opinion, and counting it as three would let
        a single session invent a rule for everybody."""
        observations = [edits.Observation("body_shortened", "x")] * 5
        found = edits.recurring([("a", observations)], minimum=3)
        assert found == []

    def test_a_pattern_carries_the_drafts_it_was_counted_from(self) -> None:
        """A pattern without its evidence is an assertion, and this one is about to become an
        instruction to a model."""
        found = edits.recurring(
            [
                self._pair("a", "body_shortened"),
                self._pair("b", "body_shortened"),
                self._pair("c", "body_shortened"),
            ]
        )
        assert set(found[0].draft_ids) == {"a", "b", "c"}

    def test_the_commonest_comes_first(self) -> None:
        pairs = [
            self._pair("a", "body_shortened", "subject_shortened"),
            self._pair("b", "body_shortened", "subject_shortened"),
            self._pair("c", "body_shortened", "subject_shortened"),
            self._pair("d", "body_shortened"),
        ]
        found = edits.recurring(pairs)
        assert found[0].kind == "body_shortened"


class TestTheInstruction:
    def test_it_is_about_how_to_write_and_never_about_what_is_true(self) -> None:
        """**The rule this feature lives or dies by.** A lesson may say "write fewer words"; it
        may not say anything that would let a draft assert something new, because every draft
        still has to resolve every claim to evidence (hard rule 6)."""
        for kind in (
            "body_shortened",
            "body_lengthened",
            "subject_shortened",
            "subject_lengthened",
            "subject_reworded",
        ):
            line = edits.as_instruction(edits.Pattern(kind, 3, "detail", ("a",)))
            assert line
            for forbidden in ("true", "fact", "claim", "evidence", "statistic"):
                assert forbidden not in line.lower()

    def test_a_removed_sentence_becomes_do_not_write_this(self) -> None:
        pattern = edits.Pattern("removed:i hope this finds you well", 4, "Removed: “x”", ("a",))
        assert "do not write it" in edits.as_instruction(pattern)
