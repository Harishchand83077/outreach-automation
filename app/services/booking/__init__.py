"""The signed booking webhook (F15, ADR-045).

The only producer of `MEETING_BOOKED` besides a person clicking it. A reply still cannot reach
that state, whatever it says, and a test says so.
"""
