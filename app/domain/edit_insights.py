"""S19.7 — edited vs unedited insights (ADR-073).

Compares reply outcomes for drafts that were sent as written against those a
person edited, with counts, pending counted separately, and a floor below
which no percentages are reported — to avoid a 1-of-1 rate being read as
evidence.

A silence is "no response yet", never a rejection. The module is pure: no I/O,
no Settings, operates on plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass

_FLOOR = 5


@dataclass(frozen=True)
class OutcomeBucket:
    """Counts for one edit class (edited / not edited)."""

    total: int
    replied: int
    positive: int
    pending: int

    @property
    def reply_rate(self) -> float | None:
        """Reply rate, or None if below the floor."""
        concluded = self.total - self.pending
        if concluded < _FLOOR:
            return None
        return self.replied / concluded if concluded else 0.0

    @property
    def positive_rate(self) -> float | None:
        """Positive reply rate out of replied, or None if below the floor."""
        if self.replied < _FLOOR:
            return None
        return self.positive / self.replied if self.replied else 0.0


@dataclass(frozen=True)
class EditInsights:
    """Side-by-side view of edited vs as-written drafts."""

    edited: OutcomeBucket
    as_written: OutcomeBucket
    floor: int = _FLOOR
    note: str = ""


def compute(messages: list[dict[str, object]]) -> EditInsights:
    """Compute edit insights from a list of message/outcome dicts.

    Each dict must have:
      - "edited": bool — True when the draft was changed before sending
      - "outcome": str | None — "replied", "positive", "bounced", "unsubscribed"
                                or None for pending

    Args:
        messages: One dict per sent message.

    Returns:
        EditInsights with two OutcomeBuckets.
    """
    edited_counts: dict[str, int] = {"total": 0, "replied": 0, "positive": 0, "pending": 0}
    as_written_counts: dict[str, int] = {"total": 0, "replied": 0, "positive": 0, "pending": 0}

    for msg in messages:
        edited = bool(msg.get("edited"))
        outcome = msg.get("outcome")
        counts = edited_counts if edited else as_written_counts

        counts["total"] += 1
        if outcome is None:
            counts["pending"] += 1
        elif outcome in ("replied", "positive"):
            counts["replied"] += 1
            if outcome == "positive":
                counts["positive"] += 1

    return EditInsights(
        edited=OutcomeBucket(**edited_counts),
        as_written=OutcomeBucket(**as_written_counts),
    )
