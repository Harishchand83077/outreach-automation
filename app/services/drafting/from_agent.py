"""Drafting by somebody else's model: validate what an agent proposed (ADR-030).

The third producer of a `DraftResult`, after the research drafter and the template renderer, and
for the same reason as the second: the review queue, `save_draft`, the approval route, the send
loop and the audit trail all already know how to handle one. A proposal that arrived over MCP
reaches a recipient down exactly the path an in-app draft does -- same validator, same human
approval, same cap, same window, same suppression check. What changes is who wrote the words.

**It validates with `validate_draft`, not `validate_rendered`, and that is the whole decision.**
ADR-012 made the template path's validation deliberately narrower -- no invention checks -- on
the grounds that an operator typing their own email is the authority on their own offer, and
failing their draft for asserting it would be the system refusing to send what it was asked to
send. A model is not that authority. Two reasons, and the second is the serious one:

1. Hard rule 6 forbids the drafter inventing facts about the sender. Which model is doing the
   inventing does not change what the recipient is told.
2. **The agent may have just read a lead's reply.** Hard rule 7 calls that untrusted input, and
   over MCP it reaches something holding tools. A proposal is the one place where text a
   stranger wrote could come back as text we would send, so the claim check -- every concrete
   assertion resolving to a persona proof point or a verified research hook -- is exactly the
   defence that has to apply here, and it has to apply unrelaxed.

So a proposal is held to a *higher* standard than an operator's template and the same standard
as our own drafter. A proposal that fails is still stored, with its reasons, because a reviewer
seeing why beats a draft that silently never appeared -- and because the agent is the one party
that can correct it, the reasons go back in the tool result too.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.domain.persona import Persona
from app.services.drafting.generator import DraftAttempt, DraftResult
from app.services.drafting.schema import DraftOutput
from app.services.drafting.validate import validate_draft

#: Stored on the draft row in place of a prompt version. A reviewer asking "what wrote this?"
#: gets an answer that is true: not our prompt, somebody's own client. `created_by` carries
#: *whose*, and `docs/DATASET_DESIGN.md` needs both -- an agent-written draft is not a sample of
#: our own drafter's behaviour and must not be counted as one.
AGENT_VERSION = "agent-v1"


class ProposalRejected(ValueError):
    """The text could not be made into a draft at all, so there is nothing to store.

    Distinct from failing validation on purpose. A body of 9,000 characters or a subject with a
    newline in it is not a weak draft, it is not a draft -- and the caller turns this into an
    error the agent can act on by sending different arguments, rather than a row in a queue that
    no editing could rescue.
    """


def draft_from_agent(
    *,
    subject: str,
    body: str,
    claims: list[dict[str, Any]] | None = None,
    persona: Persona,
    hooks: list[dict[str, Any]] | None = None,
    lead_name: str | None = None,
    lead_company: str | None = None,
    kind: str = "initial",
) -> DraftResult:
    """Validate an agent's proposed email and return it as a storable result.

    No network, no model and no clock: given the same arguments, the same verdict every time.
    `ProposalRejected` means the arguments were not an email; a result with `accepted == False`
    means they were an email that the checks object to, and both of those are useful answers.
    """
    try:
        # Pydantic coerces each mapping into a `Claim`, which is what refuses an invented
        # citation format (`source_1` instead of `pp2`) before any rule looks at the text.
        output = DraftOutput.model_validate(
            {"subject": subject, "body": body, "claims": list(claims or [])}
        )
    except ValidationError as exc:
        # The field names and the rule, never a dump of the model's own error objects: this text
        # goes back over the wire to a client and into a log.
        reasons = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ProposalRejected(reasons or "the proposal is not a usable subject and body") from exc

    report = validate_draft(
        output,
        persona=persona,
        hooks=hooks or [],
        lead_company=lead_company or "",
        lead_name=lead_name or "",
        kind=kind,
    )

    # The text is kept on the attempt whether it passed or not, which is what lets `save_draft`
    # store a failed proposal as `validation_failed` rather than refuse it for having no text.
    # `DraftResult.draft` stays None on a failure, and that is what `accepted` reads, so a
    # failed proposal can never be mistaken for an approvable one.
    return DraftResult(
        draft=output if report.passed else None,
        attempts=[DraftAttempt(draft=output, report=report)],
        prompt_version=AGENT_VERSION,
        # Our own claim-check model was not called. Saying otherwise on the stored row would be
        # a claim about rigour a reviewer has no way to check -- the deterministic resolution of
        # every `evidence_ref` did run, and that is what `validation` records.
        claims_checked=False,
    )
