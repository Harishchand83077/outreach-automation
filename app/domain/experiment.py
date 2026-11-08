"""Which arm of an experiment a lead goes into (F13, ADR-053). No I/O (§4).

**Random, not round-robin**, and that is the whole reason this module is pure and takes an
injected `Random`. Round-robin looks fairer and correlates the arm with arrival order: a sheet
sorted by company size, or an import done in two halves on two days, puts systematically
different people in each arm, and the difference then shows up in the results as though the
*email* caused it. Randomisation is what makes the comparison mean anything, so it is tested
with a seeded generator rather than hoped for.

**An arm is sticky per lead** (`leads.variant`), for the same reason the sending mailbox is
(ADR-044): a first email from one arm and a follow-up from another measures neither, and the
recipient is the one person who can tell.

**A fixed exploration share is held back, always.** Weights exist so an operator can send most
of the volume through the arm they believe in -- and a weighted allocator with nothing held back
can drive an arm to zero, which is exactly when it stops producing the data that would show
the belief was wrong. So `EXPLORATION_SHARE` of assignments ignore the weights entirely and pick
uniformly, which floors every arm at `EXPLORATION_SHARE / len(arms)` however the weights are
set. A floor, not a correction: nothing here learns from outcomes, and F16 is the slice that
would.
"""

from __future__ import annotations

from random import Random
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The share of assignments that ignore the weights and pick uniformly. One number, in code,
#: not per experiment: an operator who can set it to zero has a weighted allocator that can
#: starve an arm, and the arm that gets starved is the one whose result would have surprised
#: them. 0.2 is enough that a 1:99 split still puts a tenth of its sends in the quiet arm.
EXPLORATION_SHARE = 0.2

MAX_ARMS = 4
MIN_WEIGHT = 1
MAX_WEIGHT = 100


class Arm(BaseModel):
    """One arm: a label, how much of the non-exploration volume it takes, and what it changes.

    `instruction` is a line handed to the drafter for this arm, and it is the only thing an arm
    may vary. It cannot introduce a claim: the draft it produces goes through the same
    `validate_draft` and the same claim check as every other draft, so an arm asking for a
    statistic it cannot support fails validation rather than sending one. That is hard rule 6
    holding whatever an operator types here.
    """

    model_config = ConfigDict(extra="forbid")

    #: Short and stable, because it is stored on every message this arm ever sends and a
    #: renamed arm would split its own results in two.
    variant: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    weight: int = Field(default=1, ge=MIN_WEIGHT, le=MAX_WEIGHT)
    instruction: str = Field(default="", max_length=400)


class ExperimentSpec(BaseModel):
    """An experiment as an operator describes it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    arms: list[Arm] = Field(min_length=2, max_length=MAX_ARMS)

    @model_validator(mode="after")
    def _distinct_variants(self) -> Self:
        """Two arms with one label are one arm with twice the volume and no way to tell them
        apart afterwards, which is worse than a rejected form."""
        labels = [arm.variant for arm in self.arms]
        if len(set(labels)) != len(labels):
            raise ValueError("each arm needs its own variant label")
        return self


def allocate(arms: list[Arm], rng: Random, *, exploration_share: float = EXPLORATION_SHARE) -> str:
    """Pick an arm. Random, with a fixed share that ignores the weights.

    Raises on an empty list rather than returning None: a campaign with a running experiment and
    no arms is a row that should not exist, and a silent None would be recorded as "no
    experiment" on every message -- indistinguishable from a campaign that never had one.
    """
    if not arms:
        raise ValueError("an experiment with no arms cannot allocate")
    if len(arms) == 1:
        return arms[0].variant

    if rng.random() < exploration_share:
        # The floor. Uniform over the arms, weights deliberately not consulted.
        return rng.choice(arms).variant

    total = sum(arm.weight for arm in arms)
    draw = rng.uniform(0, total)
    running = 0.0
    for arm in arms:
        running += arm.weight
        if draw <= running:
            return arm.variant
    # Float arithmetic can leave `draw` a hair past the total. The last arm is the answer; a
    # fall-through returning None would be a NULL variant on a real send.
    return arms[-1].variant


def effective_share(
    arms: list[Arm], variant: str, *, exploration_share: float = EXPLORATION_SHARE
) -> float:
    """What share of sends an arm actually gets, exploration included.

    Reported on the screen, because the number an operator sets is not the number they get and
    being surprised by that is how somebody concludes the allocator is broken.
    """
    if not arms:
        return 0.0
    arm = next((one for one in arms if one.variant == variant), None)
    if arm is None:
        return 0.0
    total = sum(one.weight for one in arms)
    weighted = arm.weight / total if total else 0.0
    return exploration_share / len(arms) + (1 - exploration_share) * weighted
