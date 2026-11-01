"""The fit score (F6, slice S12.5).

A model extracts requirements; **rules** compare them with confirmed facts. So the score is
reproducible, auditable, and never a number on its own: every match names the fact it rests on
and the words that made it one.
"""

from __future__ import annotations

from app.domain.kinds import CandidateFact
from app.services.candidate.fit import MIN_OVERLAP, score_fit

FACTS = [
    CandidateFact(id="pp1", text="Built a FastAPI service handling 2000 requests a second"),
    CandidateFact(id="pp2", text="Three years of Postgres, including query tuning"),
    CandidateFact(id="pp3", text="Led a team of four engineers"),
]


def test_a_match_names_the_fact_it_rests_on():
    result = score_fit(["Experience with FastAPI service design"], FACTS)

    assert [m.fact_id for m in result.matched] == ["pp1"]
    assert result.missing == []


def test_a_match_shows_why_rather_than_only_that():
    """A person reading this has to be able to dismiss a bad match at a glance."""
    result = score_fit(["Strong Postgres query tuning"], FACTS)

    assert "postgres" in result.matched[0].shared
    assert "tuning" in result.matched[0].shared


def test_an_unmet_requirement_is_listed_rather_than_scored_away():
    result = score_fit(["Kubernetes operator development"], FACTS)

    assert result.matched == []
    assert result.missing == ["Kubernetes operator development"]


def test_the_score_is_never_the_only_output():
    result = score_fit(["FastAPI service", "Kubernetes"], FACTS)

    assert result.score == 0.5
    assert result.total == 2
    assert result.matched and result.missing


def test_one_shared_word_is_not_a_match():
    """`MIN_OVERLAP` exists so a single common word does not manufacture a match."""
    assert MIN_OVERLAP >= 2
    result = score_fit(["Team player"], FACTS)

    assert result.matched == []


def test_noise_words_cannot_carry_a_match():
    """'Years of experience with' is in every posting and means nothing on its own."""
    result = score_fit(["Years of experience working with strong knowledge"], FACTS)

    assert result.matched == []


def test_the_best_fact_wins_rather_than_the_first():
    result = score_fit(["Postgres query tuning for a FastAPI service"], FACTS)

    assert len(result.matched) == 1


def test_no_facts_means_everything_is_a_gap():
    """A candidate who has confirmed nothing matches nothing -- not a high score by default."""
    result = score_fit(["FastAPI", "Postgres"], [])

    assert result.matched == []
    assert len(result.missing) == 2
    assert result.score == 0.0


def test_an_empty_posting_scores_nothing_rather_than_dividing_by_zero():
    result = score_fit([], FACTS)

    assert result.total == 0
    assert result.score == 0.0


def test_blank_requirements_are_ignored():
    result = score_fit(["", "   ", "FastAPI service"], FACTS)

    assert result.total == 1


def test_fact_ids_are_reported_once_each():
    result = score_fit(["FastAPI service", "FastAPI requests a second"], FACTS)

    assert result.fact_ids == ["pp1"]
