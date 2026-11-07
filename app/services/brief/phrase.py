"""A model may phrase a brief line and may never add one (ADR-052).

The whole slice is in the shape of the contract rather than in the prompt. A model is handed
`{line_id: text}` and must return `{line_id: better text}`:

- **It cannot add a line**, because a line it invented would have no `line_id`, and an id it
  made up matches nothing. There is no field for a new claim, exactly as `propose_reply` has no
  `subject` and `ProposedEdits` has no field for a heading -- the mistake is not expressible
  rather than caught afterwards.
- **It cannot change what a line says**, because `domain.wording.invented` compares the
  rewording against the original and refuses a number or a named thing that was not already
  there. The same check that stops an invented skill on a resume (ADR-043), asked here of the
  same kind of edit.
- **It cannot drop a line**, because a line it does not mention keeps its own wording. Silence
  is not deletion; omitting the one awkward line would be the quietest way to mislead.
- **It never sees a source**, so it cannot move one. Provenance travels in `Line`, which this
  module reads and never writes.

A refusal costs that one line its rewording and nothing else: the deterministic sentence is
already correct, so falling back to it is not a degraded answer, it is the answer.
"""

from __future__ import annotations

from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field

from app.domain import wording
from app.services.brief.build import Brief, Line
from app.services.llm.router import LLMRouter

#: Lines offered to a model in one call. A brief is five short sections; a cap exists so a
#: pathological row count cannot turn one screen into the day's token budget.
MAX_LINES = 40

TASK = "phrase_brief"

SYSTEM = """You improve the wording of lines in a sales brief. You do not change what they say.

Rules, all of them absolute:
1. Return a rewording for each line id you were given, and for no other id.
2. Never introduce a name, a company, a product, a technology or a number that is not already
   in that line's own text. If you cannot improve a line without doing so, return it unchanged.
3. Never merge two lines, split one, or add a line. One id in, one id out.
4. Keep each line under 200 characters and keep it a plain statement of fact.

You are rewording claims that a person will repeat to a stranger. A better sentence that says
slightly more than the original is worse than the original."""


class PhrasedLine(BaseModel):
    """One line's new wording, named by the id it was given. There is no field for a new id."""

    model_config = ConfigDict(extra="forbid")

    line_id: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=1, max_length=400)


class Phrasing(BaseModel):
    """What a model is allowed to return: a wording per line id, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    lines: list[PhrasedLine] = Field(default_factory=list, max_length=MAX_LINES)


def apply_phrasing(brief: Brief, phrasing: Phrasing) -> tuple[Brief, list[str]]:
    """Fold a model's rewordings into a brief, refusing any that assert something new.

    Returns the brief and the refusals, in words. Pure: no model, no I/O, so the rule can be
    tested without one -- which is what makes it a rule rather than a prompt.
    """
    by_id = {line.line_id: line for line in brief.lines}
    offered: dict[str, str] = {}
    refusals: list[str] = []

    for item in phrasing.lines:
        original = by_id.get(item.line_id)
        if original is None:
            # Not an error worth failing the brief over, and worth saying out loud: a model
            # returning an id that was never offered is a model trying to add a claim.
            refusals.append(
                f"{item.line_id} is not a line in this brief, so its wording was discarded."
            )
            continue
        if item.line_id in offered:
            refusals.append(f"{item.line_id} was reworded twice; the second was discarded.")
            continue
        numbers, names = wording.invented(item.text, source=original.text)
        if numbers or names:
            said = ", ".join(numbers + names)
            refusals.append(
                f"{item.line_id} was reworded to say {said}, which its source does not. The "
                "original wording was kept."
            )
            continue
        offered[item.line_id] = item.text.strip()

    rewritten: list[Line] = [
        # `replace` keeps the source, the message id and the observed date untouched: a
        # rewording is a change of words and never a change of provenance.
        replace(line, text=offered[line.line_id]) if line.line_id in offered else line
        for line in brief.lines
    ]
    brief.lines = rewritten
    return brief, refusals


async def phrase(router: LLMRouter, brief: Brief) -> tuple[Brief, list[str]]:
    """Ask a model to tidy the wording, then refuse whatever it added.

    A model failure is not a brief failure. The deterministic text is already a correct brief,
    so an outage, an exhausted chain or an unparseable answer leaves the brief exactly as it
    was, with a note. This is the opposite trade from drafting, where a failed model means no
    email -- here the work is already done and the model is polish.
    """
    if not brief.lines:
        return brief, []

    listed = "\n".join(f"{line.line_id}: {line.text}" for line in brief.lines[:MAX_LINES])
    try:
        answer = await router.structured(
            Phrasing,
            task=TASK,
            system=SYSTEM,
            user=(
                "Reword each of these lines. Return every id you see here "
                f"and no others.\n\n{listed}"
            ),
        )
    except Exception as exc:
        # Any model failure leaves the brief as it was read; the text is already correct.
        return brief, [f"The wording was not improved ({type(exc).__name__}); lines are as read."]

    return apply_phrasing(brief, answer)
