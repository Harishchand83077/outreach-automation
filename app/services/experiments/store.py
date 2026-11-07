"""Reading and writing experiments, and the funnel that makes one worth running (ADR-053).

The allocation rule itself is in `app/domain/experiment.py` and has no I/O, so it is tested with
a seeded generator rather than against a database. This module is the part that needs rows: find
the active experiment, assign a lead once, and count outcomes per arm.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC
from random import Random
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, Experiment, Lead, LeadOutcome, Message
from app.db.repo import OrgContext
from app.domain.experiment import Arm, ExperimentSpec, allocate, effective_share
from app.domain.outcome import Outcome

#: How a funnel may be cut. Each is a property of the *send*, not of the lead, which is why
#: weekday and hour are derived from `sent_at` here rather than stored: a denormalised copy of a
#: derived value is one that can disagree with its source (ADR-026).
DIMENSIONS: tuple[str, ...] = ("variant", "weekday", "hour", "warmup_day")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def arms_of(experiment: Experiment) -> list[Arm]:
    """The arms as typed objects.

    No shape tolerance for ADR-026's original `["a", "b"]` declaration, and mypy is the reason
    it is not here: the column is typed `list[dict[str, Any]]`, so an `isinstance(raw, str)`
    branch is unreachable code that reads like a safety net. The table has never had a producer,
    so it has no rows to be tolerant of.
    """
    return [Arm(**raw) for raw in experiment.arms or []]


async def active_for(
    session: AsyncSession, ctx: OrgContext, *, campaign_id: uuid.UUID
) -> Experiment | None:
    """The one active experiment on a campaign, or None.

    One, by `uq_experiments_one_active` rather than by this function picking. A `limit(1)` over
    several would answer differently depending on row order, which is how a result becomes
    untraceable.
    """
    return await session.scalar(
        select(Experiment).where(
            Experiment.org_id == ctx.org_id,
            Experiment.campaign_id == campaign_id,
            Experiment.active.is_(True),
        )
    )


async def assign(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    lead: Lead,
    campaign_id: uuid.UUID,
    rng: Random | None = None,
) -> tuple[str | None, Arm | None]:
    """Put a lead in an arm, once, and return the arm.

    **Idempotent on the lead.** A lead that already has a variant keeps it, whatever the
    experiment now says -- including after an arm is added or the weights change. A regeneration,
    a follow-up and a reply all come back through here and all get the same answer, which is the
    whole point: an A first email and a B follow-up measures neither.

    A lead whose stored variant is no longer an arm of the active experiment keeps it too, and
    gets no instruction: the message already sent carried that label and rewriting history to
    tidy a funnel is worse than a funnel with a retired arm in it.
    """
    experiment = await active_for(session, ctx, campaign_id=campaign_id)
    if experiment is None:
        return lead.variant, None

    arms = arms_of(experiment)
    if not arms:
        # A row that should not exist. Reported by returning nothing rather than raising inside
        # a drafting job: the email is the work, and an experiment is bookkeeping about it.
        return lead.variant, None

    if lead.variant is None:
        # `Random` rather than `secrets`: this picks an experiment arm, not a token. S311 is
        # about credentials, and a cryptographic generator here would buy nothing and make the
        # allocation untestable with a seed.
        lead.variant = allocate(
            arms,
            rng or Random(),  # noqa: S311
            exploration_share=experiment.exploration_share,
        )
        await session.flush()

    return lead.variant, next((arm for arm in arms if arm.variant == lead.variant), None)


@dataclass
class Bucket:
    """One row of a funnel: how many went out, and what came back.

    **`pending` is the load-bearing field.** Most cold email gets no reply, and ADR-026's whole
    dataset design rests on not calling that a negative: a lead still inside its wait window has
    not produced a result yet, and counting it as a failure would make every arm look worse the
    sooner somebody looked. So silence-so-far is `pending`; `censored` is the leads we watched
    until the window closed and nothing happened, which is an absence rather than a rejection;
    and only `encouraging` counts the results a person would be pleased to see.

    **The outcome vocabulary is read from `Outcome`, never restated here.** Writing the strings
    out is this project's most repeated defect: `direction == "outbound"` against a column
    holding `'out'`, `outcome != "ok"` where success is `"success"`,
    `reason="one-click unsubscribe"` against a four-value constraint. Where an enum exists,
    copying its values is the whole bug.
    """

    key: str
    label: str
    sent: int = 0
    pending: int = 0
    censored: int = 0
    encouraging: int = 0
    share: float | None = None
    #: Count per actual `Outcome` value, so a funnel can be read without this module deciding
    #: which results matter. `has_vendor` and `referral` are different news from `not_relevant`
    #: and collapsing them is exactly what ADR-026 refused.
    outcomes: dict[str, int] = field(default_factory=dict)

    def record(self, outcome: Outcome | None) -> None:
        self.sent += 1
        if outcome is None:
            self.pending += 1
            return
        self.outcomes[outcome.value] = self.outcomes.get(outcome.value, 0) + 1
        if outcome.censored:
            self.censored += 1
        elif outcome.is_encouraging:
            self.encouraging += 1

    @property
    def settled(self) -> int:
        """Sends whose result is known one way or the other, the censored ones included.

        A rate over pending leads moves on its own as the day passes, which reads as a trend and
        is just the clock.
        """
        return self.sent - self.pending

    @property
    def encouraging_rate(self) -> float | None:
        return round(self.encouraging / self.settled, 4) if self.settled else None


async def funnel(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    campaign_id: uuid.UUID,
    by: str = "variant",
) -> dict[str, Any]:
    """Outbound messages for a campaign, bucketed, with each lead's recorded outcome.

    Counted from `messages` and `lead_outcomes`, which are the record -- never from a counter
    incremented at send time. A counter drifts: a crash between the send and the increment leaves
    it wrong for ever and nothing notices. The mailbox-health rollup made the same choice for the
    same reason, and a rollup is always a fresh answer.
    """
    if by not in DIMENSIONS:
        raise ValueError(f"a funnel can be cut by {', '.join(DIMENSIONS)}, not {by!r}")

    campaign = await session.get(Campaign, campaign_id)
    if campaign is None or campaign.org_id != ctx.org_id:
        raise ValueError("no such campaign in this workspace")

    zone = _zone_of(campaign)
    rows = (
        await session.execute(
            select(Message, LeadOutcome)
            .join(Lead, Lead.id == Message.lead_id)
            .outerjoin(LeadOutcome, LeadOutcome.lead_id == Message.lead_id)
            .where(
                Message.org_id == ctx.org_id,
                Lead.campaign_id == campaign_id,
                Message.direction == "out",
                # A message still `sending` may yet fail (ADR-002), so it is not a send. The
                # same distinction the platform overview got wrong twice.
                Message.status == "sent",
            )
        )
    ).all()

    buckets: dict[str, Bucket] = {}
    for message, outcome in rows:
        key = _bucket_key(message, by=by, zone=zone)
        if key not in buckets:
            buckets[key] = Bucket(key=key, label=_label(key, by=by))
        buckets[key].record(_outcome_of(outcome))

    experiment = await active_for(session, ctx, campaign_id=campaign_id)
    arms = arms_of(experiment) if experiment is not None else []
    if by == "variant" and experiment is not None:
        for bucket in buckets.values():
            bucket.share = round(
                effective_share(arms, bucket.key, exploration_share=experiment.exploration_share),
                4,
            )

    out = sorted(buckets.values(), key=lambda bucket: bucket.key)

    return {
        "by": by,
        "dimensions": list(DIMENSIONS),
        "experiment": (
            {
                "id": str(experiment.id),
                "name": experiment.name,
                "exploration_share": experiment.exploration_share,
                "arms": [arm.model_dump() for arm in arms],
            }
            if experiment is not None
            else None
        ),
        "buckets": [
            {
                "key": bucket.key,
                "label": bucket.label,
                "sent": bucket.sent,
                "pending": bucket.pending,
                "censored": bucket.censored,
                "encouraging": bucket.encouraging,
                "settled": bucket.settled,
                "encouraging_rate": bucket.encouraging_rate,
                "outcomes": bucket.outcomes,
                "share": bucket.share,
            }
            for bucket in out
        ],
        # Said on the screen rather than implied by a blank column, because an operator reading
        # a 0% reply rate over four settled sends will act on it, and nothing here can stop them
        # except a sentence.
        "note": (
            "Leads still inside their wait window are counted as pending, never as a failure: "
            "silence so far is not a result. Rates are over settled leads only, and a lead "
            "watched to the end of its window with nothing happening is counted as censored "
            "rather than as a rejection."
        ),
    }


def _zone_of(campaign: Campaign) -> Any:
    """The campaign's own timezone, for bucketing a send into somebody's working day.

    A send at 23:40 IST belongs to that operator's Monday, and bucketing by UTC would quietly
    move a sixth of India's working hours into the previous day -- the same mistake the activity
    chart was built to avoid.
    """
    from zoneinfo import ZoneInfo

    name = str((campaign.settings or {}).get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _bucket_key(message: Message, *, by: str, zone: Any) -> str:
    if by == "variant":
        return message.variant or "(no arm)"
    if by == "warmup_day":
        return "(not recorded)" if message.warmup_day is None else f"day {message.warmup_day:02d}"
    # `created_at` is not nullable, so there is always a moment to bucket by. mypy pointed out
    # that the None branch I wrote here could never run.
    moment = message.sent_at or message.created_at
    local = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(zone)
    return _WEEKDAYS[local.weekday()] if by == "weekday" else f"{local.hour:02d}:00"


def _label(key: str, *, by: str) -> str:
    if by == "variant":
        return f"Arm {key}" if key != "(no arm)" else "No arm"
    return key


def _outcome_of(row: LeadOutcome | None) -> Outcome | None:
    """The row's outcome as the enum, or None while it is still being watched.

    A stored value the enum does not know is treated as still-watching rather than guessed at:
    it can only mean this code is older than the row, and inventing a bucket for it would put a
    number on a screen that nothing can explain.
    """
    if row is None or row.outcome is None:
        return None
    try:
        return Outcome(row.outcome)
    except ValueError:
        return None


def spec_of(experiment: Experiment) -> ExperimentSpec:
    """The experiment as a validated spec, for echoing back to a form."""
    return ExperimentSpec(name=experiment.name, arms=arms_of(experiment))
