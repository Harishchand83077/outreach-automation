"""What a reviewer changed about a draft, described deterministically (F16, ADR-059). No I/O.

**Describe, then compare, then summarise** -- the three steps the plan names, and the order
matters because each one is only safe given the one before it.

**The describing is arithmetic and not a model**, which is the decision this module is. A model
asked "what did the human change?" produces a fluent sentence that is sometimes about a change
that did not happen, and the whole point of this feature is to feed *back into a prompt* -- so a
hallucinated observation becomes a standing instruction, and the system learns something nobody
did. Everything below is computable from the two texts and can be checked by reading them.

What it can see is therefore narrower than what a person would notice, and that is the trade:
a short list of things that are definitely true beats a long list that is mostly true. The
limits are stated in ADR-059 rather than discovered later.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

#: An observation is only interesting if it happened more than once. One is a Tuesday.
MIN_OCCURRENCES = 3

#: A phrase has to be this long to be worth reporting as removed. Below it, "the" and "a" drown
#: everything that matters.
MIN_PHRASE_WORDS = 3

_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Observation:
    """One thing that is true of one edit.

    `kind` is a stable key so two edits that did the same thing group together; `detail` is the
    part a person reads. Both are computed, neither is written by a model.
    """

    kind: str
    detail: str


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split((text or "").strip()) if part.strip()]


def describe(
    *, before_subject: str, before_body: str, after_subject: str, after_body: str
) -> list[Observation]:
    """Everything that is definitely true about this one edit."""
    found: list[Observation] = []

    if before_subject.strip() != after_subject.strip():
        before_words = len(words(before_subject))
        after_words = len(words(after_subject))
        if after_words < before_words:
            found.append(
                Observation(
                    "subject_shortened",
                    f"Subject cut from {before_words} to {after_words} words.",
                )
            )
        elif after_words > before_words:
            found.append(
                Observation(
                    "subject_lengthened",
                    f"Subject grew from {before_words} to {after_words} words.",
                )
            )
        else:
            found.append(Observation("subject_reworded", "Subject reworded at the same length."))

    before_count = len(words(before_body))
    after_count = len(words(after_body))
    if before_count and abs(after_count - before_count) / before_count >= 0.15:
        if after_count < before_count:
            found.append(
                Observation(
                    "body_shortened",
                    f"Body cut from {before_count} to {after_count} words.",
                )
            )
        else:
            found.append(
                Observation(
                    "body_lengthened",
                    f"Body grew from {before_count} to {after_count} words.",
                )
            )

    # **Whole sentences that disappeared.** A removed sentence is the clearest signal a reviewer
    # gives: they read it and decided it should not be sent. A changed sentence is ambiguous --
    # it might be a typo -- so only removals are reported.
    kept = {_flatten(one) for one in sentences(after_body)}
    for sentence in sentences(before_body):
        if _flatten(sentence) in kept:
            continue
        if len(words(sentence)) < MIN_PHRASE_WORDS:
            continue
        found.append(Observation(f"removed:{_flatten(sentence)}", f"Removed: “{sentence.strip()}”"))

    return found


def _flatten(text: str) -> str:
    return " ".join(words(text))


@dataclass(frozen=True)
class Pattern:
    """Something several reviewers did several times."""

    kind: str
    occurrences: int
    detail: str
    #: The drafts it was seen on, so a person can open them and check. **A pattern without its
    #: evidence is an assertion**, and this one is about to become an instruction to a model.
    draft_ids: tuple[str, ...]

    @property
    def sentence(self) -> str:
        return f"{self.detail} ({self.occurrences} times)"


def recurring(
    per_draft: list[tuple[str, list[Observation]]], *, minimum: int = MIN_OCCURRENCES
) -> list[Pattern]:
    """Observations seen on at least `minimum` separate drafts, commonest first.

    Counted **per draft, not per occurrence**: one reviewer removing the same sentence from one
    email three times during an edit is one opinion, and counting it as three would let a single
    session invent a rule for everybody.
    """
    counts: Counter[str] = Counter()
    details: dict[str, str] = {}
    evidence: dict[str, list[str]] = {}

    for draft_id, observations in per_draft:
        for kind in {one.kind for one in observations}:
            counts[kind] += 1
            evidence.setdefault(kind, []).append(draft_id)
            details.setdefault(kind, next(one.detail for one in observations if one.kind == kind))

    return [
        Pattern(
            kind=kind,
            occurrences=count,
            detail=details[kind],
            draft_ids=tuple(evidence[kind][:20]),
        )
        for kind, count in counts.most_common()
        if count >= minimum
    ]


def as_instruction(pattern: Pattern) -> str:
    """The line a pattern would add to a drafting prompt, if a person approved it.

    **It is always about how to write, never about what is true.** A removed sentence becomes
    "do not write this", not "this is false" -- the drafter may still only assert what the
    evidence supports, and this changes nothing about that. The same shape as an A/B arm's
    instruction (ADR-053), and it goes through the same claim check for the same reason.
    """
    if pattern.kind.startswith("removed:"):
        return f"Reviewers keep deleting this sentence, so do not write it: {pattern.detail}"
    return {
        "subject_shortened": "Reviewers keep shortening the subject. Write a shorter one.",
        "subject_lengthened": "Reviewers keep lengthening the subject. Be more specific in it.",
        "subject_reworded": "Reviewers keep rewording the subject. Try a plainer one.",
        "body_shortened": "Reviewers keep cutting the body down. Write fewer words.",
        "body_lengthened": "Reviewers keep adding to the body. It is arriving too thin.",
    }.get(pattern.kind, pattern.detail)
