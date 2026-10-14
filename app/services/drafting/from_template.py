"""Drafting without a model: render the operator's template for one lead.

The research drafter and this one produce the **same** `DraftResult`, which is the point. The
review queue, `save_draft`, the approval route, the send loop and the audit trail all already
know how to handle one, so a template-mode campaign reaches a recipient down exactly the path a
researched draft does -- same cap, same window, same suppression check, same human approval. The
mode changes who writes the email and nothing else about what happens to it.

That is also why this returns a `DraftResult` with one `DraftAttempt` rather than something
simpler. There is no loop to run: a template either renders or it does not, and a second attempt
would produce the same text. But a draft that failed has to be *stored with its reason*, because
a reviewer looking at the queue needs to see that this lead has no company name rather than
wonder why it is missing.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.domain.persona import Persona
from app.domain.template import EmailTemplate, build_context
from app.services.drafting.generator import DraftAttempt, DraftResult
from app.services.drafting.schema import DraftOutput
from app.services.drafting.validate import ValidationReport, validate_rendered

#: Stored on the draft row in place of a prompt version. A reviewer asking "what wrote this?"
#: gets an answer that is true, and a later change to the renderer is visible in the history.
TEMPLATE_VERSION = "template-v1"


def draft_from_template(
    *,
    template: EmailTemplate,
    persona: Persona,
    lead_id: uuid.UUID,
    full_name: str | None = None,
    email: str = "",
    company_name: str | None = None,
    company_url: str | None = None,
    domain: str | None = None,
    job_title: str | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
    kind: str = "initial",
) -> DraftResult:
    """Render one lead's email. No network, no model, no clock: given the same row, the same
    text, every time.

    A render with an unfilled merge field comes back **not accepted**, with the missing fields
    named in the failure. The draft is still returned so it can be stored and looked at -- the
    hole is visible in the body, and `accepted` being false is what keeps it out of the review
    queue and therefore out of a mailbox.
    """
    context = build_context(
        full_name=full_name,
        email=email,
        company_name=company_name,
        company_url=company_url,
        domain=domain,
        job_title=job_title,
        notes=notes,
        extra=extra,
        persona=persona.model_dump(mode="json"),
    )
    rendered = template.render_for(context)

    report = ValidationReport()
    if not rendered.ok:
        report.fail(rendered.reason)

    output: DraftOutput | None = None
    try:
        candidate = DraftOutput(subject=rendered.subject, body=rendered.body, claims=[])
    except ValueError as exc:
        # The template passed its own validation when it was saved, so this is a render that
        # broke it: a cell long enough to push the body past the column limit, or a subject that
        # became empty because every placeholder in it resolved to nothing.
        report.fail(f"this lead's values do not make a sendable email: {exc}")
    else:
        output = candidate
        shape = validate_rendered(candidate, persona=persona, kind=kind)
        for failure in shape.failures:
            report.fail(failure)
        for warning in shape.warnings:
            report.warn(warning)

    attempt = DraftAttempt(draft=output if report.passed else None, report=report)
    return DraftResult(
        draft=output if report.passed else None,
        attempts=[attempt],
        prompt_version=TEMPLATE_VERSION,
        # No claim-check model is called, and saying otherwise on the stored row would be a lie
        # a reviewer could not detect. A template's claims are the operator's own.
        claims_checked=False,
    )
