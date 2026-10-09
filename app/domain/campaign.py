"""Campaign settings: the sending rules, as validated data rather than scattered constants.

`ARCHITECTURE_TARGET.md` §5.2. The defaults here are the product rules from `CLAUDE.md`, so a
campaign created with no settings behaves the way the owner specified: weekdays only, inside an
IST window, one follow-up after three days, every draft reviewed.

Pure data with no I/O. The scheduling *maths* that consumes these lives in
`app.domain.scheduling` with an injected clock (Phase 5); this module only says what is allowed.

The prototype had none of this. There was no window, no cap, no follow-up, no timezone — one grep
for all of those terms returned seven incidental hits across the whole codebase (issues A7, D3).
"""

from __future__ import annotations

from datetime import date, time
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Monday to Friday. India-only B2B audience, and weekend cold email reads as spam.
DEFAULT_SEND_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4)

#: Asia/Kolkata throughout. Timestamps are stored as UTC `timestamptz`; the conversion happens
#: only in the scheduling layer.
DEFAULT_TIMEZONE = "Asia/Kolkata"

ApprovalMode = Literal["review_all", "auto_send_if_confidence"]
SearchPolicy = Literal["never", "when_thin", "always"]
ThinResearchPolicy = Literal["review_flag", "skip"]

Weekday = Annotated[int, Field(ge=0, le=6, description="0 = Monday, 6 = Sunday")]


class CampaignSettings(BaseModel):
    """When and how fast a campaign may send, and how much a human must see."""

    model_config = ConfigDict(extra="forbid")

    # --- When ---
    timezone: str = DEFAULT_TIMEZONE
    send_days: list[Weekday] = Field(default_factory=lambda: list(DEFAULT_SEND_DAYS))
    window_start: time = time(10, 0)
    window_end: time = time(17, 30)
    #: Dates to skip entirely, on top of the weekend. See QUESTIONS.md Q5.
    holidays: list[date] = Field(default_factory=list, max_length=100)

    # --- How much ---
    #: Counts *every* outbound email — initial, follow-up and reply alike (ADR 8 in the target
    #: architecture). At steady state each lead costs two sends, so 100/day is roughly 50 new
    #: leads plus 50 follow-ups.
    daily_cap: int = Field(default=100, ge=1, le=2000)

    # --- Follow-ups ---
    followup_after_days: int = Field(default=3, ge=1, le=60)
    max_followups: int = Field(default=1, ge=0, le=5)
    #: After the last follow-up, wait this long before calling it NO_RESPONSE.
    final_wait_days: int = Field(default=5, ge=1, le=90)
    #: A follow-up approved later than this after its due date is flagged, not sent — a stale
    #: "just following up" is worse than silence.
    followup_max_delay_days: int = Field(default=3, ge=1, le=30)

    # --- Review ---
    #: `auto_send_if_confidence` is designed for but deliberately never enabled; a campaign that
    #: asks for it is rejected below.
    approval_mode: ApprovalMode = "review_all"
    #: Drafts below this confidence need individual review even in a bulk approve.
    bulk_approve_min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)

    # --- Research ---
    search_policy: SearchPolicy = "when_thin"
    thin_research_policy: ThinResearchPolicy = "review_flag"
    regen_attempts_max: int = Field(default=3, ge=1, le=10)

    @field_validator("timezone")
    @classmethod
    def _timezone_must_exist(cls, v: str) -> str:
        """Fail here rather than at send time, where a bad zone would stall the loop."""
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("send_days")
    @classmethod
    def _send_days_are_sorted_and_unique(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("send_days cannot be empty — the campaign could never send")
        return sorted(set(v))

    @model_validator(mode="after")
    def _window_is_a_real_interval(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError(
                f"window_start ({self.window_start}) must be before window_end ({self.window_end})"
            )
        return self

    @model_validator(mode="after")
    def _auto_send_stays_off(self) -> Self:
        """Hard product rule: a human approves every first email and every reply.

        The mode exists in the type so the data model does not need changing later, but enabling
        it is refused here — turning it on has to be a deliberate code change with its own
        review, not a settings edit.
        """
        if self.approval_mode != "review_all":
            raise ValueError(
                "approval_mode must be 'review_all': automatic sending without human review "
                "is designed for but not enabled"
            )
        return self

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def window_seconds(self) -> int:
        """Length of the daily send window, used to pace sends across it."""
        start = self.window_start.hour * 3600 + self.window_start.minute * 60
        end = self.window_end.hour * 3600 + self.window_end.minute * 60
        return end - start

    def is_send_day(self, when: date) -> bool:
        """True if this date is a configured send day and not a listed holiday."""
        return when.weekday() in self.send_days and when not in set(self.holidays)
