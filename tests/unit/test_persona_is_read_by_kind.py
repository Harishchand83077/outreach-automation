"""`campaigns.persona` is read in one way, by the campaign's kind.

**This guard exists because the assumption was repeated seven times and corrected once.** Phase 13
added `campaigns.kind` and a second schema -- a `job_hunt` campaign stores a `CandidateProfile`,
not a `Persona` -- and updated the create route. The other six readers went on validating every
campaign as an outreach `Persona`, and each one failed differently:

- the worker's `_persona` raised, so `research_lead` died five times and no draft was ever written;
- the campaign screen's readiness check reported *"The sender persona cannot be read"* and listed
  the candidate's own field names as if they were faults;
- `SendLoop` caught the error and `continue`d, so a job-hunt campaign was **skipped in silence**
  and could never have sent anything;
- the reply drafter raised, so answering a reply crashed;
- the MCP propose tool refused;
- and `PATCH /campaigns/{id}` rejected the profile, so the person could not even save a correction.

One root cause, six symptoms, none of which named it. That is what a literal copied into six
callers costs, and this repo has a name for the shape: a correct producer, a correct consumer, and
nothing checking they agreed.

So: `Persona.model_validate` belongs to `app/domain/kinds.py`, which is the module whose job is to
know which schema applies. Everywhere else calls `sender_for`.
"""

from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path("app")

#: The module that is *allowed* to choose a schema, because choosing is what it is for.
OWNER = pathlib.Path("app/domain/kinds.py")


def _calls_persona_validate(path: pathlib.Path) -> list[int]:
    """Line numbers where this file calls `Persona.model_validate(...)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "model_validate":
            continue
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id == "Persona":
            found.append(node.lineno)
    return found


def test_only_the_kinds_module_decides_which_persona_schema_applies() -> None:
    problems: list[str] = []
    for path in sorted(APP.glob("**/*.py")):
        if path == OWNER:
            continue
        for line in _calls_persona_validate(path):
            problems.append(f"{path.as_posix()}:{line}")

    assert not problems, (
        "these call Persona.model_validate directly, which reads every campaign as an outreach "
        "campaign and breaks every job_hunt one -- use sender_for(persona, kind):\n  "
        + "\n  ".join(problems)
    )


def test_the_guard_can_see_the_call_it_is_written_about() -> None:
    """If the pattern is never matched anywhere, passing means nothing.

    Asserted against the one file that is *supposed* to contain it, so the day `load_sender` is
    rewritten to dispatch differently, this fails and asks to be reread rather than going quiet.
    """
    assert _calls_persona_validate(OWNER), "kinds.py no longer validates a Persona; reread this"


def test_every_reader_of_the_column_goes_through_the_one_function() -> None:
    """The other half: the files that *used* to hold the defect still read the column.

    Without this, deleting a call site would satisfy the test above while losing the behaviour --
    the cheapest way to make a guard pass is to remove the code it guards.
    """
    expected = [
        "app/api/routers/campaigns.py",
        "app/services/mcp/proposals.py",
        "app/services/reply/drafter.py",
        "app/worker/handlers.py",
        "app/worker/send_loop.py",
    ]

    missing = [
        name
        for name in expected
        if "sender_for" not in pathlib.Path(name).read_text(encoding="utf-8")
    ]

    assert not missing, f"these no longer load a campaign's sender at all: {missing}"
