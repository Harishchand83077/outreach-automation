"""What a campaign is *for*, and the few things that differ because of it.

`docs/CAMPAIGN_KINDS.md` is the analysis. The short version: an agency selling projects and a
candidate looking for work are doing the same mechanical thing, so the pipeline is shared and
this module holds the small set of genuine differences — defaults, the sender's shape, the
drafting requirements, and the opt-out register.

**`kind` is orthogonal to `mode`.** `mode` (`research` | `template`, ADR-012) already decides
*who writes the email*: a researched grounded draft, or the operator's own text with merge
fields. That is the "researched draft vs fixed draft" distinction, and both kinds get both. Four
combinations, one pipeline, no third concept.

**The sender is a different input form, not a different contract.** A `CandidateProfile` compiles
into the same `Persona` the drafter, validator, footer and claim check already take, with the
candidate's facts becoming proof points under the same `pp\\d+` ids. That is what lets a
`job_hunt` draft inventing experience fail the *same* check, with the *same* message, as an
`outreach` draft inventing a statistic — one rule, enforced in one place, for two kinds.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.domain.persona import NonEmptyStr, Persona, ProofPoint


class CampaignKind(StrEnum):
    """Why this campaign exists. Stored on `campaigns.kind`."""

    OUTREACH = "outreach"
    JOB_HUNT = "job_hunt"


KINDS: tuple[CampaignKind, ...] = (CampaignKind.OUTREACH, CampaignKind.JOB_HUNT)


@dataclass(frozen=True)
class KindDefaults:
    """The settings a campaign of this kind starts with.

    Defaults only — every one stays editable. A kind that *enforced* a cap would be a second
    place sending limits are decided, and `CampaignSettings` is the first.
    """

    daily_cap: int
    max_followups: int
    #: Ceiling the UI offers. The hard ceiling of 3 in `CampaignSettings` still applies above it.
    followups_ceiling: int
    attach_by_default: bool
    modes: tuple[str, ...]
    goal_hint: str


_DEFAULTS: dict[CampaignKind, KindDefaults] = {
    CampaignKind.OUTREACH: KindDefaults(
        # Today's values, exactly. The migration changes no behaviour because there is no
        # behaviour here to change.
        daily_cap=100,
        max_followups=1,
        followups_ceiling=3,
        attach_by_default=False,
        modes=("research", "template"),
        goal_hint="book a 20-minute call",
    ),
    CampaignKind.JOB_HUNT: KindDefaults(
        # **25, and not a product opinion about ambition.** A candidate sends from a personal
        # Gmail with no domain reputation, no SPF/DKIM alignment of their own and no warm-up
        # history; 25 a day is the volume at which such an account does not look compromised.
        # The warm-up ramp still applies on top of it.
        daily_cap=25,
        max_followups=1,
        followups_ceiling=2,
        attach_by_default=False,
        modes=("research", "template"),
        goal_hint="a short conversation about openings",
    ),
}


#: The opt-out sentence each kind ends on.
#:
#: **Different register, identical obligation.** Both kinds are cold email to somebody who did not
#: ask for it, so both carry an honest identity and a working opt-out, and the suppression list
#: treats them the same. What changes is only that a job enquiry borrowing sales language reads as
#: insincere -- it is one person asking another for a conversation.
_OPT_OUT: dict[CampaignKind, str] = {
    CampaignKind.OUTREACH: ('Not relevant? Reply with "no thanks" and I won\'t email you again.'),
    CampaignKind.JOB_HUNT: (
        "If you'd rather I didn't follow up, just say and I won't write again."
    ),
}


def defaults_for(kind: CampaignKind | str) -> KindDefaults:
    """The starting settings for this kind.

    Raises on an unknown kind rather than falling back: a campaign created with a kind nobody
    implemented would otherwise silently get outreach's cap, which is four times the one a
    candidate should be sending at.
    """
    try:
        resolved = CampaignKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown campaign kind {kind!r}") from exc
    return _DEFAULTS[resolved]


def opt_out_line_for(kind: CampaignKind | str) -> str:
    return _OPT_OUT[CampaignKind(kind)]


class CandidateFact(ProofPoint):
    """One checkable statement about the candidate.

    Reuses `ProofPoint` so the id keeps the `pp\\d+` shape the claim checker resolves. A fact with
    an id of any other shape is evidence that cannot be cited, which would make the grounding rule
    unenforceable exactly where it matters most.
    """


class CandidateProfile(BaseModel):
    """The sender of a `job_hunt` campaign: a person, with a history.

    **The drafter may assert only what is in here.** `outreach` already forbids inventing facts
    about the sender (hard rule 6); for a candidate the same rule bites harder, because the
    failure mode is worse. An email claiming three years of Kubernetes they do not have is not a
    weak email -- it is a lie told in their name, which they will be asked about in an interview.

    No résumé parsing, deliberately. The candidate writes their own facts, which is the only
    version in which the grounding rule means anything: a parser inventing a fact would be
    inventing it on behalf of the person who gets asked about it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["job_hunt"] = "job_hunt"

    full_name: NonEmptyStr
    #: "Backend engineer, 4 years". Signs the email beside the name.
    headline: str = Field(default="", max_length=300)
    target_role: str = Field(default="", max_length=500)
    facts: list[CandidateFact] = Field(default_factory=list, max_length=30)
    #: Portfolio, GitHub or a résumé link. A link is strongly preferred to an attachment: cold
    #: mail carrying a file lands in spam far more often, and a link costs the reader nothing.
    links: list[HttpUrl] = Field(default_factory=list, max_length=5)

    goal: str = Field(default="", max_length=500)
    cta_text: str = Field(default="", max_length=300)
    booking_url: HttpUrl | None = None
    tone: str = Field(default="direct and warm", max_length=200)
    language: str = Field(default="en", max_length=20)
    avoid: list[str] = Field(default_factory=list, max_length=100)
    signature: str = Field(default="", max_length=1000)
    max_words: int = Field(default=140, ge=40, le=400)
    followup_angle: str = Field(default="", max_length=1000)

    def as_persona(self) -> Persona:
        """Compile into the `Persona` every downstream component already takes.

        The facts become proof points with their ids intact, so `evidence_corpus`,
        `resolve_evidence` and `_check_claims_resolve` work untouched. `agency_name` is empty and
        the footer already skips an empty agency line.
        """
        return Persona(
            sender_name=self.full_name,
            sender_role=self.headline,
            agency_name="",
            agency_url=self.links[0] if self.links else None,
            offer=self.target_role,
            proof_points=[ProofPoint(id=fact.id, text=fact.text) for fact in self.facts],
            goal=self.goal,
            cta_text=self.cta_text,
            booking_url=self.booking_url,
            tone=self.tone,
            language=self.language,
            avoid=list(self.avoid),
            signature=self.signature,
            max_words=self.max_words,
            followup_angle=self.followup_angle,
        )

    def missing_for_drafting(self) -> list[tuple[str, str]]:
        """Fields a model-written draft cannot do without, with why each one matters.

        Deliberately **not** `agency_name` or `offer`: requiring an agency from somebody who has
        none is ADR-013's bug, and a field filled in to satisfy a validator is a field the
        drafter will go on to quote.

        A method rather than a `DRAFTING_REQUIREMENTS` tuple like `Persona`'s: on a Pydantic
        model a bare class attribute becomes a *field*, which would serialise into
        `campaigns.persona` and be accepted back from a caller.
        """
        missing: list[tuple[str, str]] = []
        if not self.headline:
            missing.append(
                ("headline", "one line on who you are -- it sits beside your name in the email")
            )
        if not self.target_role:
            missing.append(
                ("target_role", "what you are looking for: every draft is built around it")
            )
        if not self.facts:
            # The important one. With nothing to cite, a model has only invention left -- and
            # the claim check would fail every draft it produced, which reads as the system
            # being broken rather than as the profile being empty.
            missing.append(
                (
                    "facts",
                    "add at least one thing you have actually done: a draft may claim nothing "
                    "that is not here",
                )
            )
        if not self.booking_url and not self.cta_text:
            missing.append(
                ("cta_text", "say what you are asking for -- an email has to end with a question")
            )
        return missing


class _SenderAdapter:
    """A `CandidateProfile` wearing the interface the rest of the system expects.

    `missing_for_drafting()` has to answer for the *profile*, while everything else wants the
    compiled `Persona`. Returning the persona alone would lose the candidate-specific
    requirements and start asking a job seeker to name their agency again.
    """

    def __init__(self, profile: CandidateProfile) -> None:
        self._profile = profile
        self._persona = profile.as_persona()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._persona, name)

    def missing_for_drafting(self) -> list[tuple[str, str]]:
        return self._profile.missing_for_drafting()

    @property
    def persona(self) -> Persona:
        return self._persona


def load_sender(data: dict[str, Any], *, kind: CampaignKind | str) -> Any:
    """Validate `campaigns.persona` according to the campaign's kind.

    An `outreach` campaign gets today's `Persona`, unchanged and unaware any of this exists --
    which is why every row already in the database keeps working.
    """
    resolved = CampaignKind(kind)
    if resolved is CampaignKind.OUTREACH:
        return Persona.model_validate(data)
    return _SenderAdapter(CandidateProfile.model_validate(data))
