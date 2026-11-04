"""Signals: a reason to write to a company today (F11, ADR-049).

**Nothing in this package can send, draft or create a lead**, and
`tests/unit/test_signals_only_propose.py` asserts all three over the source tree rather than
leaving them to good intentions. A signal is an entry on a list somebody reads.
"""

from app.services.signals import intake, watchlist

__all__ = ["intake", "watchlist"]
