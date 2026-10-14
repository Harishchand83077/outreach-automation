"""Fixed templates with merge fields, for campaigns that do not want a model writing the email.

`ARCHITECTURE_TARGET.md` assumes one drafter: research the company, then ask a model for a
grounded email. That is the right default for agency outreach and the wrong tool for the other
half of the job -- a job hunt, or a list where every recipient genuinely should get the same
message with their own name in it. For those the operator writes the email once and the system
fills in the blanks, the way every sequencer does it.

So a campaign has a **mode**. `research` is unchanged. `template` renders the operator's text and
never calls a model for a first email or a follow-up. Replies are a different matter and stay
with the model in both modes, because a reply has to answer what the person actually said.

**What this module is careful about, and why.**

*An unresolved placeholder must never reach a recipient.* `Hi {{first_name}},` is the most
recognisable failure of mail-merge and `Hi ,` is the second. Both are worse than a generic
opening, because both announce that nobody read it. So a placeholder either resolves, or has an
explicit fallback, or the render **fails** and the draft is reported invalid rather than offered
for approval. `Rendered.missing` is what the review queue blocks on.

*Substitution is one pass and never recursive.* The template is written by the operator, but the
values come from an uploaded spreadsheet -- which is to say, from outside. A company name of
`{{booking_url}}` must appear in the email as those literal characters rather than expanding
into anything. One pass makes that true by construction instead of by escaping.

*Nothing is evaluated.* No expressions, no conditionals, no attribute access: a field name and an
optional literal fallback. A templating engine would hand the operator loops over arbitrary
objects, and would hand a spreadsheet a path into the process.

Pure data and pure functions: no I/O and no clock, so every rule above is testable without a
database (§4's rule for `app/domain/`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: `{{field}}` or `{{field|fallback}}`. Deliberately narrow: a field name is letters, digits and
#: underscores, so nothing in a spreadsheet header can smuggle in syntax. The fallback is a
#: literal and may not itself contain a brace.
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|([^{}]*))?\}\}")

#: Anything that looks like it was *meant* to be a placeholder but is not one. Caught when the
#: template is saved, because `{first_name}` and `{{first name}}` would otherwise be emailed
#: verbatim and the operator would never hear it from us.
SUSPECT = re.compile(r"\{\{[^{}]*\}\}|\{[a-zA-Z0-9_ ]+\}|%[a-zA-Z_]+%|\$\{[^{}]*\}")

#: Fields every lead has, whatever the sheet looked like. `first_name` is derived rather than
#: stored: a sheet gives "Asha Rao" and the email wants "Asha".
LEAD_FIELDS: frozenset[str] = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "email",
        "company",
        "company_url",
        "domain",
        "job_title",
        "notes",
    }
)

#: Fields from the campaign's persona, so a template can sign itself without the operator
#: retyping their own name into every one of them.
PERSONA_FIELDS: frozenset[str] = frozenset(
    {
        "sender_name",
        "sender_role",
        "agency_name",
        "booking_url",
        "offer",
    }
)

MAX_BODY_CHARS = 5000
MAX_SUBJECT_CHARS = 200

_HONORIFICS: frozenset[str] = frozenset({"mr", "mrs", "ms", "dr", "prof", "shri", "smt", "sri"})


@dataclass(frozen=True)
class Rendered:
    """One rendered email, with everything that went wrong while rendering it."""

    subject: str
    body: str
    #: Placeholders that had no value and no fallback. A non-empty tuple means do not send: the
    #: draft is still stored, so the operator can see it and fix the sheet or the template.
    missing: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing

    @property
    def reason(self) -> str:
        """Why this draft cannot be approved, phrased for a reviewer rather than for a log."""
        if self.ok:
            return ""
        fields = ", ".join(sorted(set(self.missing)))
        example = self.missing[0]
        return (
            f"this lead has no value for {fields}. Fill that column in the sheet, or give the "
            f"placeholder a fallback like {{{{{example}|there}}}}"
        )


def first_name_of(full_name: str | None) -> str:
    """The part of a name to open an email with.

    Deliberately simple: the first whitespace-separated word, with an honorific dropped. Getting
    clever here produces confidently wrong output on exactly the names it cannot parse, and an
    operator can always map a `first_name` column of their own, which wins over this.
    """
    parts = [p for p in (full_name or "").replace(",", " ").split() if p]
    while parts and parts[0].rstrip(".").lower() in _HONORIFICS:
        parts.pop(0)
    return parts[0] if parts else ""


def last_name_of(full_name: str | None) -> str:
    """The last word of a name, or empty for a single-word name -- not the word itself."""
    parts = [p for p in (full_name or "").replace(",", " ").split() if p]
    while parts and parts[0].rstrip(".").lower() in _HONORIFICS:
        parts.pop(0)
    return parts[-1] if len(parts) > 1 else ""


def _clean(value: Any) -> str:
    """A spreadsheet cell as it may appear inside an email.

    Control characters are removed rather than escaped. They have no business in a plain-text
    email, they are invisible in a review screen, and a lone carriage return is enough to turn a
    subject line into two headers in any mailer that trusts it.
    """
    if value is None or isinstance(value, bool):
        return ""
    text = value if isinstance(value, str) else str(value)
    text = "".join(ch for ch in text if ord(ch) >= 32 or ch == "\t")
    return text.strip()


def slug_of(column: str) -> str:
    """A spreadsheet header as a merge-field name. "Company Name" becomes `company_name`."""
    return re.sub(r"[^a-z0-9_]+", "_", str(column).strip().lower()).strip("_")


def build_context(
    *,
    full_name: str | None = None,
    email: str = "",
    company_name: str | None = None,
    company_url: str | None = None,
    domain: str | None = None,
    job_title: str | None = None,
    notes: str | None = None,
    extra: dict[str, Any] | None = None,
    persona: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Everything a template may refer to, flattened and cleaned.

    Precedence matters. The persona is lowest, because it is the same for every lead. Our derived
    fields come next. A column from the operator's own sheet wins over both when it has a value,
    because somebody who mapped a `first_name` column knows their list better than
    `first_name_of` does -- but an *empty* cell does not win, or a blank column would blank the
    email that the derived value could have filled.
    """
    context: dict[str, str] = {}

    for key, value in (persona or {}).items():
        if key in PERSONA_FIELDS:
            context[key] = _clean(value)

    context.update(
        {
            "first_name": first_name_of(full_name),
            "last_name": last_name_of(full_name),
            "full_name": _clean(full_name),
            "email": _clean(email),
            "company": _clean(company_name),
            "company_url": _clean(company_url),
            "domain": _clean(domain),
            "job_title": _clean(job_title),
            "notes": _clean(notes),
        }
    )

    for key, value in (extra or {}).items():
        slug = slug_of(key)
        if not slug:
            continue
        cleaned = _clean(value)
        if cleaned or slug not in context:
            context[slug] = cleaned

    return context


def render(text: str, context: dict[str, str]) -> tuple[str, list[str]]:
    """Substitute placeholders once. Returns the text, and the fields that had nothing to offer.

    A field is missing when it resolves to an empty string *and* the placeholder gave no
    fallback. An empty fallback -- `{{job_title|}}` -- is an explicit decision and is honoured.
    """
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        field = match.group(1)
        fallback = match.group(2)
        value = context.get(field, "")
        if value:
            return value
        if fallback is not None:
            return fallback.strip()
        missing.append(field)
        # Left as-is, so the stored draft shows the reviewer exactly where the hole is. It can
        # never be sent: `missing` is non-empty and the review queue refuses to approve it.
        return match.group(0)

    return PLACEHOLDER.sub(replace, text), missing


def tidy(text: str) -> str:
    """Clean up the whitespace a substitution leaves behind.

    `Role: {{job_title|}}` renders as `Role: ` and `Hi {{title|}} {{first_name}}` renders with a
    double space. Neither is wrong exactly, and both are the visible tell of a template -- which
    is the one thing a personalised email must not look like. So runs of spaces collapse and
    trailing whitespace goes, per line, which a plain-text cold email never needs. Blank lines
    are left alone: paragraph breaks are the operator's formatting.
    """
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def placeholders_in(text: str) -> set[str]:
    """Every field a template refers to. Used to tell the operator what the sheet must carry."""
    return {match.group(1) for match in PLACEHOLDER.finditer(text or "")}


def suspect_placeholders_in(text: str) -> list[str]:
    """Things that look like placeholders but are not, so they would be emailed verbatim.

    `{first_name}`, `{{first name}}` with a space, `%NAME%` and `${name}` are all shapes an
    operator arrives with from some other tool. Sending them silently is the failure this
    catches: it is found at the point of writing rather than by a recipient.
    """
    found: list[str] = []
    for match in SUSPECT.finditer(text or ""):
        token = match.group(0)
        if PLACEHOLDER.fullmatch(token):
            continue
        found.append(token)
    return found


class EmailTemplate(BaseModel):
    """One operator-written email with merge fields."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)

    @field_validator("subject", "body")
    @classmethod
    def _no_control_characters(cls, v: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in v):
            raise ValueError("must not contain control characters")
        return v

    @field_validator("subject")
    @classmethod
    def _subject_is_one_line(cls, v: str) -> str:
        """A newline in a subject is two headers in any mailer that does not fold it."""
        if "\n" in v or "\r" in v:
            raise ValueError("a subject must be a single line")
        return v.strip()

    @model_validator(mode="after")
    def _no_near_miss_placeholders(self) -> Self:
        suspects = suspect_placeholders_in(self.subject) + suspect_placeholders_in(self.body)
        if suspects:
            shown = ", ".join(sorted(set(suspects))[:5])
            raise ValueError(
                f"{shown} is not a merge field -- use double braces and a field name, "
                f"like {{{{first_name}}}}"
            )
        return self

    @property
    def fields(self) -> set[str]:
        return placeholders_in(self.subject) | placeholders_in(self.body)

    def render_for(self, context: dict[str, str]) -> Rendered:
        subject, missing_subject = render(self.subject, context)
        body, missing_body = render(self.body, context)
        return Rendered(
            subject=tidy(subject),
            body=tidy(body),
            missing=tuple(missing_subject + missing_body),
        )


class FollowupTemplate(EmailTemplate):
    """A follow-up, and how long after the previous email it is due."""

    after_days: int = Field(default=3, ge=1, le=60)


class CampaignTemplates(BaseModel):
    """Every fixed email a `template`-mode campaign sends, written once when it is created."""

    model_config = ConfigDict(extra="forbid")

    initial: EmailTemplate
    followups: list[FollowupTemplate] = Field(default_factory=list, max_length=5)

    @property
    def fields(self) -> set[str]:
        """Every field this campaign's sheet needs to carry."""
        found = set(self.initial.fields)
        for followup in self.followups:
            found |= followup.fields
        return found

    def unknown_fields(self, known_columns: set[str] | None = None) -> set[str]:
        """Fields that are neither ours nor a column the operator's sheet provides.

        Checked when a sheet is known rather than when the template is written: a campaign is
        routinely created before the list exists, and refusing `{{industry}}` at that point
        would be refusing to let somebody write their email first.
        """
        known = LEAD_FIELDS | PERSONA_FIELDS | {slug_of(c) for c in (known_columns or set())}
        return {field for field in self.fields if field not in known}

    def for_followup(self, index: int) -> FollowupTemplate | None:
        """The template for follow-up number `index`, zero-based, or None if there is none.

        None means stop, and is not an error. `max_followups` and the number of templates are
        set in two different places and the smaller has to win, or a campaign tries to send an
        email nobody wrote.
        """
        return self.followups[index] if 0 <= index < len(self.followups) else None
