"""Cursor pagination, so no endpoint can return a whole table.

`ARCHITECTURE_TARGET.md` §14: "all paginated, none returns whole tables". The prototype's
`GET /api/leads` returned every lead's full insights, draft and reply text on every poll, and the
dashboard polled five such endpoints every 2.5 seconds (issue C7). That leaked every lead's
details to anyone who found the URL, and it would not have survived a thousand leads.

Cursors rather than offsets, because an offset shifts under inserts: with new leads arriving
while somebody pages through, `OFFSET 40` silently skips rows it has already renumbered. A cursor
names the last row seen, so the next page continues from a fixed point whatever else happens.

The cursor is opaque to the client and unsigned on purpose. It encodes a timestamp and an id,
both of which the caller already received in the page it came from, so there is nothing to
protect — and every query is still scoped by `org_id`, so a tampered cursor can at worst skip
the caller's own rows, never reach someone else's.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Cursor:
    """Where the previous page stopped."""

    created_at: datetime
    row_id: uuid.UUID

    def encode(self) -> str:
        payload = json.dumps({"t": self.created_at.isoformat(), "i": str(self.row_id)})
        return base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()

    @classmethod
    def decode(cls, raw: str | None) -> Cursor | None:
        if not raw:
            return None
        try:
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()))
            moment = datetime.fromisoformat(data["t"])
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return cls(created_at=moment, row_id=uuid.UUID(data["i"]))
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The cursor is not valid. Start from the first page.",
            ) from exc


def clamp_limit(limit: int | None) -> int:
    """Never let a caller ask for everything."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


@dataclass
class Page[T]:
    """One page, and where to continue."""

    items: list[T]
    next_cursor: str | None

    def body(self, key: str) -> dict[str, Any]:
        return {key: self.items, "next_cursor": self.next_cursor}


def build_page(rows: list[Any], limit: int, serialise: Any) -> Page[Any]:
    """Turn `limit + 1` fetched rows into a page of `limit` plus a cursor.

    Fetching one extra is how "is there more?" is answered without a second COUNT query over a
    table that may be large.
    """
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = (
        Cursor(created_at=visible[-1].created_at, row_id=visible[-1].id).encode()
        if has_more and visible
        else None
    )
    return Page(items=[serialise(row) for row in visible], next_cursor=next_cursor)
