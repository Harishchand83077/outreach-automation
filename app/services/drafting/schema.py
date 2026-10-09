"""The shape of a draft, and what a claim has to carry to be allowed.

`ARCHITECTURE_TARGET.md` §13. The model returns `{subject, body, claims[]}` as structured
output, which fixes two prototype defects at once:

- **The subject is a field.** The prototype took line 1 of the response if it began with
  `Subject:`, so the moment the model added "Here's your email:" the subject fell back to a
  generic one and the preamble went into the body (issue B13).
- **Every concrete claim must name its evidence.** `evidence_ref` points at a research hook or a
  persona proof point, and the validator resolves it. Unresolvable means the draft is rejected,
  which is what stops the model asserting traction we never had (issue A3).

The body is plain text. Not "plain text preferred" — there is no HTML path at all, so the
prototype's unescaped `replace("\\n", "<br>")` has nothing to escape (issue B14), and
deliverability improves as a side effect.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: `pp3` for a persona proof point, `hook:12345678` for a verified research hook.
EVIDENCE_REF = re.compile(r"^(pp\d+|hook:[a-z0-9]+)$")


class Claim(BaseModel):
    """One assertion in the draft, with the evidence it rests on."""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=500)
    #: A persona proof-point id, or a verified research hook id. Nothing else resolves.
    evidence_ref: str = Field(min_length=1, max_length=60)

    @field_validator("evidence_ref")
    @classmethod
    def _recognisable_shape(cls, v: str) -> str:
        """Reject a made-up citation format early.

        A model that invents `source_1` instead of citing `pp2` is guessing, and the specific
        failure is worth reporting as "the citation format is wrong" rather than the vaguer
        "that evidence does not exist".
        """
        cleaned = v.strip()
        if not EVIDENCE_REF.match(cleaned):
            raise ValueError(
                f"evidence_ref {cleaned!r} must be a persona proof point (pp1, pp2, ...) "
                "or a research hook id (hook:...)"
            )
        return cleaned


class DraftOutput(BaseModel):
    """What the drafting model returns, before validation."""

    model_config = ConfigDict(extra="ignore")

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    claims: list[Claim] = Field(default_factory=list, max_length=10)

    @field_validator("subject")
    @classmethod
    def _subject_is_one_clean_line(cls, v: str) -> str:
        """Strip any `Subject:` prefix the model adds out of habit, and refuse newlines.

        A newline in a subject is header injection: everything after it would be interpreted as
        a new header by the receiving server.
        """
        cleaned = v.strip()
        if cleaned.lower().startswith("subject:"):
            cleaned = cleaned[len("subject:") :].strip()
        if "\n" in cleaned or "\r" in cleaned:
            raise ValueError("subject must be a single line")
        return cleaned

    @field_validator("body")
    @classmethod
    def _body_has_no_control_characters(cls, v: str) -> str:
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in v):
            raise ValueError("body must not contain control characters")
        return v.strip()

    @model_validator(mode="after")
    def _claims_cite_distinct_evidence(self) -> Self:
        """Two claims may not lean on the same evidence.

        Not pedantry: it is how one hook gets stretched into three specifics that sound
        independently verified when only one thing was ever checked.
        """
        refs = [claim.evidence_ref for claim in self.claims]
        if len(refs) != len(set(refs)):
            raise ValueError("each claim must cite a different piece of evidence")
        return self

    @property
    def subject_word_count(self) -> int:
        return len(self.subject.split())

    @property
    def body_word_count(self) -> int:
        return len(self.body.split())
