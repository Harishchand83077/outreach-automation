"""Unit tests for app.domain.health — campaign health score computation."""

from __future__ import annotations

import pytest

from app.domain.health import HealthScore, compute


class TestInsufficientData:
    def test_zero_sent_is_insufficient(self) -> None:
        score = compute(sent=0, replied=0, meetings=0, bounced=0, opted_out=0)
        assert score.grade == "insufficient"
        assert score.reply_rate is None
        assert score.bounce_rate is None

    def test_below_floor_is_insufficient(self) -> None:
        score = compute(sent=9, replied=2, meetings=0, bounced=0, opted_out=0)
        assert score.grade == "insufficient"


class TestGradeGood:
    def test_high_reply_low_bounce_is_good(self) -> None:
        # 20% reply, 2% bounce
        score = compute(sent=100, replied=20, meetings=2, bounced=2, opted_out=0)
        assert score.grade == "good"
        assert pytest.approx(score.reply_rate, abs=0.001) == 0.20
        assert pytest.approx(score.bounce_rate, abs=0.001) == 0.02

    def test_exactly_at_good_threshold(self) -> None:
        # 15% reply, 3% bounce — boundary values
        score = compute(sent=100, replied=15, meetings=0, bounced=3, opted_out=0)
        assert score.grade == "good"


class TestGradePoor:
    def test_high_bounce_is_poor(self) -> None:
        # 10% bounce rate
        score = compute(sent=100, replied=10, meetings=0, bounced=10, opted_out=0)
        assert score.grade == "poor"

    def test_very_low_reply_is_poor(self) -> None:
        # 1% reply rate
        score = compute(sent=100, replied=1, meetings=0, bounced=1, opted_out=0)
        assert score.grade == "poor"

    def test_zero_reply_is_poor(self) -> None:
        score = compute(sent=50, replied=0, meetings=0, bounced=0, opted_out=0)
        assert score.grade == "poor"


class TestGradeFair:
    def test_moderate_reply_is_fair(self) -> None:
        # 8% reply, 4% bounce
        score = compute(sent=100, replied=8, meetings=1, bounced=4, opted_out=0)
        assert score.grade == "fair"

    def test_good_reply_but_borderline_bounce_is_fair(self) -> None:
        # 20% reply but 5% bounce
        score = compute(sent=100, replied=20, meetings=0, bounced=5, opted_out=0)
        assert score.grade == "fair"


class TestRates:
    def test_rates_are_fractions_not_percentages(self) -> None:
        score = compute(sent=100, replied=10, meetings=0, bounced=5, opted_out=0)
        assert 0.0 < score.reply_rate < 1.0  # type: ignore[operator]
        assert 0.0 < score.bounce_rate < 1.0  # type: ignore[operator]

    def test_positive_rate_includes_meetings(self) -> None:
        score = compute(sent=100, replied=5, meetings=3, bounced=0, opted_out=0)
        assert pytest.approx(score.positive_rate, abs=0.001) == 0.08  # (5+3)/100

    def test_reason_is_a_non_empty_string(self) -> None:
        score = compute(sent=100, replied=10, meetings=0, bounced=2, opted_out=0)
        assert isinstance(score.reason, str)
        assert len(score.reason) > 0

    def test_dataclass_fields(self) -> None:
        score = compute(sent=50, replied=8, meetings=1, bounced=2, opted_out=0)
        assert isinstance(score, HealthScore)
        assert score.grade in {"good", "fair", "poor", "insufficient"}
