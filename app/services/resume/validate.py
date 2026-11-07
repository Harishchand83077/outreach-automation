"""What a tailored resume may say (F20, ADR-043).

**The rule, stated once:** every line of the document is one of three things -- the person's own
header, a heading from a fixed list, or a bullet that rests on a **confirmed** `profile_facts`
entry. Nothing else can get in, and a rewording of a fact may not introduce a number, a skill, a
product or an employer the fact does not contain.

Hard rule 6 forbids inventing facts about the sender. A resume is the sharpest case of it: the
cost of a fabricated number is not a weak document, it is somebody being asked about it in an
interview and having to say they did not do that.

**How a new claim is recognised, and what this honestly does not catch.** A reworded bullet is
checked for two things against the fact it rewords:

1. **Numbers.** Every number in the rewording must appear in the fact, compared after stripping
   thousands separators and a trailing `%` or `+`. So "42%" from a fact that says nothing of the
   kind is refused, and "2,000" against "2000" is not.
2. **Named things.** Every token that is capitalised, all-capitals, or carries an internal digit
   or `+`, `#` or `.` -- `AWS`, `Python`, `C++`, `k8s`, `Node.js`, `Acme` -- must appear in the
   fact. Lower-case connective rewording is free.

So a rewording may use that fact's own vocabulary plus lower-case words, which is a real
constraint: it must begin with a word the fact already uses. That is the conservative direction
on purpose -- a refused rewording costs one edit, or the person includes the fact verbatim, while
an invented skill costs their credibility. What this **cannot** catch is a lower-case
exaggeration ("led a team" from a fact about writing code), which no deterministic check could.
That is why a version is shown to the person before anything is attached, and why `created_by`
records whether a model produced it.

**Validated twice, on purpose.** `validate_ops` checks the plan and `check_rendered` checks the
finished document, line by line, against the ops it claims to come from. The second looks
redundant because `render` builds the first -- and it is the one that holds when a second
producer appears, because `store.create_version` runs it on the text it is about to write
whatever built that text. This project's dominant defect is a correct producer and a correct
consumer with nothing checking that they agreed; here the check is the artefact itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.db.models import RESUME_SECTIONS
from app.domain import wording
from app.services.resume.ops import EditOp

#: How a bullet is written into the document. `check_rendered` matches on it exactly, so it is
#: defined once here rather than in both the renderer and the checker.
BULLET = "- "


@dataclass(frozen=True)
class KnownFact:
    """A `profile_facts` row, reduced to what the rules need.

    `confirmed` is carried rather than filtered out by the caller so a refusal can say *why* a
    fact was refused: "that fact is still a proposal" is actionable and "no such fact" is not.
    """

    fact_id: str
    statement: str
    confirmed: bool


@dataclass(frozen=True)
class Problem:
    message: str
    fact_id: str | None = None
    op_index: int | None = None


@dataclass
class Report:
    problems: list[Problem] = field(default_factory=list)
    #: The fact ids the document cites, in the order it cites them.
    cited: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems

    @property
    def messages(self) -> list[str]:
        return [problem.message for problem in self.problems]


def _numbers(text: str) -> set[str]:
    """Delegates to `domain.wording`: S15.3's brief asks the same question of the same kind of
    edit, and two copies of this rule would be the signature defect in the worst place."""
    return wording.numbers(text)


def _tokens(text: str) -> set[str]:
    return wording.tokens(text)


def _named(text: str) -> set[str]:
    return wording.named(text)


def check_rewording(text: str, fact: KnownFact) -> list[str]:
    """What a rewording asserts that its fact does not. Empty means it asserts nothing new."""
    problems: list[str] = []
    invented_numbers, invented_names = wording.invented(text, source=fact.statement)

    if invented_numbers:
        problems.append(
            f"{fact.fact_id} is reworded with {', '.join(invented_numbers)}, which is not in "
            "the fact. A resume may not state a number you have not confirmed."
        )
    if invented_names:
        problems.append(
            f"{fact.fact_id} is reworded with {', '.join(invented_names)}, which is not in the "
            "fact. Reword using the fact's own words, add a fact that says this, or include the "
            "fact as it stands."
        )
    return problems


def validate_ops(ops: list[EditOp], facts: dict[str, KnownFact]) -> Report:
    """Check a proposal before anything is rendered or stored."""
    report = Report()
    seen: set[str] = set()

    for index, op in enumerate(ops):
        fact = facts.get(op.fact_id)
        if fact is None:
            report.problems.append(
                Problem(
                    message=(
                        f"{op.fact_id} is not one of your facts. A resume may only state facts "
                        "you have recorded."
                    ),
                    fact_id=op.fact_id,
                    op_index=index,
                )
            )
            continue
        if not fact.confirmed:
            report.problems.append(
                Problem(
                    message=(
                        f"{op.fact_id} is still a proposal. Confirm it first: a resume may only "
                        "state facts you have agreed to."
                    ),
                    fact_id=op.fact_id,
                    op_index=index,
                )
            )
            continue
        if not op.includes:
            continue

        if op.fact_id in seen:
            report.problems.append(
                Problem(
                    message=f"{op.fact_id} is included twice.",
                    fact_id=op.fact_id,
                    op_index=index,
                )
            )
            continue
        seen.add(op.fact_id)
        report.cited.append(op.fact_id)

        if op.text:
            for message in check_rewording(op.text, fact):
                report.problems.append(Problem(message=message, fact_id=op.fact_id, op_index=index))

    return report


def line_for(op: EditOp, fact: KnownFact) -> str:
    """The one line an `include` produces. The renderer and the checker both read this."""
    return BULLET + (op.text or " ".join(fact.statement.split()))


def render(ops: list[EditOp], facts: dict[str, KnownFact], *, header: str = "") -> str:
    """The document. Sections in their canonical order, bullets in the order of the ops."""
    lines: list[str] = []
    cleaned_header = (header or "").strip()
    if cleaned_header:
        lines.extend(line.strip() for line in cleaned_header.splitlines() if line.strip())

    for section in RESUME_SECTIONS:
        bullets = [
            line_for(op, facts[op.fact_id])
            for op in ops
            if op.includes and op.section == section and op.fact_id in facts
        ]
        if not bullets:
            continue
        lines.append("")
        lines.append(section)
        lines.extend(bullets)

    return "\n".join(lines).strip() + "\n"


def check_rendered(
    text: str, ops: list[EditOp], facts: dict[str, KnownFact], *, header: str = ""
) -> Report:
    """Every line of the finished document, against what it is allowed to be.

    Run by `store.create_version` on the text it is about to write, whatever produced that text.
    A line that is not the header, a known heading or a bullet of an included op is refused and
    quoted back, so a smuggled sentence names itself.
    """
    report = Report()
    allowed_headers = {line.strip() for line in (header or "").strip().splitlines() if line.strip()}
    allowed_bullets = {
        line_for(op, facts[op.fact_id]) for op in ops if op.includes and op.fact_id in facts
    }

    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line in RESUME_SECTIONS or line in allowed_headers:
            continue
        if line in allowed_bullets:
            continue
        report.problems.append(
            Problem(
                message=(
                    f"line {number} of the rendered resume does not come from a confirmed "
                    f"fact: {line[:120]!r}"
                )
            )
        )
    return report
