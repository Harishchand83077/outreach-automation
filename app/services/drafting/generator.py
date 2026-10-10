"""Generating a draft: write, validate, regenerate, give up.

`ARCHITECTURE_TARGET.md` §13. The loop is bounded (issue B15 — the prototype counted attempts
and then never checked the count, so a rejection loop ran until LangGraph's recursion limit
raised at 25). Here the cap is read from campaign settings and enforced.

The order of the two validation layers is deliberate:

1. **Deterministic checks first.** They cost nothing, and an obviously broken draft should not
   spend an LLM call on a claim check.
2. **The claim check second**, and only on claims that resolved. It asks a cheap model whether
   each claim is actually supported by its evidence, which catches meaning the regex cannot —
   "we have deep experience in logistics" cited to a proof point about one logistics project.

When attempts run out the lead is left needing a human, and **error text never becomes email
content** (issue B2). A draft that cannot be made honest is not sent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.domain.persona import Persona
from app.services.drafting.prompts import (
    CLAIM_CHECK_PROMPT_VERSION,
    CLAIM_CHECK_SYSTEM,
    DRAFT_PROMPT_VERSION,
    DRAFT_SYSTEM,
    REPLY_PROMPT_VERSION,
    REPLY_SYSTEM,
    build_claim_check_prompt,
    build_draft_prompt,
)
from app.services.drafting.schema import DraftOutput
from app.services.drafting.validate import (
    ValidationReport,
    resolve_evidence,
    validate_draft,
)
from app.services.llm.errors import InvalidOutputError, LLMError


class ClaimVerdict(BaseModel):
    """The claim-check model's answer."""

    supported: bool
    reason: str = Field(default="", max_length=500)


class Router(Protocol):
    """Just the part of LLMRouter this module needs, so tests can supply a fake."""

    async def structured[M: BaseModel](
        self,
        schema: type[M],
        *,
        task: str,
        system: str,
        user: str,
        lead_id: uuid.UUID | None = ...,
        temperature: float = ...,
        contains_pii: bool | None = ...,
    ) -> M: ...


@dataclass
class DraftAttempt:
    """One pass, kept so a reviewer can see what was rejected and why."""

    draft: DraftOutput | None
    report: ValidationReport | None = None
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.draft is not None and self.report is not None and self.report.passed


@dataclass
class DraftResult:
    """The outcome of the whole bounded loop."""

    draft: DraftOutput | None = None
    attempts: list[DraftAttempt] = field(default_factory=list)
    prompt_version: str = DRAFT_PROMPT_VERSION
    claims_checked: bool = False

    @property
    def accepted(self) -> bool:
        return self.draft is not None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_feedback(self) -> str:
        for attempt in reversed(self.attempts):
            if attempt.report is not None and attempt.report.failures:
                return attempt.report.feedback()
            if attempt.error:
                return attempt.error
        return ""

    def validation_payload(self) -> dict[str, Any]:
        """Stored on the draft row, so the review queue can explain itself."""
        last = self.attempts[-1] if self.attempts else None
        return {
            "attempts": self.attempt_count,
            "prompt_version": self.prompt_version,
            "claims_checked": self.claims_checked,
            "passed": self.accepted,
            "failures": list(last.report.failures) if last and last.report else [],
            "warnings": list(last.report.warnings) if last and last.report else [],
            "rejected_attempts": [
                {
                    "failures": a.report.failures if a.report else [],
                    "error": a.error,
                }
                for a in self.attempts
                if not a.accepted
            ],
        }


async def generate_draft(
    *,
    router: Router,
    persona: Persona,
    hooks: list[dict[str, Any]],
    lead_id: uuid.UUID | None = None,
    lead_name: str | None = None,
    lead_company: str | None = None,
    job_title: str | None = None,
    notes: str | None = None,
    kind: str = "initial",
    max_attempts: int = 3,
    check_claims: bool = True,
    their_subject: str | None = None,
    their_reply: str | None = None,
    reply_label: str | None = None,
) -> DraftResult:
    """Produce a validated draft, or return the reasons it could not be produced.

    Never raises on a bad draft: a lead that needs a human is a normal outcome, and the caller
    decides what to do with it. Only an exhausted LLM chain propagates, because that is a
    capacity problem rather than a draft problem.
    """
    is_reply = kind == "reply"
    result = DraftResult(prompt_version=REPLY_PROMPT_VERSION if is_reply else DRAFT_PROMPT_VERSION)
    feedback: str | None = None

    for _ in range(max(1, max_attempts)):
        user_prompt = build_draft_prompt(
            persona=persona,
            hooks=hooks,
            lead_name=lead_name,
            lead_company=lead_company,
            job_title=job_title,
            notes=notes,
            kind=kind,
            feedback=feedback,
            their_subject=their_subject,
            their_reply=their_reply,
            reply_label=reply_label,
        )

        try:
            draft = await router.structured(
                DraftOutput,
                task="draft_reply" if is_reply else "draft_email",
                system=REPLY_SYSTEM if is_reply else DRAFT_SYSTEM,
                user=user_prompt,
                lead_id=lead_id,
            )
        except InvalidOutputError as exc:
            # The model did not return the shape we asked for. Worth another attempt, with the
            # problem spelled out.
            result.attempts.append(DraftAttempt(draft=None, error=str(exc)))
            feedback = (
                "Your previous response was not valid JSON matching the required schema. "
                "Return exactly the fields requested."
            )
            continue
        except LLMError as exc:
            result.attempts.append(DraftAttempt(draft=None, error=str(exc)))
            return result

        report = validate_draft(
            draft,
            persona=persona,
            hooks=hooks,
            lead_company=lead_company or "",
            lead_name=lead_name or "",
            kind=kind,
        )

        if report.passed and check_claims and draft.claims:
            await _check_claims(
                router=router,
                draft=draft,
                persona=persona,
                hooks=hooks,
                report=report,
                lead_id=lead_id,
            )
            result.claims_checked = True

        result.attempts.append(DraftAttempt(draft=draft, report=report))

        if report.passed:
            result.draft = draft
            return result

        feedback = report.feedback()

    # Attempts exhausted. The lead needs a human; the failed text is kept for the reviewer to
    # look at but is never treated as sendable.
    return result


async def _check_claims(
    *,
    router: Router,
    draft: DraftOutput,
    persona: Persona,
    hooks: list[dict[str, Any]],
    report: ValidationReport,
    lead_id: uuid.UUID | None,
) -> None:
    """Ask a cheap model whether each claim is really supported by what it cites.

    This catches what a regex cannot: a claim that is plausible, related, or simply stronger
    than its evidence. "Deep experience in logistics" cited to a proof point about one logistics
    project resolves fine and reads fine, and is still an overstatement.

    A failure of the check itself is a warning, not a rejection: the deterministic layer has
    already passed, and blocking a correct draft because a checker was unavailable would be the
    worse error.
    """
    for claim in draft.claims:
        evidence = resolve_evidence(claim.evidence_ref, persona, hooks)
        if evidence is None:
            continue  # already reported by the deterministic layer

        try:
            verdict = await router.structured(
                ClaimVerdict,
                task="validate_claims",
                system=CLAIM_CHECK_SYSTEM,
                user=build_claim_check_prompt(claim=claim.text, evidence=evidence),
                lead_id=lead_id,
            )
        except LLMError as exc:
            report.warn(
                f"could not verify the claim {claim.text[:60]!r} against its evidence "
                f"({type(exc).__name__}); a reviewer should read it"
            )
            continue

        if not verdict.supported:
            report.fail(
                f"the claim {claim.text[:80]!r} is not supported by {claim.evidence_ref}"
                + (f": {verdict.reason}" if verdict.reason else "")
            )


__all__ = [
    "CLAIM_CHECK_PROMPT_VERSION",
    "ClaimVerdict",
    "DraftAttempt",
    "DraftResult",
    "generate_draft",
]
