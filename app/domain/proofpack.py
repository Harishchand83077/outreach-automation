"""S19.5 — proof pack and clarity check (ADR-071).

A proof pack is a selection of confirmed facts that map to a posting's top
requirements, chosen by rules — no model, no generated prose, nothing to
hallucinate. A clarity check suggests rewrites on specific detectable issues.

Both are pure (no I/O, no Settings, injected clock). The proof pack reads from
whatever the caller passes; the clarity check runs over a plain string.

No cover-letter generator. No automatic rewrite.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Proof pack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactMatch:
    """One confirmed fact that maps to one requirement."""

    requirement: str
    fact_id: str
    statement: str
    shared: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProofPack:
    """The result of `build()`: facts ordered by their requirement's weight."""

    matches: list[FactMatch]
    unmatched_requirements: list[str]

    @property
    def coverage(self) -> float:
        """Fraction of requirements that have at least one confirmed fact."""
        total = len(self.matches) + len(self.unmatched_requirements)
        if total == 0:
            return 0.0
        return len(self.matches) / total


def build(
    *,
    requirements: list[str],
    matched: list[dict[str, object]],
    confirmed_facts: dict[str, str],
) -> ProofPack:
    """Select confirmed facts for each requirement, in requirement order.

    Args:
        requirements: The posting's extracted requirements (from fit_scores.requirements).
        matched: The fit_scores.matched list — each entry has "requirement",
                 "fact_id", and "shared" (words that linked them).
        confirmed_facts: Mapping of fact_id → statement for all confirmed facts
                         in this workspace (source="confirmed" or confirmed_at set).

    Returns:
        A ProofPack with the confirmed-fact matches and any unmatched requirements.
    """
    seen_requirements: set[str] = set()
    matches: list[FactMatch] = []

    for entry in matched:
        req = str(entry.get("requirement", ""))
        fact_id = str(entry.get("fact_id", ""))
        raw_shared = entry.get("shared")
        shared = [str(w) for w in raw_shared] if isinstance(raw_shared, list) else []

        if req in seen_requirements:
            continue
        statement = confirmed_facts.get(fact_id)
        if statement is None:
            continue

        matches.append(
            FactMatch(requirement=req, fact_id=fact_id, statement=statement, shared=shared)
        )
        seen_requirements.add(req)

    unmatched = [r for r in requirements if r not in seen_requirements]
    return ProofPack(matches=matches, unmatched_requirements=unmatched)


# ---------------------------------------------------------------------------
# Clarity check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaritySuggestion:
    """A suggested edit with the reason behind it."""

    span_start: int
    span_end: int
    original: str
    suggestion: str
    reason: str


_WEASELS = re.compile(
    r"\b(very|really|quite|rather|somewhat|fairly|just|literally|basically|actually|honestly)\b",
    re.IGNORECASE,
)
_PASSIVE = re.compile(r"\b(was|were|is|are|been|be)\s+\w+ed\b", re.IGNORECASE)
_LONG_SENTENCE_CHARS = 180
_REPETITION_MIN_WORD_LEN = 5


def clarity_check(text: str) -> list[ClaritySuggestion]:
    """Return a list of clarity suggestions for a plain-text document.

    Checks:
      - Weasel words (suggest removing)
      - Long sentences (suggest splitting)
      - Repeated nearby words (suggest varying)

    Nothing is rewritten automatically. The caller presents these to the person,
    who accepts or ignores each one.
    """
    suggestions: list[ClaritySuggestion] = []
    _check_weasels(text, suggestions)
    _check_long_sentences(text, suggestions)
    _check_repetition(text, suggestions)
    return suggestions


def _check_weasels(text: str, out: list[ClaritySuggestion]) -> None:
    for m in _WEASELS.finditer(text):
        out.append(
            ClaritySuggestion(
                span_start=m.start(),
                span_end=m.end(),
                original=m.group(),
                suggestion="(remove)",
                reason=f"{m.group()!r} is a weasel word that weakens the claim. Remove it.",
            )
        )


def _check_long_sentences(text: str, out: list[ClaritySuggestion]) -> None:
    for m in re.finditer(r"[^.!?]+[.!?]", text):
        sentence = m.group()
        if len(sentence) > _LONG_SENTENCE_CHARS:
            out.append(
                ClaritySuggestion(
                    span_start=m.start(),
                    span_end=m.end(),
                    original=sentence.strip(),
                    suggestion="(split into two sentences)",
                    reason=f"Sentence is {len(sentence)} characters. Consider splitting it.",
                )
            )


def _check_repetition(text: str, out: list[ClaritySuggestion]) -> None:
    words = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\b\w+\b", text.lower())]
    seen: dict[str, int] = {}
    for word, start, end in words:
        if len(word) < _REPETITION_MIN_WORD_LEN:
            continue
        word_norm = _normalise_word(word)
        prev_start = seen.get(word_norm)
        if prev_start is not None and (start - prev_start) < 200:
            original = text[start:end]
            out.append(
                ClaritySuggestion(
                    span_start=start,
                    span_end=end,
                    original=original,
                    suggestion="(vary the word)",
                    reason=f"{original!r} appears again within 200 characters. Consider a synonym.",
                )
            )
        seen[word_norm] = start


def _normalise_word(word: str) -> str:
    return unicodedata.normalize("NFKD", word).encode("ascii", "ignore").decode()
