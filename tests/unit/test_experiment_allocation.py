"""Allocation is random, and every arm has a floor under it (F13, ADR-053).

`app/domain/experiment.py` has no I/O and takes an injected `Random`, so the distribution is
asserted with a seed rather than hoped for. The properties here are the ones that make a result
mean anything: that arrival order cannot leak into the arm, and that a weight cannot starve an
arm to zero.
"""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from random import Random

import pytest
from pydantic import ValidationError

from app.db.models import MIN_EXPLORATION_SHARE
from app.domain.experiment import (
    EXPLORATION_SHARE,
    Arm,
    ExperimentSpec,
    allocate,
    effective_share,
)


def _spread(arms: list[Arm], n: int = 20_000, seed: int = 7) -> Counter[str]:
    rng = Random(seed)
    return Counter(allocate(arms, rng) for _ in range(n))


class TestItIsRandom:
    def test_an_even_split_is_even(self) -> None:
        counts = _spread([Arm(variant="a"), Arm(variant="b")])
        assert abs(counts["a"] - counts["b"]) < 1_000

    def test_the_arm_does_not_follow_arrival_order(self) -> None:
        """The reason this is random rather than round-robin. Round-robin looks fairer and
        correlates the arm with arrival order -- a sheet sorted by company size, or an import
        done in two halves on two days, puts systematically different people in each arm, and
        the difference then shows up as though the *email* caused it.

        Asserted as the absence of alternation: round-robin would produce zero repeats.
        """
        rng = Random(11)
        arms = [Arm(variant="a"), Arm(variant="b")]
        series = [allocate(arms, rng) for _ in range(500)]
        repeats = sum(1 for first, second in pairwise(series) if first == second)
        assert repeats > 150

    def test_one_arm_is_not_a_coin_flip(self) -> None:
        assert allocate([Arm(variant="only")], Random(1)) == "only"

    def test_no_arms_raises_rather_than_returning_none(self) -> None:
        """A None would be stored as "no experiment" on every message, which is
        indistinguishable from a campaign that never had one."""
        with pytest.raises(ValueError, match="no arms"):
            allocate([], Random(1))


class TestTheFloor:
    def test_a_lopsided_weighting_still_reaches_the_quiet_arm(self) -> None:
        """The slice's reason for existing. 1 against 99 with nothing held back puts about one
        send in a hundred through the quiet arm, which at this project's volumes is never -- and
        the quiet arm is the one whose result would have surprised somebody."""
        counts = _spread([Arm(variant="loud", weight=99), Arm(variant="quiet", weight=1)])
        assert counts["quiet"] / 20_000 > 0.09

    def test_the_floor_is_exploration_over_the_number_of_arms(self) -> None:
        arms = [Arm(variant="a", weight=100), Arm(variant="b", weight=1)]
        assert effective_share(arms, "b") > EXPLORATION_SHARE / len(arms)

    def test_the_weights_still_decide_the_rest(self) -> None:
        counts = _spread([Arm(variant="a", weight=3), Arm(variant="b", weight=1)])
        assert counts["a"] > counts["b"]

    def test_shares_sum_to_one(self) -> None:
        arms = [Arm(variant="a", weight=3), Arm(variant="b", weight=1), Arm(variant="c")]
        assert abs(sum(effective_share(arms, arm.variant) for arm in arms) - 1.0) < 1e-9

    def test_the_default_share_is_at_or_above_the_databases_floor(self) -> None:
        """The constant in `domain` and the check constraint in `models` have to agree, and the
        constraint is built from `MIN_EXPLORATION_SHARE`. A default below the floor would be a
        form whose own default the database refuses."""
        assert EXPLORATION_SHARE >= MIN_EXPLORATION_SHARE

    def test_a_zero_share_is_expressible_only_as_an_argument_never_as_a_default(self) -> None:
        """`allocate` honours a zero when a test passes one, because the function has to be
        testable without the floor. What stops an operator reaching it is the route and the
        check constraint, not this function."""
        counts = _spread_with_share([Arm(variant="a", weight=1), Arm(variant="b", weight=99)], 0.0)
        assert counts["a"] / 20_000 < 0.05


def _spread_with_share(arms: list[Arm], share: float, n: int = 20_000) -> Counter[str]:
    rng = Random(3)
    return Counter(allocate(arms, rng, exploration_share=share) for _ in range(n))


class TestTheSpec:
    def test_two_arms_are_the_minimum(self) -> None:
        with pytest.raises(ValidationError):
            ExperimentSpec(name="one arm", arms=[Arm(variant="a")])

    def test_two_arms_with_one_label_are_refused(self) -> None:
        """One arm with twice the volume and no way to tell them apart afterwards, which is
        worse than a rejected form."""
        with pytest.raises(ValidationError, match="own variant label"):
            ExperimentSpec(name="dupe", arms=[Arm(variant="a"), Arm(variant="a")])

    def test_a_label_is_lowercase_and_stable(self) -> None:
        """Stored on every message this arm sends: a renamed arm splits its own results in two,
        so the pattern keeps them to something nobody is tempted to retype."""
        with pytest.raises(ValidationError):
            Arm(variant="Arm One")

    def test_an_arm_may_not_carry_an_unknown_field(self) -> None:
        """`extra="forbid"`, so a typo in a stored row is a refusal rather than a silently
        ignored instruction."""
        with pytest.raises(ValidationError):
            Arm(variant="a", instrcution="be brief")  # type: ignore[call-arg]
