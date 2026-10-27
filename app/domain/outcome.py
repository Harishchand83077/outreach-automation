"""What happened to a lead, as distinct from what any one reply said.

`docs/DATASET_DESIGN.md` is the reasoning; this is the part of it that has no I/O, so the two
rules the dataset rests on can be unit-tested against a clock and nothing else.

**A reply label and an outcome are different things, and conflating them would be the defect.**
`classifications.label` records what *one message* said, is written before anything acts on it,
and drives suppressions. An outcome is the lead's standing result: slower, revisable, and
sometimes set by no message at all — silence past a wait window is an outcome, and a meeting
booked is an outcome that no reply can produce.

**Silence is censored, not negative.** A lead that has not replied carries no outcome at all until
the window passes, and then carries one that is explicitly marked censored. Recording silence as
rejection would teach anything reading this that every email without a prompt answer was a bad
email — when the commonest reason for silence is that somebody was busy on Tuesday. The failure
would be invisible, because the dataset would look full.

This module has no database imports on purpose: it is the vocabulary, and both the worker and the
export read the same copy of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from app.domain.reply import ReplyLabel


class Outcome(StrEnum):
    """The standing result for one lead.

    Deliberately not a positive/negative axis. "We already have an agency", "not this quarter"
    and "wrong person, talk to Priya" are three different results and two of them are good news;
    a single axis would throw away exactly the distinctions worth learning from.
    """

    POSITIVE_INTEREST = "positive_interest"
    #: Set **only** by a human click or a booking webhook — never inferred from a warm reply.
    #: A locked product rule since Phase 1, and a test asserts no label can reach it.
    MEETING_BOOKED = "meeting_booked"
    #: Not them, but they pointed somewhere. The highest-converting thing cold outreach produces,
    #: and the most expensive to misfile as a rejection.
    REFERRAL = "referral"
    NOT_NOW = "not_now"
    NOT_RELEVANT = "not_relevant"
    #: They have the budget and the problem; somebody else is serving it. A displacement and
    #: timing signal, which folding into `not_relevant` would delete.
    HAS_VENDOR = "has_vendor"
    OBJECTION = "objection"
    AUTO_REPLY = "auto_reply"
    UNSUBSCRIBE = "unsubscribe"
    HOSTILE = "hostile"
    #: Deliverability, not an opinion: it says nothing about the email's content.
    BOUNCE = "bounce"
    #: We watched until `observed_until` and nothing happened. **Censored, not negative.**
    NO_REPLY_AFTER_WINDOW = "no_reply_after_window"

    @property
    def censored(self) -> bool:
        """True when this row records an absence rather than an event.

        Survival analysis' term, and the right one: the subject was observed for a period and the
        event of interest had not occurred by the end of it. It may still occur.
        """
        return self is Outcome.NO_REPLY_AFTER_WINDOW

    @property
    def is_encouraging(self) -> bool:
        """Whether a human would be pleased to see this, for funnel display only.

        **Not a training label.** It exists so a dashboard can colour a row, and it is a property
        of the outcome rather than a second taxonomy. Anything learning from this data should read
        the outcome itself, which carries far more than one bit.
        """
        return self in {
            Outcome.POSITIVE_INTEREST,
            Outcome.MEETING_BOOKED,
            Outcome.REFERRAL,
            Outcome.NOT_NOW,
        }


class Stage(StrEnum):
    """How far down the funnel a lead got. Shared vocabulary, per-kind sequences."""

    SENT = "sent"
    REPLIED = "replied"
    MEETING = "meeting"
    PROPOSAL = "proposal"
    WON = "won"
    SCREEN = "screen"
    INTERVIEW = "interview"
    OFFER = "offer"


#: The ordered stages for each campaign kind. One column, validated per kind, so a cross-kind
#: query still works and a per-kind funnel is one `WHERE`.
FUNNEL_STAGES: dict[str, tuple[Stage, ...]] = {
    "outreach": (Stage.SENT, Stage.REPLIED, Stage.MEETING, Stage.PROPOSAL, Stage.WON),
    "job_hunt": (Stage.SENT, Stage.REPLIED, Stage.SCREEN, Stage.INTERVIEW, Stage.OFFER),
}


def stages_for_kind(kind: str) -> tuple[Stage, ...]:
    """The funnel for this kind, falling back to `outreach`.

    Falling back rather than raising: a kind added later must not make an existing export blow up
    halfway through writing a file.
    """
    return FUNNEL_STAGES.get(kind, FUNNEL_STAGES["outreach"])


#: Reply label to outcome. Not every label maps: `question` and `other` mean a person has to
#: decide, and inventing an outcome for them would manufacture training labels out of ambiguity.
_FROM_LABEL: dict[ReplyLabel, Outcome] = {
    ReplyLabel.INTERESTED: Outcome.POSITIVE_INTEREST,
    ReplyLabel.WRONG_PERSON_REFERRAL: Outcome.REFERRAL,
    ReplyLabel.NOT_NOW: Outcome.NOT_NOW,
    ReplyLabel.NOT_INTERESTED: Outcome.NOT_RELEVANT,
    ReplyLabel.HAS_VENDOR: Outcome.HAS_VENDOR,
    ReplyLabel.OBJECTION: Outcome.OBJECTION,
    ReplyLabel.OPT_OUT: Outcome.UNSUBSCRIBE,
    ReplyLabel.OUT_OF_OFFICE: Outcome.AUTO_REPLY,
    ReplyLabel.HOSTILE: Outcome.HOSTILE,
    ReplyLabel.BOUNCE: Outcome.BOUNCE,
}


def outcome_for_label(label: ReplyLabel) -> Outcome | None:
    """The outcome this reply implies, or None when it implies none.

    `MEETING_BOOKED` is absent from the table by construction, not by omission.
    """
    return _FROM_LABEL.get(label)


def resolve_silence(
    *, first_sent_at: datetime | None, wait_days: int, now: datetime
) -> Outcome | None:
    """`NO_REPLY_AFTER_WINDOW` once the wait has elapsed, else None.

    None means "still watching", and that is a different state from every outcome in the enum —
    which is why the column is nullable rather than carrying a `pending` member. A `pending`
    value would end up in exports and be counted as a class.

    A lead that has never been sent to has no clock and therefore no silence to resolve.
    """
    if first_sent_at is None:
        return None
    if now - first_sent_at < timedelta(days=wait_days):
        return None
    return Outcome.NO_REPLY_AFTER_WINDOW


#: Sub-types for `OBJECTION`. A sub-type rather than separate labels because separate labels
#: would have changed `POLICY`, which decides suppressions; this cannot.
class ObjectionKind(StrEnum):
    PRICE = "price"
    TIMING = "timing"
    TRUST = "trust"
    FIT = "fit"
    OTHER = "other"
