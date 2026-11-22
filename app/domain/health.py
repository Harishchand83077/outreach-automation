"""Campaign health score — pure module, no I/O.

Grades a campaign from its aggregate send/reply/bounce counts. The floor of
ten sent emails prevents a single bounce from labelling a fresh campaign "poor"
before it has a sample to judge from.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Minimum emails sent before any rate is reported. Below this every rate is
#: misleading — one bounce in five is 20%, which looks catastrophic but proves
#: nothing.
_MIN_SAMPLE = 10


@dataclass(frozen=True)
class HealthScore:
    grade: str  # "good" | "fair" | "poor" | "insufficient"
    reply_rate: float | None
    positive_rate: float | None
    bounce_rate: float | None
    reason: str


def compute(
    *,
    sent: int,
    replied: int,
    meetings: int,
    bounced: int,
    opted_out: int,
) -> HealthScore:
    """Score a campaign from its lifetime aggregate counts.

    Grades:
    - ``good``  — reply ≥ 15 % *and* bounce ≤ 3 %
    - ``poor``  — bounce ≥ 10 % *or* reply < 2 %
    - ``fair``  — everything else with enough data
    - ``insufficient`` — fewer than ``_MIN_SAMPLE`` emails sent
    """
    if sent < _MIN_SAMPLE:
        return HealthScore(
            grade="insufficient",
            reply_rate=None,
            positive_rate=None,
            bounce_rate=None,
            reason=f"Need at least {_MIN_SAMPLE} emails sent to score this campaign.",
        )

    reply_rate = replied / sent
    positive_rate = (replied + meetings) / sent
    bounce_rate = bounced / sent

    if bounce_rate >= 0.10:
        grade = "poor"
        reason = f"Bounce rate {bounce_rate:.0%} — check your list quality and sending domain."
    elif reply_rate < 0.02:
        grade = "poor"
        reason = f"Reply rate {reply_rate:.0%} — subject lines or targeting may need work."
    elif reply_rate >= 0.15 and bounce_rate <= 0.03:
        grade = "good"
        reason = f"Reply rate {reply_rate:.0%}, bounce rate {bounce_rate:.0%}."
    else:
        grade = "fair"
        reason = f"Reply rate {reply_rate:.0%}, bounce rate {bounce_rate:.0%}."

    return HealthScore(
        grade=grade,
        reply_rate=reply_rate,
        positive_rate=positive_rate,
        bounce_rate=bounce_rate,
        reason=reason,
    )
