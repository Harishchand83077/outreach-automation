"""Scheduling maths, including the property-based tests §22 asks for.

Window edges, weekends and the warm-up ramp are where hand-picked examples miss cases, so the
invariants are asserted over generated inputs as well as the specific table from §9.4.

The clock is always passed in. Nothing here reads the wall clock, so a test run at 3am in a
different timezone gives the same answer.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import assume, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from app.domain.campaign import CampaignSettings
from app.domain.scheduling import (
    MAX_GAP,
    MIN_GAP,
    business_days_between,
    followup_due_at,
    in_window,
    is_followup_overdue,
    ist_day_bounds,
    next_window_open,
    no_response_at,
    pacing_gap,
    remaining_capacity,
    warmup_cap,
    window_close,
)

IST = ZoneInfo("Asia/Kolkata")
SETTINGS = CampaignSettings()


def ist(year, month, day, hour=0, minute=0) -> datetime:
    """A campaign-local instant, returned as UTC — which is how the system stores time."""
    return datetime(year, month, day, hour, minute, tzinfo=IST).astimezone(UTC)


class TestTheWindow:
    """Mon-Fri, 10:00-17:30 Asia/Kolkata."""

    @pytest.mark.parametrize(
        "when,expected",
        [
            (ist(2026, 10, 12, 10, 0), True),  # Monday, window opens
            (ist(2026, 10, 12, 13, 0), True),  # Monday midday
            (ist(2026, 10, 12, 17, 29), True),  # Monday, last minute
            (ist(2026, 10, 12, 17, 30), False),  # exactly at close — exclusive
            (ist(2026, 10, 12, 9, 59), False),  # a minute early
            (ist(2026, 10, 12, 21, 0), False),  # Monday evening
            (ist(2026, 10, 10, 13, 0), False),  # Saturday
            (ist(2026, 10, 11, 13, 0), False),  # Sunday
        ],
    )
    def test_the_boundaries(self, when, expected):
        assert in_window(when, SETTINGS) is expected

    def test_the_window_is_evaluated_in_ist_not_utc(self):
        """11:00 IST on a Monday is 05:30 UTC — outside a naive UTC 10-17:30 window. Getting
        this wrong would send at 3pm IST only, or not at all."""
        when = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)
        assert in_window(when, SETTINGS) is True

    def test_a_naive_datetime_is_treated_as_utc(self):
        assert in_window(datetime(2026, 10, 12, 5, 30), SETTINGS) is True  # noqa: DTZ001

    def test_a_holiday_is_outside_the_window(self):
        diwali = date(2026, 11, 8)
        s = CampaignSettings(holidays=[diwali])
        assert in_window(ist(2026, 11, 8, 13, 0), s) is False

    def test_window_close_is_in_utc(self):
        closes = window_close(ist(2026, 10, 12, 11, 0), SETTINGS)
        assert closes.astimezone(IST).time() == time(17, 30)


class TestNextWindowOpen:
    def test_before_the_window_waits_until_it_opens_today(self):
        result = next_window_open(ist(2026, 10, 12, 7, 0), SETTINGS)
        assert result.astimezone(IST).hour == 10
        assert result.astimezone(IST).date() == date(2026, 10, 12)

    def test_inside_the_window_is_now(self):
        now = ist(2026, 10, 12, 11, 0)
        assert next_window_open(now, SETTINGS) == now

    def test_after_the_window_waits_for_tomorrow(self):
        result = next_window_open(ist(2026, 10, 12, 20, 0), SETTINGS)
        assert result.astimezone(IST).date() == date(2026, 10, 13)

    def test_friday_evening_waits_for_monday(self):
        """The weekend rule, where it actually bites."""
        result = next_window_open(ist(2026, 10, 9, 20, 0), SETTINGS)
        local = result.astimezone(IST)
        assert local.date() == date(2026, 10, 12)
        assert local.weekday() == 0
        assert local.time() == time(10, 0)

    def test_saturday_waits_for_monday(self):
        result = next_window_open(ist(2026, 10, 10, 11, 0), SETTINGS)
        assert result.astimezone(IST).date() == date(2026, 10, 12)

    def test_a_holiday_on_monday_pushes_to_tuesday(self):
        s = CampaignSettings(holidays=[date(2026, 10, 12)])
        result = next_window_open(ist(2026, 10, 9, 20, 0), s)
        assert result.astimezone(IST).date() == date(2026, 10, 13)

    def test_a_long_holiday_block_is_searched_past_rather_than_looping(self):
        """100 consecutive holidays is the most `settings` allows, so the search must get past
        them rather than give up. The year limit inside the loop is a guard against a
        misconfiguration that the schema makes unreachable, not an expected path."""
        blocked = [date(2026, 10, 12) + timedelta(days=n) for n in range(100)]
        s = CampaignSettings(holidays=blocked)

        opened = next_window_open(ist(2026, 10, 12, 20, 0), s)
        assert in_window(opened, s)
        assert opened.astimezone(IST).date() > blocked[-1]


class TestFollowupTiming:
    """§9.4's table is the specification."""

    @pytest.mark.parametrize(
        "sent_day,expected_weekday,note",
        [
            (date(2026, 10, 12), 3, "Mon -> Thu"),
            (date(2026, 10, 13), 4, "Tue -> Fri"),
            (date(2026, 10, 14), 0, "Wed -> Mon, Saturday rolls on"),
            (date(2026, 10, 15), 0, "Thu -> Mon, Sunday rolls on"),
            (date(2026, 10, 16), 0, "Fri -> Mon"),
        ],
    )
    def test_the_specified_table(self, sent_day, expected_weekday, note):
        sent = datetime.combine(sent_day, time(11, 0), tzinfo=IST).astimezone(UTC)
        due = followup_due_at(first_sent_at=sent, settings=SETTINGS)
        assert due.astimezone(IST).weekday() == expected_weekday, note

    def test_a_followup_always_lands_inside_the_window(self):
        for day in range(1, 29):
            sent = datetime(2026, 10, day, 16, 0, tzinfo=IST).astimezone(UTC)
            due = followup_due_at(first_sent_at=sent, settings=SETTINGS)
            assert in_window(due, SETTINGS), f"sent on the {day}th landed outside the window"

    def test_the_time_of_day_is_roughly_preserved(self):
        """A follow-up at a similar hour reads better than one at 10:00:00 sharp every time."""
        sent = datetime(2026, 10, 12, 15, 30, tzinfo=IST).astimezone(UTC)
        due = followup_due_at(first_sent_at=sent, settings=SETTINGS)
        assert due.astimezone(IST).time() == time(15, 30)

    def test_an_out_of_hours_send_rolls_to_the_window_opening(self):
        sent = datetime(2026, 10, 12, 23, 0, tzinfo=IST).astimezone(UTC)
        due = followup_due_at(first_sent_at=sent, settings=SETTINGS)
        assert in_window(due, SETTINGS)

    def test_the_delay_is_configurable(self):
        s = CampaignSettings(followup_after_days=7)
        sent = datetime(2026, 10, 12, 11, 0, tzinfo=IST).astimezone(UTC)
        due = followup_due_at(first_sent_at=sent, settings=s)
        assert (due.astimezone(IST).date() - date(2026, 10, 12)).days >= 7

    def test_calendar_days_not_business_days(self):
        """Three business days from a Friday would be Wednesday — too long to read as a
        follow-up. Three calendar days then rolled gives Monday."""
        sent = datetime(2026, 10, 16, 11, 0, tzinfo=IST).astimezone(UTC)
        due = followup_due_at(first_sent_at=sent, settings=SETTINGS)
        assert due.astimezone(IST).date() == date(2026, 10, 19)

    def test_an_overdue_followup_is_detected(self):
        due = ist(2026, 10, 12, 11, 0)
        assert not is_followup_overdue(due_at=due, now=due + timedelta(days=1), settings=SETTINGS)
        assert is_followup_overdue(due_at=due, now=due + timedelta(days=10), settings=SETTINGS)


class TestWarmup:
    """§9.2: 20/day, +10 every 2 business days, to the cap."""

    START = date(2026, 10, 12)  # a Monday

    @pytest.mark.parametrize(
        "business_day,expected",
        [(1, 20), (2, 20), (3, 30), (4, 30), (5, 40), (17, 100), (30, 100)],
    )
    def test_the_ramp(self, business_day, expected):
        day = self.START
        advanced = 1
        while advanced < business_day:
            day += timedelta(days=1)
            if SETTINGS.is_send_day(day):
                advanced += 1
        assert warmup_cap(today=day, settings=SETTINGS, warmup_started_on=self.START) == expected

    def test_it_reaches_the_cap_on_business_day_17(self):
        """About three and a half weeks, as §9.2 states."""
        day, advanced = self.START, 1
        while advanced < 17:
            day += timedelta(days=1)
            if SETTINGS.is_send_day(day):
                advanced += 1
        assert warmup_cap(today=day, settings=SETTINGS, warmup_started_on=self.START) == 100

    def test_the_ramp_counts_business_days_not_calendar_days(self):
        """Counting calendar days would jump two steps over a weekend: start at 20 on Friday
        and send 40 on Monday, which is exactly the spike warm-up exists to avoid."""
        friday, monday = date(2026, 10, 16), date(2026, 10, 19)
        assert warmup_cap(today=monday, settings=SETTINGS, warmup_started_on=friday) == 20

    def test_no_warmup_date_means_the_full_cap(self):
        assert warmup_cap(today=self.START, settings=SETTINGS, warmup_started_on=None) == 100

    def test_the_mailbox_cap_overrides_the_campaign_cap(self):
        assert (
            warmup_cap(
                today=self.START + timedelta(days=60),
                settings=SETTINGS,
                warmup_started_on=self.START,
                daily_cap=50,
            )
            == 50
        )

    def test_the_ramp_never_exceeds_the_cap(self):
        for extra in range(0, 200, 7):
            cap = warmup_cap(
                today=self.START + timedelta(days=extra),
                settings=SETTINGS,
                warmup_started_on=self.START,
            )
            assert 20 <= cap <= 100

    def test_remaining_capacity_never_goes_negative(self):
        assert remaining_capacity(effective_cap=20, sent_today=25) == 0
        assert remaining_capacity(effective_cap=20, sent_today=5) == 15


class TestPacing:
    def test_the_cap_is_spread_across_the_window(self):
        """At 100/day over 7.5 hours the mean gap is about 4.5 minutes — not a burst."""
        now = ist(2026, 10, 12, 10, 0)
        gap = pacing_gap(now=now, settings=SETTINGS, remaining=100, rng=random.Random(0))
        assert timedelta(minutes=2) < gap < timedelta(minutes=8)

    def test_the_gap_is_clamped_at_both_ends(self):
        now = ist(2026, 10, 12, 10, 0)
        rng = random.Random(1)

        tight = pacing_gap(now=now, settings=SETTINGS, remaining=10_000, rng=rng)
        assert tight >= MIN_GAP * (1 - 0.3)

        loose = pacing_gap(now=now, settings=SETTINGS, remaining=1, rng=rng)
        assert loose <= MAX_GAP * 1.3

    def test_jitter_makes_successive_gaps_differ(self):
        """An email exactly every 270 seconds is obviously a machine."""
        now = ist(2026, 10, 12, 11, 0)
        rng = random.Random(42)
        gaps = {
            pacing_gap(now=now, settings=SETTINGS, remaining=50, rng=rng).total_seconds()
            for _ in range(10)
        }
        assert len(gaps) > 5, "the gaps should vary"

    def test_no_remaining_capacity_waits_the_maximum(self):
        gap = pacing_gap(now=ist(2026, 10, 12, 11, 0), settings=SETTINGS, remaining=0)
        assert gap == MAX_GAP

    def test_after_the_window_waits_rather_than_dividing_by_nothing(self):
        gap = pacing_gap(now=ist(2026, 10, 12, 20, 0), settings=SETTINGS, remaining=50)
        assert gap == MAX_GAP


class TestDayBounds:
    def test_the_day_is_the_operators_day_not_utc(self):
        """§9.2 counts sends per IST day. On UTC the cap would reset mid-afternoon in India."""
        start, end = ist_day_bounds(ist(2026, 10, 12, 23, 30), SETTINGS)
        assert start.astimezone(IST).date() == date(2026, 10, 12)
        assert start.astimezone(IST).time() == time(0, 0)
        assert (end - start) == timedelta(days=1)

    def test_an_early_morning_utc_instant_belongs_to_the_right_ist_day(self):
        # 20:00 UTC on the 12th is 01:30 IST on the 13th.
        start, _ = ist_day_bounds(datetime(2026, 10, 12, 20, 0, tzinfo=UTC), SETTINGS)
        assert start.astimezone(IST).date() == date(2026, 10, 13)


class TestBusinessDays:
    def test_it_is_zero_on_the_first_day(self):
        """So the warm-up ramp can index off it directly."""
        assert business_days_between(date(2026, 10, 12), date(2026, 10, 12), SETTINGS) == 0

    def test_a_weekend_is_skipped(self):
        # Friday to Monday is one business day apart.
        assert business_days_between(date(2026, 10, 16), date(2026, 10, 19), SETTINGS) == 1

    def test_a_backwards_range_is_zero(self):
        assert business_days_between(date(2026, 10, 19), date(2026, 10, 12), SETTINGS) == 0


class TestNoResponse:
    def test_it_waits_the_configured_days(self):
        last = ist(2026, 10, 12, 11, 0)
        when = no_response_at(last_sent_at=last, settings=SETTINGS)
        assert (when.astimezone(IST).date() - date(2026, 10, 12)).days == 5


# ---------------------------------------------------------------------------
# Property-based tests. §22 asks for these on the scheduling maths specifically.
# ---------------------------------------------------------------------------

# Naive bounds by design: hypothesis generates naive datetimes, which are then stamped UTC.
utc_datetimes = st.datetimes(
    min_value=datetime(2026, 1, 1),  # noqa: DTZ001
    max_value=datetime(2028, 12, 31),  # noqa: DTZ001
).map(lambda d: d.replace(tzinfo=UTC))


class TestInvariants:
    @given(utc_datetimes)
    @hyp_settings(max_examples=200, deadline=None)
    def test_next_window_open_always_lands_inside_the_window(self, when):
        """The single most important invariant: whatever the loop is told to wait for, that
        instant must actually be sendable."""
        opened = next_window_open(when, SETTINGS)
        assert in_window(opened, SETTINGS)

    @given(utc_datetimes)
    @hyp_settings(max_examples=200, deadline=None)
    def test_next_window_open_never_goes_backwards(self, when):
        assert next_window_open(when, SETTINGS) >= when

    @given(utc_datetimes)
    @hyp_settings(max_examples=200, deadline=None)
    def test_inside_the_window_is_a_fixed_point(self, when):
        if in_window(when, SETTINGS):
            assert next_window_open(when, SETTINGS) == when

    @given(utc_datetimes)
    @hyp_settings(max_examples=200, deadline=None)
    def test_a_followup_is_always_sendable_and_never_immediate(self, sent_at):
        due = followup_due_at(first_sent_at=sent_at, settings=SETTINGS)
        assert in_window(due, SETTINGS)
        assert due > sent_at, "a follow-up must be in the future"

    @given(utc_datetimes)
    @hyp_settings(max_examples=100, deadline=None)
    def test_a_followup_is_never_on_a_weekend(self, sent_at):
        due = followup_due_at(first_sent_at=sent_at, settings=SETTINGS)
        assert due.astimezone(IST).weekday() < 5

    @given(
        st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31)),
        st.integers(min_value=0, max_value=400),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_warmup_ramp_is_monotonic_and_bounded(self, start, offset):
        """It may never decrease, and may never exceed the cap. A dip would send fewer emails
        on a later day for no reason; an overshoot is the spike warm-up exists to prevent."""
        today = start + timedelta(days=offset)
        tomorrow = today + timedelta(days=1)
        a = warmup_cap(today=today, settings=SETTINGS, warmup_started_on=start)
        b = warmup_cap(today=tomorrow, settings=SETTINGS, warmup_started_on=start)
        assert 20 <= a <= 100
        assert b >= a

    @given(
        st.integers(min_value=1, max_value=500),
        st.integers(min_value=0, max_value=23),
    )
    @hyp_settings(max_examples=200, deadline=None)
    def test_the_pacing_gap_is_always_positive_and_bounded(self, remaining, hour):
        now = datetime(2026, 10, 12, hour, 0, tzinfo=IST).astimezone(UTC)
        gap = pacing_gap(now=now, settings=SETTINGS, remaining=remaining, rng=random.Random(hour))
        assert gap > timedelta(0)
        # Jitter can exceed the clamp by its own fraction, which is intended.
        assert gap <= MAX_GAP * (1 + 0.3)

    @given(utc_datetimes, st.integers(min_value=1, max_value=200))
    @hyp_settings(max_examples=100, deadline=None)
    def test_day_bounds_always_contain_the_instant(self, when, _unused):
        start, end = ist_day_bounds(when, SETTINGS)
        assert start <= when < end

    @given(st.integers(min_value=0, max_value=200), st.integers(min_value=0, max_value=200))
    @hyp_settings(max_examples=100, deadline=None)
    def test_remaining_capacity_is_never_negative(self, cap, sent):
        assert remaining_capacity(effective_cap=cap, sent_today=sent) >= 0

    @given(st.integers(min_value=0, max_value=6))
    def test_a_single_send_day_still_produces_a_valid_window(self, weekday):
        """Even a campaign that only sends on one weekday must get a usable answer."""
        s = CampaignSettings(send_days=[weekday])
        opened = next_window_open(ist(2026, 10, 12, 20, 0), s)
        assert in_window(opened, s)
        assert opened.astimezone(IST).weekday() == weekday

    @given(utc_datetimes)
    @hyp_settings(max_examples=100, deadline=None)
    def test_window_close_is_after_the_window_opens_on_that_day(self, when):
        assume(in_window(when, SETTINGS))
        assert window_close(when, SETTINGS) > when
