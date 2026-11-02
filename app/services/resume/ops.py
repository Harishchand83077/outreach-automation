"""The vocabulary a tailored resume is built from (F20, ADR-043).

A pure module. An **edit operation** is the only thing that can put a line in a resume, and it
names the confirmed fact that line rests on -- so "which fact is this sentence?" is answerable by
construction rather than by asking a model afterwards.

Two kinds, and the pair is deliberately small:

- `include` places a fact in a section, optionally with `text`: a rewording **inside that one
  fact**, which `validate` polices.
- `drop` records a fact the proposal deliberately left out, with its reason. It produces no line.
  It exists because a resume is as much about what was cut as what was kept, and a person reading
  a proposal needs to see that a fact was considered rather than missed.

There is no `reorder` operation: the order of the `include` ops *is* the order of the document.
And there is no operation that writes a heading or a header line. A heading is the one line of a
resume that is not a fact, so a proposal able to invent one would have a free line of prose
outside the claim check -- the same reasoning that gives `propose_reply` no `subject` argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.models import RESUME_OP_KINDS, RESUME_SECTIONS

#: A reworded bullet longer than this is not a rewording. Resume bullets are one line.
MAX_TEXT_CHARS = 300

#: A proposal longer than this is not a resume. Enough for a long career, short enough that the
#: PDF job's memory is bounded by a number somebody chose rather than by whatever arrived.
MAX_OPS = 60


class OpError(Exception):
    """The operations are not readable as operations, with a sentence saying what was expected."""


@dataclass(frozen=True)
class EditOp:
    kind: str
    fact_id: str
    #: Required for `include`, ignored for `drop`.
    section: str = ""
    #: A rewording of the fact. `None` means use the fact's own statement verbatim, which is
    #: always the safest version and is what a proposal should choose when in doubt.
    text: str | None = None
    #: Why a fact was dropped. Shown to the person; asserted to nobody.
    reason: str | None = None

    @property
    def includes(self) -> bool:
        return self.kind == "include"

    def as_json(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": self.kind, "fact_id": self.fact_id}
        if self.section:
            row["section"] = self.section
        if self.text:
            row["text"] = self.text
        if self.reason:
            row["reason"] = self.reason
        return row


def _one(raw: Any, index: int) -> EditOp:
    if not isinstance(raw, dict):
        raise OpError(f"operation {index + 1} is not an object")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in RESUME_OP_KINDS:
        raise OpError(
            f"operation {index + 1} has kind {kind!r}; "
            f"this deployment has {' and '.join(RESUME_OP_KINDS)}"
        )
    fact_id = str(raw.get("fact_id") or "").strip()
    if not fact_id:
        raise OpError(f"operation {index + 1} names no fact")

    section = str(raw.get("section") or "").strip()
    if kind == "include":
        # Matched case-insensitively and then stored in the canonical spelling, so a proposal
        # writing "experience" is accepted while the document still has one spelling of each
        # heading. A heading that varies by row reads as two sections.
        canonical = {name.lower(): name for name in RESUME_SECTIONS}
        if section.lower() not in canonical:
            raise OpError(
                f"operation {index + 1} has section {section!r}; "
                f"the sections are {', '.join(RESUME_SECTIONS)}"
            )
        section = canonical[section.lower()]
    else:
        section = ""

    text = raw.get("text")
    cleaned: str | None = None
    if text is not None and str(text).strip():
        cleaned = " ".join(str(text).split())
        if len(cleaned) > MAX_TEXT_CHARS:
            raise OpError(
                f"operation {index + 1} rewords a fact into {len(cleaned)} characters; "
                f"a resume bullet is at most {MAX_TEXT_CHARS}"
            )

    reason = raw.get("reason")
    return EditOp(
        kind=kind,
        fact_id=fact_id,
        section=section,
        text=cleaned,
        reason=(" ".join(str(reason).split())[:300] or None) if reason else None,
    )


def parse_ops(raw: Any) -> list[EditOp]:
    """Read operations from JSON, refusing anything that is not one.

    Raises rather than dropping an unreadable operation: a resume silently missing the bullet
    somebody asked for is the kind of wrong answer this project has already paid for twice.
    """
    if not isinstance(raw, list):
        raise OpError("the edit operations must be a list")
    if len(raw) > MAX_OPS:
        raise OpError(f"a resume takes at most {MAX_OPS} operations; this has {len(raw)}")
    return [_one(item, index) for index, item in enumerate(raw)]


def as_json(ops: list[EditOp]) -> list[dict[str, Any]]:
    return [op.as_json() for op in ops]
