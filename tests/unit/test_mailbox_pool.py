"""Weighted selection for a campaign's mailbox pool (F7, ADR-044).

`choose` is pure, which is the reason it exists as its own function: the rotation is the part
somebody will want to reason about from a funnel report months later, and a rule that needs a
database to evaluate is a rule nobody re-derives.
"""

from __future__ import annotations

import uuid

from app.services.mailboxes.pool import PoolMember, choose

A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
C = uuid.UUID("00000000-0000-4000-8000-00000000000c")


def _pool(*pairs: tuple[uuid.UUID, int]) -> list[PoolMember]:
    return [PoolMember(mailbox_id=one, weight=weight) for one, weight in pairs]


class TestTheOrdinaryCase:
    def test_one_mailbox_always_wins(self) -> None:
        """Every campaign until somebody adds a second mailbox. The pre-F7 behaviour, which the
        migration had to leave untouched."""
        assert choose(_pool((A, 1)), counts={A: 97}, usable={A}) == A

    def test_the_emptier_mailbox_goes_next(self) -> None:
        assert choose(_pool((A, 1), (B, 1)), counts={A: 5, B: 2}, usable={A, B}) == B

    def test_an_untouched_mailbox_goes_before_a_used_one(self) -> None:
        assert choose(_pool((A, 1), (B, 1)), counts={A: 1}, usable={A, B}) == B


class TestTheWeights:
    def test_a_heavier_mailbox_takes_proportionally_more(self) -> None:
        """The property, run out over a day's sends rather than hand-traced for two.

        A traced sequence is where I got this wrong first: weight 3 against weight 1 does **not**
        mean three in a row, it means the loads stay in proportion -- so after one send from the
        heavy mailbox its load is 1/3 and the light one's is 0, and the light one goes next. The
        share is what the weights promise, and the share is what this asserts.
        """
        pool = _pool((A, 3), (B, 1))
        counts = {A: 0, B: 0}

        for _ in range(80):
            counts[choose(pool, counts=counts, usable={A, B})] += 1  # type: ignore[index]

        assert counts[A] + counts[B] == 80
        assert abs(counts[A] / counts[B] - 3) < 0.2, counts

    def test_a_warming_mailbox_can_be_given_a_small_share(self) -> None:
        """The case the weights exist for: a new address beside an established one, taking a
        tenth of the work while its reputation builds."""
        pool = _pool((A, 10), (B, 1))
        counts = {A: 0, B: 0}

        for _ in range(110):
            counts[choose(pool, counts=counts, usable={A, B})] += 1  # type: ignore[index]

        assert counts[B] == 10, counts
        assert counts[A] == 100

    def test_the_answer_does_not_depend_on_who_asks(self) -> None:
        """A tie broken at random would hand the same unassigned lead to two mailboxes in one
        pass, which the claim would then have to refuse -- a refusal nobody needed."""
        pool = _pool((A, 1), (B, 1), (C, 1))
        counts = {A: 4, B: 4, C: 4}

        answers = {choose(pool, counts=counts, usable={A, B, C}) for _ in range(20)}

        assert answers == {A}


class TestWhenAMemberCannotSend:
    def test_an_unusable_member_is_skipped_rather_than_stopping_the_pool(self) -> None:
        """Degrading to a smaller pool is the whole point of having one. Stopping because a
        member needs reconnecting would make a pool worse than no pool."""
        assert choose(_pool((A, 1), (B, 1)), counts={A: 50, B: 0}, usable={B}) == B

    def test_no_usable_member_is_none_rather_than_a_guess(self) -> None:
        assert choose(_pool((A, 1), (B, 1)), counts={}, usable=set()) is None

    def test_an_empty_pool_is_none(self) -> None:
        assert choose([], counts={}, usable={A}) is None

    def test_a_weight_of_zero_cannot_divide_by_zero(self) -> None:
        """The check constraint makes this unreachable from the API. It is defended anyway,
        because the one place a bad weight could arrive is a hand-written UPDATE -- which is
        exactly how `send_days` once arrived as a list of names and stopped every mailbox."""
        assert choose([PoolMember(mailbox_id=A, weight=0)], counts={A: 3}, usable={A}) == A
