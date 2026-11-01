"""A fit score the candidate can audit (F6).

**The division of labour is the design.** A model extracts requirements from a posting as plain
text, and that output is untrusted data (hard rule 7) — a posting is text somebody else wrote.
Deterministic rules then compare each requirement with the candidate's **confirmed** facts.

So the score is never an opaque number: every match names the fact id it rests on, every gap is
listed, and nothing is asserted that does not point at something the candidate agreed to. It is
the same idea as the drafter's claim check, applied to a different question.

This module is pure. Extraction lives in `requirements.py` because it calls a model; the
comparison here never does, so a score is reproducible and a disagreement about one is settleable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.kinds import CandidateFact

#: Words too common to carry meaning in a requirement. Deliberately short: a stop list that
#: grows becomes a second, invisible scoring rule.
_NOISE = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "you",
        "your",
        "we",
        "our",
        "experience",
        "years",
        "year",
        "strong",
        "good",
        "excellent",
        "working",
        "knowledge",
        "ability",
        "plus",
        "etc",
    }
)

#: A requirement and a fact match when they share at least this many meaningful words. Two is
#: deliberately lenient: this proposes matches for a person to read, and a missed match is worse
#: than one they can dismiss at a glance.
MIN_OVERLAP = 2


def _terms(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9+#.]+", (text or "").lower())
    return {word for word in words if len(word) > 1 and word not in _NOISE}


@dataclass(frozen=True)
class Match:
    requirement: str
    fact_id: str
    #: The words that made this a match, so a person can see *why* rather than trust a score.
    shared: tuple[str, ...]


@dataclass
class FitScore:
    matched: list[Match] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.matched) + len(self.missing)

    @property
    def score(self) -> float:
        """Matched requirements as a fraction. Never shown without `matched` and `missing`."""
        return len(self.matched) / self.total if self.total else 0.0

    @property
    def fact_ids(self) -> list[str]:
        seen: list[str] = []
        for match in self.matched:
            if match.fact_id not in seen:
                seen.append(match.fact_id)
        return seen


def score_fit(requirements: list[str], facts: list[CandidateFact]) -> FitScore:
    """Compare a posting's requirements with the candidate's confirmed facts.

    `facts` must already be the confirmed ones: a proposed fact is not something the candidate
    has agreed to say, so scoring against it would produce a match they never claimed.
    """
    result = FitScore()
    prepared = [(fact, _terms(fact.text)) for fact in facts]

    for requirement in requirements:
        cleaned = requirement.strip()
        if not cleaned:
            continue
        wanted = _terms(cleaned)
        if not wanted:
            continue

        best: Match | None = None
        best_count = 0
        for fact, fact_terms in prepared:
            shared = wanted & fact_terms
            if len(shared) >= MIN_OVERLAP and len(shared) > best_count:
                best = Match(requirement=cleaned, fact_id=fact.id, shared=tuple(sorted(shared)))
                best_count = len(shared)

        if best is None:
            result.missing.append(cleaned)
        else:
            result.matched.append(best)

    return result
