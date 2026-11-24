"""S19.5 — proof pack and clarity check unit tests."""

from __future__ import annotations

from app.domain.proofpack import ClaritySuggestion, build, clarity_check


class TestProofPack:
    def _facts(self) -> dict[str, str]:
        return {
            "f1": "Led a team of 8 engineers to deliver a data platform on time",
            "f2": "Proficient in Python, SQL and dbt",
            "f3": "Built CI/CD pipelines with GitHub Actions",
        }

    def _matched(self) -> list[dict[str, object]]:
        return [
            {"requirement": "Team leadership", "fact_id": "f1", "shared": ["team", "led"]},
            {"requirement": "Python experience", "fact_id": "f2", "shared": ["python"]},
        ]

    def test_matched_facts_appear_in_pack(self) -> None:
        pack = build(
            requirements=["Team leadership", "Python experience", "Data engineering"],
            matched=self._matched(),
            confirmed_facts=self._facts(),
        )
        assert len(pack.matches) == 2
        ids = {m.fact_id for m in pack.matches}
        assert ids == {"f1", "f2"}

    def test_unconfirmed_fact_is_excluded(self) -> None:
        # f3 is in matched but NOT in confirmed_facts
        matched = [{"requirement": "CI/CD", "fact_id": "f3", "shared": []}]
        pack = build(
            requirements=["CI/CD"],
            matched=matched,
            confirmed_facts={},
        )
        assert pack.matches == []
        assert "CI/CD" in pack.unmatched_requirements

    def test_unmatched_requirements_are_listed(self) -> None:
        pack = build(
            requirements=["Team leadership", "Python experience", "Data engineering"],
            matched=self._matched(),
            confirmed_facts=self._facts(),
        )
        assert pack.unmatched_requirements == ["Data engineering"]

    def test_coverage_calculation(self) -> None:
        pack = build(
            requirements=["Team leadership", "Python experience", "Data engineering"],
            matched=self._matched(),
            confirmed_facts=self._facts(),
        )
        assert abs(pack.coverage - 2 / 3) < 0.01

    def test_duplicate_requirement_not_double_counted(self) -> None:
        matched = [
            {"requirement": "Python", "fact_id": "f1", "shared": []},
            {"requirement": "Python", "fact_id": "f2", "shared": []},
        ]
        pack = build(
            requirements=["Python"],
            matched=matched,
            confirmed_facts={"f1": "knows Python", "f2": "also Python"},
        )
        assert len(pack.matches) == 1

    def test_no_io(self) -> None:
        """Module must not import httpx, asyncpg, or anything that opens a socket."""
        import importlib.util

        spec = importlib.util.find_spec("app.domain.proofpack")
        assert spec is not None
        source = open(spec.origin).read()
        for bad in ("httpx", "asyncpg", "aiosmtpd", "smtplib", "socket"):
            assert bad not in source, f"proofpack imports {bad!r}"


class TestClarityCheck:
    def test_weasel_words_flagged(self) -> None:
        suggestions = clarity_check("I am very good at leading teams.")
        reasons = [s.reason for s in suggestions]
        assert any("weasel" in r for r in reasons)

    def test_long_sentence_flagged(self) -> None:
        long = (
            "I have led many different teams across a variety of organizations in m"
            "ultiple industries over the past decade, and I have delivered a lot of "
            "important projects on time and under budget for clients. "
        )
        suggestions = clarity_check(long)
        reasons = [s.reason for s in suggestions]
        assert any("Sentence is" in r for r in reasons)

    def test_repetition_flagged(self) -> None:
        text = "I managed the project carefully. I also managed another project."
        suggestions = clarity_check(text)
        reasons = [s.reason for s in suggestions]
        assert any("appear" in r for r in reasons)

    def test_clean_text_no_suggestions(self) -> None:
        text = "Led eight engineers. Delivered on time. Built three pipelines."
        suggestions = clarity_check(text)
        assert suggestions == []

    def test_suggestion_is_dataclass(self) -> None:
        s = ClaritySuggestion(1, 5, "very", "(remove)", "weasel")
        assert s.span_start == 1
        assert s.original == "very"
