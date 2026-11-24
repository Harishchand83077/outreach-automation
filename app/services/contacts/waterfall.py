"""Contact lookup waterfall — pure, no I/O (S18.4, ADR-066).

Given a list of provider credentials, returns them in the order a waterfall
should try them: lower ``priority`` first, NULL priority last, ties broken by
provider name for determinism.  Invalid (provider-refused) and revoked
credentials are excluded.

**No network calls here.**  The waterfall returns an ordered list; calling the
providers is still ``lookup.run``, which the button-press guard already covers.
No ``Settings`` argument and no ``httpx`` import — the waterfall has no way to
fall back to an env key.
"""

from __future__ import annotations

from app.db.models import ProviderCredential

#: Sentinel for NULL priority so it sorts after any real integer.
_NULL_PRIORITY = 2**31


def ordered_providers(
    credentials: list[ProviderCredential],
) -> list[ProviderCredential]:
    """Return credentials sorted for the waterfall, skipping invalid/revoked ones.

    Args:
        credentials: All live ``ProviderCredential`` rows for a workspace.

    Returns:
        Filtered and sorted list — lower ``priority`` first, then ``provider`` name.
    """
    usable = [c for c in credentials if c.invalid_reason is None and c.revoked_at is None]
    return sorted(
        usable,
        key=lambda c: (c.priority if c.priority is not None else _NULL_PRIORITY, c.provider),
    )
