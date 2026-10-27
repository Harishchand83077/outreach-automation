"""Scheduling maths. Pure functions with the clock passed in.

`ARCHITECTURE_TARGET.md` §9.2 to §9.4. Nothing here touches a database or reads the wall clock,
which is the point: window edges, weekend roll-forward and the warm-up ramp are exactly the
things that are hard to get right and easy to test wrong if the answer depends on when you run
the test.

Everything arrives and leaves as UTC `datetime`. The campaign's timezone is applied only inside
these functions, so the rest of the system never has to think about IST.

The prototype had none of this — no window, no cap, no warm-up, no follow-up (issues A7, D3).
A send went out the moment a draft was approved, at whatever hour that happened to be.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, time, timedelta

from app.domain.campaign import CampaignSettings

#: Pacing bounds. The floor stops a burst when capacity is nearly spent; the ceiling stops an
#: almost-empty day from spreading two emails eight hours apart.
MIN_GAP = timedelta(seconds=60)
MAX_GAP = timedelta(minutes=20)
#: Jitter as a fraction of the gap. Identical intervals look automated to a receiving server.
JITTER_FRACTION = 0.3


def to_local(when: datetime, settings: CampaignSettings) -> datetime:
    """UTC in, campaign-local out. A naive datetime is assumed to be UTC."""
    aware = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return aware.astimezone(settings.zone)


def in_window(when: datetime, settings: CampaignSettings) -> bool:
    """Is this instant inside the campaign's send window?

    Both conditions matter: a Saturday at 11am is outside, and so is a Tuesday at 9pm.
    """
    local = to_local(when, settings)
    if not settings.is_send_day(local.date()):
        return False
    return settings.window_start <= local.time() < settings.window_end


def next_window_open(after: datetime, settings: CampaignSettings) -> datetime:
    """The next instant the window is open, at or after `after`.

    Walks forward a day at a time rather than computing in closed form. At a maximum of a year
    that is trivially cheap, and it handles holidays and arbitrary `send_days` without a special
    case for each.
    """
    local = to_local(after, settings)

    if settings.is_send_day(local.date()):
        if local.time() < settings.window_start:
            candidate = datetime.combine(local.date(), settings.window_start, tzinfo=settings.zone)
            return candidate.astimezone(UTC)
        if local.time() < settings.window_end:
            return after if after.tzinfo is not None else after.replace(tzinfo=UTC)

    # Today is unusable, so find the next day that is not.
    day = local.date() + timedelta(days=1)
    for _ in range(366):
        if settings.is_send_day(day):
            candidate = datetime.combine(day, settings.window_start, tzinfo=settings.zone)
            return candidate.astimezone(UTC)
        day += timedelta(days=1)

    raise ValueError("no send day found within a year — check send_days and the holiday list")


def window_close(when: datetime, settings: CampaignSettings) -> datetime:
    """When today's window shuts, in UTC. Used to work out remaining capacity time."""
    local = to_local(when, settings)
    closing = datetime.combine(local.date(), settings.window_end, tzinfo=settings.zone)
    return closing.astimezone(UTC)


def business_days_between(start: date, end: date, settings: CampaignSettings) -> int:
    """Send days from `start` to `end` inclusive, minus one.

    Zero on the first day, so the warm-up ramp can index off it directly. Counts the campaign's
    own send days and skips its holidays, because a ramp measured in calendar days would jump
    two steps over a weekend and send 40 on the Monday after starting at 20 on the Friday.
    """
    if end < start:
        return 0
    count = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if settings.is_send_day(day):
            count += 1
    return count


def warmup_cap(
    *,
    today: date,
    settings: CampaignSettings,
    warmup_started_on: date | None,
    warmup_start: int = 20,
    warmup_step: int = 10,
    warmup_step_days: int = 2,
    daily_cap: int | None = None,
) -> int:
    """How many emails this mailbox may send today.

    §9.2: start at 20/day, add 10 every 2 business days, up to the cap — 20, 30, 40 … reaching
    100 on business day 17, roughly three and a half weeks. A new mailbox that sends 100 on day
    one looks exactly like a compromised one.

    No warm-up date means warm-up is finished (or was never needed), so the full cap applies.
    """
    ceiling = daily_cap if daily_cap is not None else settings.daily_cap
    if warmup_started_on is None:
        return ceiling

    elapsed = business_days_between(warmup_started_on, today, settings)
    steps = elapsed // max(warmup_step_days, 1)
    return min(warmup_start + steps * warmup_step, ceiling)


def warmup_day_number(*, today: date, warmup_started_on: date | None) -> int | None:
    """Which calendar day of the warm-up ramp `today` is, counting from 1. None when not warming.

    Recorded on each sent message because it cannot be reconstructed afterwards:
    `warmup_started_on` can be reset, and then the same `sent_at` computes a different answer.
    Deliverability questions are mostly questions about *when in the ramp* something went out, so
    losing it would make the one dimension hardest to reason about also the one unrecoverable.

    Calendar days rather than the business days `warmup_cap` steps on: this is a label for an
    observation, not an allowance, and "day 9" should mean the same thing to a person reading a
    row as it does on a calendar.
    """
    if warmup_started_on is None:
        return None
    return max(1, (today - warmup_started_on).days + 1)


def remaining_capacity(*, effective_cap: int, sent_today: int) -> int:
    """What is left of today's allowance. Never negative."""
    return max(effective_cap - sent_today, 0)


def pacing_gap(
    *,
    now: datetime,
    settings: CampaignSettings,
    remaining: int,
    rng: random.Random | None = None,
) -> timedelta:
    """How long to wait before the next send.

    `gap = remaining window / remaining capacity`, clamped and jittered. Spreading the cap
    across the day rather than bursting is most of what keeps a young mailbox out of trouble —
    100 emails in ten minutes is the signature of a compromised account.

    Jitter matters for the same reason: a send exactly every 270 seconds is obviously a machine.
    """
    if remaining <= 0:
        return MAX_GAP

    closes = window_close(now, settings)
    seconds_left = max((closes - now).total_seconds(), 0.0)

    if seconds_left <= 0:
        # The window has shut; the loop will wait for the next opening anyway.
        return MAX_GAP

    base = timedelta(seconds=seconds_left / remaining)
    clamped = max(MIN_GAP, min(base, MAX_GAP))

    generator = rng or random
    factor = 1.0 + generator.uniform(-JITTER_FRACTION, JITTER_FRACTION)
    return timedelta(seconds=clamped.total_seconds() * factor)


def followup_due_at(
    *,
    first_sent_at: datetime,
    settings: CampaignSettings,
    after_days: int | None = None,
) -> datetime:
    """When a follow-up becomes due: three calendar days on, rolled into the next window.

    §9.4's table is the specification:

    | First sent | Follow-up lands |
    |---|---|
    | Mon | Thu |
    | Tue | Fri |
    | Wed | **Mon** (Sat rolls on) |
    | Thu | **Mon** (Sun rolls on) |
    | Fri | **Mon** |

    Calendar days, then rolled — not three *business* days. Three business days from Wednesday
    would be the following Monday either way, but from Friday it would be Wednesday, which is
    too long a gap to still read as a follow-up.
    """
    days = after_days if after_days is not None else settings.followup_after_days
    local = to_local(first_sent_at, settings)
    target_date = local.date() + timedelta(days=days)

    # Keep the original time of day if it is inside the window, so a follow-up lands at a
    # similar hour rather than always at the window's opening minute.
    target_time = (
        local.time()
        if settings.window_start <= local.time() < settings.window_end
        else settings.window_start
    )
    candidate = datetime.combine(target_date, target_time, tzinfo=settings.zone).astimezone(UTC)

    return candidate if in_window(candidate, settings) else next_window_open(candidate, settings)


def no_response_at(
    *,
    last_sent_at: datetime,
    settings: CampaignSettings,
) -> datetime:
    """When a silent lead may be closed as `NO_RESPONSE`, after the last follow-up."""
    local = to_local(last_sent_at, settings)
    target = local.date() + timedelta(days=settings.final_wait_days)
    candidate = datetime.combine(target, settings.window_start, tzinfo=settings.zone)
    return candidate.astimezone(UTC)


def ist_day_bounds(when: datetime, settings: CampaignSettings) -> tuple[datetime, datetime]:
    """The UTC instants bounding the campaign-local day containing `when`.

    Daily counters use these. §9.2 is explicit that `sent_today` is counted from `messages`
    rather than kept as a separate number, so it cannot drift — and "today" has to mean the
    operator's day, not UTC's, or the cap would reset mid-afternoon in India.
    """
    local = to_local(when, settings)
    start = datetime.combine(local.date(), time(0, 0), tzinfo=settings.zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def is_followup_overdue(*, due_at: datetime, now: datetime, settings: CampaignSettings) -> bool:
    """Whether a follow-up has waited so long it should be flagged rather than sent.

    A "just following up on my email" that arrives three weeks late is worse than silence: it
    tells the recipient we are not paying attention.
    """
    return now > due_at + timedelta(days=settings.followup_max_delay_days)
