"""Reply classification: the labels, the keyword rules, and what each label may cause.

`ARCHITECTURE_TARGET.md` §10.3 and §10.4. Pure logic, no I/O.

The rule that matters most is in the action table: **a label can never cause an email to be
sent.** The model's only powers are to choose a label and propose text for a human. The
prototype let a `positive` label trigger an automatic send, and because the label came from a
substring test, a reply reading *"not positive"* triggered it (issues B4, B17, C5).

Opt-out detection is deliberately belt-and-braces: a keyword rule **and** the model's label,
either one of which suppresses. Over-suppressing costs us a lead who might have replied; under-
suppressing means emailing someone who asked us to stop. Those are not symmetric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class ReplyLabel(StrEnum):
    """The ten labels from §10.3. The prototype had three."""

    INTERESTED = "interested"
    QUESTION = "question"
    OBJECTION = "objection"
    NOT_NOW = "not_now"
    NOT_INTERESTED = "not_interested"
    OPT_OUT = "opt_out"
    OUT_OF_OFFICE = "out_of_office"
    BOUNCE = "bounce"
    WRONG_PERSON_REFERRAL = "wrong_person_referral"
    OTHER = "other"


class ReplyAction(StrEnum):
    """What the system may do about a label. Note what is absent: SEND."""

    #: Suppress the address and close the lead. No reply is sent.
    SUPPRESS = "suppress"
    #: Close the lead, reopenable by a human.
    CLOSE = "close"
    #: Keep waiting, push the follow-up out.
    DEFER = "defer"
    #: Hand to a human, with a proposed reply they must approve.
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class LabelPolicy:
    action: ReplyAction
    #: Whether to draft a proposed reply for the human to approve.
    draft_reply: bool
    #: Whether a person should be told promptly.
    notify: bool


#: The action table. Every label maps to exactly one action, and none of them is "send".
POLICY: dict[ReplyLabel, LabelPolicy] = {
    # Someone wrote back wanting something. A person answers; we propose the words.
    ReplyLabel.INTERESTED: LabelPolicy(ReplyAction.NEEDS_HUMAN, True, True),
    ReplyLabel.QUESTION: LabelPolicy(ReplyAction.NEEDS_HUMAN, True, True),
    ReplyLabel.OBJECTION: LabelPolicy(ReplyAction.NEEDS_HUMAN, True, True),
    ReplyLabel.NOT_NOW: LabelPolicy(ReplyAction.NEEDS_HUMAN, True, True),
    ReplyLabel.WRONG_PERSON_REFERRAL: LabelPolicy(ReplyAction.NEEDS_HUMAN, True, True),
    # A clear no. Closed, and a human can reopen it if they disagree.
    ReplyLabel.NOT_INTERESTED: LabelPolicy(ReplyAction.CLOSE, False, False),
    # Asked to stop. Suppressed immediately, and we send nothing further — not even an
    # acknowledgement, which would itself be another unwanted email.
    ReplyLabel.OPT_OUT: LabelPolicy(ReplyAction.SUPPRESS, False, True),
    ReplyLabel.BOUNCE: LabelPolicy(ReplyAction.SUPPRESS, False, False),
    # A robot. Nobody read the email, so wait rather than treating it as engagement.
    ReplyLabel.OUT_OF_OFFICE: LabelPolicy(ReplyAction.DEFER, False, False),
    # Unclear. A person decides, because guessing is how the prototype went wrong.
    ReplyLabel.OTHER: LabelPolicy(ReplyAction.NEEDS_HUMAN, False, True),
}

#: Phrases that mean "stop emailing me", including the Hinglish forms this audience uses.
#: Matched as whole phrases, so "I will unsubscribe from the newsletter if you keep..." still
#: counts — someone using the word at all is a strong enough signal to stop.
OPT_OUT_PHRASES: tuple[str, ...] = (
    "unsubscribe",
    "remove me",
    "remove my email",
    "take me off",
    "opt out",
    "opt-out",
    "stop emailing",
    "stop contacting",
    "stop sending",
    "do not email",
    "don't email",
    "dont email",
    "do not contact",
    "don't contact",
    "no longer wish",
    "no further emails",
    "delete my data",
    "delete my details",
    "gdpr",
    # Hinglish and Indian-English forms.
    "mat bhejo",
    "mail na kare",
    "mail mat",
    "band karo",
    "please stop",
)

#: A bare "no thanks" is our own suggested wording in the footer, so it is treated as an opt-out
#: when it is essentially the whole message. In a longer reply it usually means "not this", not
#: "never again", and the model's label decides.
SHORT_REFUSALS: tuple[str, ...] = ("no thanks", "no thank you", "not interested", "no")

#: Phrases suggesting the recipient is not the right person.
REFERRAL_PHRASES: tuple[str, ...] = (
    "wrong person",
    "not the right person",
    "i am not responsible",
    "please contact",
    "you should speak to",
    "forward this to",
    "my colleague",
    "speak to my",
    "reach out to",
    "no longer with",
    "has left the company",
    "left the company",
)

_WORD = re.compile(r"[^a-z0-9\s]+")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", _WORD.sub(" ", (text or "").lower())).strip()


def keyword_opt_out(body: str) -> tuple[bool, str]:
    """Does this reply ask us to stop? Returns (yes, the phrase that matched).

    Runs independently of the model. A keyword rule cannot be talked out of its answer by
    text engineered to look like an instruction, which is exactly why both exist (§10.3).
    """
    normalised = _normalise(body)
    if not normalised:
        return False, ""

    for phrase in OPT_OUT_PHRASES:
        if _normalise(phrase) in normalised:
            return True, phrase

    # A very short refusal is almost certainly the footer's own suggested wording coming back.
    words = normalised.split()
    if len(words) <= 4:
        for phrase in SHORT_REFUSALS:
            if normalised == _normalise(phrase):
                return True, phrase

    return False, ""


def looks_like_referral(body: str) -> bool:
    """Whether the reply is pointing us at somebody else.

    Only ever used to *add* a signal for a human to look at. The lead is never re-pointed
    automatically, because extracting a colleague's address from prose and emailing them is a
    decision a person should make.
    """
    normalised = _normalise(body)
    return any(_normalise(phrase) in normalised for phrase in REFERRAL_PHRASES)


def policy_for(label: ReplyLabel) -> LabelPolicy:
    return POLICY[label]


def can_a_label_send(label: ReplyLabel) -> bool:
    """Always False, asserted as code rather than left as a comment.

    The prototype's `positive` label caused an email to be sent with no human involved, and a
    reply reading "not positive" reached it through a substring match. There is no action in the
    table that sends, and this function exists so a test can say so directly.
    """
    return False


def reconcile(
    *, model_label: ReplyLabel, body: str, is_bounce: bool = False, is_auto: bool = False
) -> tuple[ReplyLabel, list[str]]:
    """Combine the header-derived type, the keyword rules and the model's label.

    Precedence, and the reasoning for it:

    1. **A bounce wins.** It comes from headers, which a sender does not usually forge, and it
       is a fact about delivery rather than an opinion about content.
    2. **An auto-reply wins over the model**, for the same reason: nobody read the email.
    3. **A keyword opt-out wins over any other label.** If someone writes "please remove me and
       by the way yes I'm interested", we stop. Honouring the request costs us a lead;
       ignoring it is a compliance failure and plainly rude.
    4. Otherwise the model's label stands.

    Returns the final label and the reasons, which are stored so a decision can be explained
    later.
    """
    reasons: list[str] = []

    if is_bounce:
        return ReplyLabel.BOUNCE, ["delivery status notification in the headers"]

    if is_auto:
        reasons.append("auto-reply headers or wording")
        if model_label is not ReplyLabel.OUT_OF_OFFICE:
            reasons.append(f"model said {model_label.value}, overridden by the headers")
        return ReplyLabel.OUT_OF_OFFICE, reasons

    opted_out, phrase = keyword_opt_out(body)
    if opted_out:
        reasons.append(f"keyword rule matched {phrase!r}")
        if model_label is not ReplyLabel.OPT_OUT:
            reasons.append(
                f"model said {model_label.value}; the keyword rule wins because honouring an "
                "opt-out matters more than keeping a lead"
            )
        return ReplyLabel.OPT_OUT, reasons

    if model_label is ReplyLabel.OPT_OUT:
        reasons.append("model identified an opt-out that no keyword matched")
        return ReplyLabel.OPT_OUT, reasons

    if looks_like_referral(body) and model_label in (
        ReplyLabel.OTHER,
        ReplyLabel.NOT_INTERESTED,
    ):
        reasons.append("referral wording suggests we have the wrong person")
        return ReplyLabel.WRONG_PERSON_REFERRAL, reasons

    reasons.append(f"model label {model_label.value}")
    return model_label, reasons
