"""The campaign persona: who "we" are, and the only facts a draft may assert about us.

This is the structural fix for two of the worst prototype defects. The drafting prompt asked the
model for "our product/service and traction" while supplying no facts about the sender, so the
model invented the product and the traction and those claims were emailed to real people (issue
A3). And the whole thing was hardcoded to funding and venture capital — "startup and VC analyst",
"Series A/B/C" — so it was not a general outreach tool at all (issue A4).

A persona belongs to a campaign, which makes the tool general purpose: a campaign carries its own
sender identity, offer and goal.

`proof_points` carries stable ids (`pp1`, `pp2`, …). The drafter may state a concrete claim about
us **only** if it maps to one of those ids or to cited research evidence, and the validator
rejects drafts that do not (§13). That is what makes "do not invent facts about the sender"
enforceable rather than a hope.

Pure data: no I/O, so it is validated identically whether it arrives from the API, a test or a
database row.
"""

from __future__ import annotations

import re
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

#: Default word ceiling for a draft body. Short cold email outperforms long cold email, and a
#: tight budget also limits how much an unsupervised model can invent.
DEFAULT_MAX_WORDS = 120

#: Phrases that reliably hurt deliverability or read as a template. The validator rejects a draft
#: containing any of these; a campaign can add more via `avoid`.
SPAM_TRIGGERS: frozenset[str] = frozenset(
    {
        "act now",
        "click here",
        "dear sir/madam",
        "guaranteed",
        "limited time offer",
        "no obligation",
        "risk free",
        "100% free",
        "make money fast",
        "once in a lifetime",
    }
)

_PROOF_POINT_ID = re.compile(r"^pp\d+$")

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=500)]


class ProofPoint(BaseModel):
    """One verifiable thing we can say about ourselves.

    Stable `id` matters: a draft cites it, the validator resolves it, and the citation is stored
    on the message. Reordering the list must not change what a past draft was claiming.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^pp\d+$", description="Stable citation id, e.g. pp1")
    text: NonEmptyStr

    @field_validator("text")
    @classmethod
    def _no_control_characters(cls, v: str) -> str:
        if any(ord(ch) < 32 and ch not in "\t\n" for ch in v):
            raise ValueError("proof point text must not contain control characters")
        return v.strip()


class Persona(BaseModel):
    """The sender identity and offer for one campaign."""

    model_config = ConfigDict(extra="forbid")

    # --- Who is writing ---
    sender_name: NonEmptyStr
    sender_role: NonEmptyStr
    agency_name: NonEmptyStr
    agency_url: HttpUrl | None = None

    # --- What we do ---
    services: list[NonEmptyStr] = Field(default_factory=list, max_length=20)
    ideal_customer: str = Field(default="", max_length=1000)
    offer: NonEmptyStr

    #: The only claims about ourselves a draft may assert. Everything else must come from cited
    #: research evidence about the recipient.
    proof_points: list[ProofPoint] = Field(default_factory=list, max_length=20)

    # --- What we want ---
    goal: NonEmptyStr = Field(description='e.g. "book a 20-minute call"')
    cta_text: str = Field(default="", max_length=300)
    booking_url: HttpUrl | None = None

    # --- How it should read ---
    tone: str = Field(default="direct and warm", max_length=200)
    language: str = Field(default="en", max_length=20)
    #: Words and phrases this campaign must never use, on top of SPAM_TRIGGERS.
    avoid: list[str] = Field(default_factory=list, max_length=100)
    signature: str = Field(default="", max_length=1000)
    max_words: int = Field(default=DEFAULT_MAX_WORDS, ge=40, le=400)
    followup_angle: str = Field(default="", max_length=1000)

    #: Optional, but recommended: a postal address materially helps both credibility and
    #: CAN-SPAM posture. See QUESTIONS.md Q2.
    postal_address: str = Field(default="", max_length=500)

    @field_validator("avoid")
    @classmethod
    def _normalise_avoid(cls, v: list[str]) -> list[str]:
        """Lower-cased and de-duplicated, because matching is case-insensitive."""
        seen: list[str] = []
        for phrase in v:
            cleaned = phrase.strip().lower()
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen

    @model_validator(mode="after")
    def _proof_point_ids_are_unique(self) -> Self:
        ids = [point.id for point in self.proof_points]
        if len(ids) != len(set(ids)):
            raise ValueError("proof_point ids must be unique — a draft cites them by id")
        return self

    @model_validator(mode="after")
    def _a_call_to_action_needs_somewhere_to_go(self) -> Self:
        """A goal of booking a call is unreachable without a link or an explicit ask."""
        if not self.booking_url and not self.cta_text:
            raise ValueError(
                "set booking_url or cta_text: a draft needs a concrete next step to ask for"
            )
        return self

    @property
    def forbidden_phrases(self) -> frozenset[str]:
        """Everything a draft must not contain, for the deterministic validator."""
        return SPAM_TRIGGERS | frozenset(self.avoid)

    def proof_point(self, point_id: str) -> ProofPoint | None:
        """Resolve a citation. Returns None if the draft cited something that does not exist."""
        return next((p for p in self.proof_points if p.id == point_id), None)

    def next_proof_point_id(self) -> str:
        """The next free `ppN`, so adding a point never reuses a retired id."""
        used = {int(p.id[2:]) for p in self.proof_points if _PROOF_POINT_ID.match(p.id)}
        return f"pp{max(used) + 1 if used else 1}"
