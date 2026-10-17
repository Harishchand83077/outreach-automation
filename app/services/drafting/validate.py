"""Deterministic draft validation — the layer that does not involve a model.

`ARCHITECTURE_TARGET.md` §13 specifies two layers. This is the first, and it is the one worth
trusting: every rule here is a mechanical check against the persona, the verified evidence and
the text itself. The second layer asks a cheap model whether each claim is actually supported by
its snippet, which catches meaning rather than form.

Order matters. Running the deterministic checks first means an obviously broken draft never
costs an LLM call, and a regeneration gets concrete feedback ("128 words, limit is 120") rather
than a vague complaint.

The rule that closes A3: **a number, percentage or named client may only appear if it is present
in the cited evidence.** The prototype asked the model for "our product/service and traction"
with no facts supplied, so it invented both. Here, inventing one fails the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.persona import Persona
from app.services.drafting.schema import DraftOutput

#: Markdown the model slips in despite being told plain text. Each would be sent literally.
MARKDOWN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\*\*[^*]+\*\*", "bold markdown (**...**)"),
    (r"(?<![*\w])\*[^*\n]+\*(?![*\w])", "italic markdown (*...*)"),
    (r"^\s{0,3}#{1,6}\s", "a markdown heading"),
    (r"\[[^\]]+\]\([^)]+\)", "a markdown link"),
    (r"^\s{0,3}[-*+]\s+\S", "a markdown bullet list"),
    (r"`[^`]+`", "backtick code formatting"),
)

#: Placeholders a template leaves behind. Sending one is unmistakably a robot.
PLACEHOLDER_PATTERNS: tuple[str, ...] = (
    r"\[[A-Za-z _]{2,30}\]",
    r"\{\{?[A-Za-z_. ]{2,30}\}?\}",
    r"<[A-Za-z _]{2,30}>",
    r"\bYOUR_[A-Z_]+\b",
    r"\bTODO\b",
    r"\bXX+\b",
)

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
#: Punctuation a sentence ends with, which the pattern above happily swallows. Stripped before
#: a URL is counted or compared.
_URL_TRAILING = ".,;:!?"
#: A bare number, a percentage, or a currency amount, including a magnitude suffix. The suffix
#: matters: without it "$2.4M" escaped the trailing non-word boundary, and an invented revenue
#: figure is precisely what this rule exists to catch.
_MAGNITUDE = r"(?:k|m|b|bn|cr|lakh|lac|crore|million|billion)?"
_NUMBER = re.compile(
    r"(?<![\w.])(?:[₹$€£]\s?\d[\d,.]*"
    + _MAGNITUDE
    + r"|\d[\d,.]*\s?%|\d[\d,.]*"
    + _MAGNITUDE
    + r")(?![\w])",
    re.IGNORECASE,
)
#: Capitalised multi-word names, which is how a fabricated client reference looks.
_PROPER_NOUN = re.compile(r"\b(?:[A-Z][a-zA-Z0-9&.-]+)(?:\s+[A-Z][a-zA-Z0-9&.-]+){1,3}\b")
_NON_WORD = re.compile(r"[^a-z0-9]+")

#: Numbers that carry no claim: a meeting length, a day count, an ordinary year.
INNOCUOUS_NUMBERS: frozenset[str] = frozenset(
    {
        "1",
        "2",
        "3",
        "4",
        "5",
        "10",
        "15",
        "20",
        "30",
        "45",
        "60",
        "90",
        "2024",
        "2025",
        "2026",
        "2027",
    }
)


@dataclass
class ValidationReport:
    """Why a draft passed or failed, in words a regeneration can act on."""

    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Claims whose `evidence_ref` did not resolve.
    unresolved_claims: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures

    def fail(self, reason: str) -> None:
        self.failures.append(reason)

    def warn(self, reason: str) -> None:
        self.warnings.append(reason)

    def feedback(self) -> str:
        """Concrete instructions for the next attempt.

        Specific beats scolding: "the body is 142 words, the limit is 120" tells the model what
        to do, where "too long" invites it to guess.
        """
        return "\n".join(f"- {reason}" for reason in self.failures)


def _normalise(text: str) -> str:
    return _NON_WORD.sub(" ", (text or "").lower())


def evidence_corpus(persona: Persona, hooks: list[dict[str, object]]) -> str:
    """Everything a draft is allowed to draw specifics from.

    Persona proof points plus verified research hooks — and nothing else. A number that appears
    nowhere in here was invented.
    """
    parts = [point.text for point in persona.proof_points]
    parts += [str(hook.get("snippet", "")) for hook in hooks]
    parts += [str(hook.get("text", "")) for hook in hooks]
    parts.append(persona.offer)
    parts.extend(persona.services)
    if persona.ideal_customer:
        parts.append(persona.ideal_customer)
    return " \n ".join(parts)


def resolve_evidence(ref: str, persona: Persona, hooks: list[dict[str, object]]) -> str | None:
    """The text behind a citation, or None if it does not exist."""
    if ref.startswith("pp"):
        point = persona.proof_point(ref)
        return point.text if point else None
    for hook in hooks:
        if str(hook.get("id") or "") == ref:
            return f"{hook.get('text', '')} {hook.get('snippet', '')}".strip()
    return None


def validate_draft(
    draft: DraftOutput,
    *,
    persona: Persona,
    hooks: list[dict[str, object]] | None = None,
    lead_company: str = "",
    lead_name: str = "",
    kind: str = "initial",
) -> ValidationReport:
    """Run every deterministic rule. A failure means the draft must not be sent as it stands."""
    report = ValidationReport()
    hooks = hooks or []
    corpus = evidence_corpus(persona, hooks)
    normalised_corpus = _normalise(corpus)

    _check_subject(draft, report, kind=kind)
    _check_length(draft, persona, report, kind=kind)
    _check_plain_text(draft, report)
    _check_placeholders(draft, report)
    _check_forbidden_phrases(draft, persona, report)
    _check_links(draft, persona, report)
    _check_claims_resolve(draft, persona, hooks, report)
    _check_numbers(draft, normalised_corpus, report)
    _check_named_entities(
        draft,
        normalised_corpus,
        persona,
        report,
        lead_company=lead_company,
        lead_name=lead_name,
    )

    return report


def validate_rendered(
    draft: DraftOutput,
    *,
    persona: Persona,
    kind: str = "initial",
) -> ValidationReport:
    """Validate an email the **operator** wrote, rendered from a template.

    Deliberately a narrower set of rules than `validate_draft`, and the difference is the whole
    design. `validate_draft` exists because a model was asked to write about a company it was
    told about, and most of its rules are there to catch invention: unresolved claims, numbers
    that appear nowhere in the evidence, proper nouns that were never mentioned, a subject longer
    than six words. **None of that applies to text a person typed.** Hard rule 6 forbids the
    *drafter* inventing facts about the sender; an operator writing their own email is the
    authority on what their offer is, and failing their draft for saying so would be the system
    refusing to send the email it was asked to send.

    What still applies is everything about the *shape* of the message, because that is about
    deliverability and about the recipient rather than about truthfulness:

    - **an unresolved merge field**, which is the whole point. `Rendered.missing` already catches
      this and this catches it again from the other side, looking at the finished text rather
      than at the substitution. Two independent checks, because `Hi {{first_name}},` reaching a
      recipient is the single most embarrassing thing this feature can do.
    - **forbidden phrases**, spam triggers and anything the campaign added to `avoid`.
    - **plain text only** -- no markdown, no HTML, since neither is rendered by this sender.
    - **at most one link, and only the booking URL.** Several links read as marketing whoever
      wrote them.

    Length is a *warning* rather than a failure here: `max_words` is advice to a model, and an
    operator who writes 160 words has decided to.
    """
    report = ValidationReport()

    _check_placeholders(draft, report)
    _check_plain_text(draft, report)
    _check_forbidden_phrases(draft, persona, report)
    _check_links(draft, persona, report)

    if not draft.subject.strip():
        report.fail("the subject is empty")
    if kind == "initial" and draft.subject.lower().startswith(("re:", "fwd:", "fw:")):
        # Faking a reply to a conversation that never happened is a deception whoever typed it.
        report.fail("a first email must not begin with 'Re:' or 'Fwd:'")

    words = len(draft.body.split())
    limit = persona.max_words
    if words > limit:
        report.warn(f"the body is {words} words against this campaign's limit of {limit}")

    return report


def _check_subject(draft: DraftOutput, report: ValidationReport, *, kind: str) -> None:
    words = draft.subject_word_count
    # A reply keeps the thread's subject, so its length is whatever they chose and is not ours
    # to shorten. Rewriting it would break threading in some clients and looks evasive in all.
    if words > 6 and kind != "reply":
        # Long subjects read as marketing and get truncated on a phone anyway.
        report.fail(f"the subject is {words} words; keep it to 6 or fewer")
    if not draft.subject.strip():
        report.fail("the subject is empty")

    lowered = draft.subject.lower()
    if kind == "reply" and not lowered.startswith("re:"):
        report.fail("a reply must keep the thread's subject, prefixed with 'Re:'")
    if kind == "initial" and lowered.startswith(("re:", "fwd:", "fw:")):
        # Faking a reply to a conversation that never happened is a deception, and recipients
        # recognise it instantly.
        report.fail("a first email must not begin with 'Re:' or 'Fwd:'")
    if draft.subject.isupper() and len(draft.subject) > 4:
        report.fail("the subject must not be in capitals")
    if draft.subject.count("!") > 0:
        report.warn("exclamation marks in a subject hurt deliverability")


def _check_length(
    draft: DraftOutput, persona: Persona, report: ValidationReport, *, kind: str = "initial"
) -> None:
    words = draft.body_word_count
    if words > persona.max_words:
        report.fail(
            f"the body is {words} words; the limit for this campaign is {persona.max_words}"
        )
    # A cold email under 20 words has not said anything. A reply can legitimately be
    # "Thursday at 3 works, I will send an invite" — holding a reply to the same floor would
    # pad it, and padding an answer is how a short email becomes an annoying one.
    floor = 8 if kind == "reply" else 20
    if words < floor:
        report.fail(f"the body is only {words} words, which is too short to be a real message")


def _check_plain_text(draft: DraftOutput, report: ValidationReport) -> None:
    """Markdown would be sent literally: a recipient would see the asterisks."""
    for pattern, description in MARKDOWN_PATTERNS:
        if re.search(pattern, draft.body, flags=re.MULTILINE):
            report.fail(f"the body contains {description}; send plain text only")
    if re.search(r"<[a-z/][^>]*>", draft.body, flags=re.IGNORECASE):
        report.fail("the body contains HTML tags; send plain text only")


def _check_placeholders(draft: DraftOutput, report: ValidationReport) -> None:
    for pattern in PLACEHOLDER_PATTERNS:
        match = re.search(pattern, draft.body) or re.search(pattern, draft.subject)
        if match:
            report.fail(f"an unfilled placeholder {match.group(0)!r} is still in the text")
            return


def _check_forbidden_phrases(
    draft: DraftOutput, persona: Persona, report: ValidationReport
) -> None:
    haystack = f"{draft.subject}\n{draft.body}".lower()
    for phrase in sorted(persona.forbidden_phrases):
        if phrase and phrase in haystack:
            report.fail(f"the text contains the forbidden phrase {phrase!r}")


def _check_links(draft: DraftOutput, persona: Persona, report: ValidationReport) -> None:
    """One call to action, one link.

    Several links look like marketing, dilute the ask and hurt deliverability. The footer's
    opt-out and identity lines are added by the sender, not the model, so they are not counted
    here.
    """
    urls = [found_url(raw) for raw in _URL.findall(draft.body)]
    booking = found_url(str(persona.booking_url)) if persona.booking_url else ""

    if len(urls) > 1:
        report.fail(f"the body contains {len(urls)} links; include at most one")

    if urls and booking:
        # Compared without the trailing slash, which models add and drop at random.
        if urls[0].rstrip("/") != booking.rstrip("/"):
            report.fail(f"the only link allowed is the booking URL {booking}, not {urls[0]}")


def found_url(raw: str) -> str:
    """A URL as matched in prose, with the sentence's punctuation taken back off.

    `_URL` deliberately matches greedily, so a sentence ending "...book here:
    https://cal.example.com/asha." captures the full stop as part of the URL. That produced a
    draft rejected on the live system with a message nobody could act on, because the two
    strings it printed looked identical:

        the only link allowed is the booking URL https://calendar.google.com/...pli=1,
        not https://calendar.google.com/...pli=1.

    A correct draft was refused because its last sentence ended in a full stop. None of
    `.,;:!?` carries meaning at the end of a URL, and a trailing one is always the prose's.
    """
    return raw.rstrip(_URL_TRAILING)


def _check_claims_resolve(
    draft: DraftOutput,
    persona: Persona,
    hooks: list[dict[str, object]],
    report: ValidationReport,
) -> None:
    """Every citation must point at something that exists."""
    for claim in draft.claims:
        if resolve_evidence(claim.evidence_ref, persona, hooks) is None:
            report.unresolved_claims.append(claim.evidence_ref)
            report.fail(
                f"claim {claim.text[:60]!r} cites {claim.evidence_ref}, which does not exist"
            )


def _check_numbers(draft: DraftOutput, normalised_corpus: str, report: ValidationReport) -> None:
    """A figure must come from the evidence. This is the rule that closes A3.

    The prototype asked for "our traction" with nothing supplied, so the model produced numbers.
    Here an unsupported figure fails, because a recipient cannot tell an invented metric from a
    real one and we would be the ones who sent it.
    """
    for raw in _NUMBER.findall(draft.body):
        token = raw.strip()
        digits = token.strip("₹$€£% ").replace(",", "").rstrip(".").lower()
        for suffix in ("crore", "million", "billion", "lakh", "lac", "bn", "cr", "k", "m", "b"):
            if digits.endswith(suffix):
                digits = digits[: -len(suffix)].rstrip(".")
                break
        if digits in INNOCUOUS_NUMBERS:
            continue
        if _normalise(token).strip() in normalised_corpus:
            continue
        if digits and digits in normalised_corpus.replace(",", ""):
            continue
        report.fail(
            f"the figure {token!r} does not appear in the cited evidence; "
            "remove it or cite a source that states it"
        )
        return


def _check_named_entities(
    draft: DraftOutput,
    normalised_corpus: str,
    persona: Persona,
    report: ValidationReport,
    *,
    lead_company: str,
    lead_name: str,
) -> None:
    """A named company other than ours or theirs is almost always a fabricated reference.

    Warns rather than fails: capitalised phrases are a noisy signal — "Looking Forward" would
    trip it — so a reviewer decides. The number check above is the one that fails hard, because
    a figure is unambiguous.
    """
    known = {
        _normalise(persona.agency_name).strip(),
        _normalise(lead_company).strip(),
        _normalise(lead_name).strip(),
        *(_normalise(service).strip() for service in persona.services),
    }
    known.discard("")

    for candidate in set(_PROPER_NOUN.findall(draft.body)):
        normalised = _normalise(candidate).strip()
        if not normalised or normalised in normalised_corpus:
            continue
        # Token overlap, not whole-string: "Hi Ben" shares "ben" with the lead's name, and
        # greetings and sign-offs would otherwise warn on every draft.
        candidate_tokens = set(normalised.split())
        if any(candidate_tokens & set(name.split()) for name in known):
            continue
        report.warn(
            f"{candidate!r} is a name that does not appear in the evidence or the persona — "
            "check it is not an invented reference"
        )
