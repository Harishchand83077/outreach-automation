"""Parsing public ATS job feeds (F5).

Shapes verified against live boards on 2026-10-31 and recorded in `docs/ATS_FEEDS.md`. The
differences that matter: Lever returns a bare JSON array where Greenhouse and Ashby wrap theirs
in `jobs`, Lever's timestamps are epoch milliseconds, and Greenhouse's list response carries no
description at all.

Parsing is pure: bytes in, `Posting` out, no network. Fetching lives in `client.py` so a parser
can be tested against saved fixtures and never the network (hard rule).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SOURCES = ("greenhouse", "lever", "ashby", "pasted")

#: A board with thousands of postings would otherwise be read into memory whole. One Ashby board
#: measured 2.8 MB for 171 jobs, so this is generous rather than tight.
MAX_FEED_BYTES = 8 * 1024 * 1024

#: Posting text is truncated before storage. The retention sweep clears it after 30 days anyway;
#: this stops one pathological posting dominating a row.
MAX_BODY_CHARS = 20_000


class FeedError(ValueError):
    """The feed could not be read. Carries a sentence an operator can act on."""


@dataclass(frozen=True)
class Posting:
    source: str
    external_id: str
    title: str
    apply_url: str
    location: str | None = None
    posted_at: datetime | None = None
    body_text: str | None = None


def feed_url(source: str, slug: str) -> str:
    cleaned = slug.strip().strip("/")
    if not cleaned:
        raise FeedError("a board slug is required")
    if source == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{cleaned}/jobs"
    if source == "lever":
        return f"https://api.lever.co/v0/postings/{cleaned}"
    if source == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{cleaned}"
    raise FeedError(f"{source} is not a feed we can poll")


def _text(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned[:limit] or None


def _when(value: Any) -> datetime | None:
    """ISO 8601 or epoch milliseconds, since Lever uses the latter."""
    if value is None:
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rows(source: str, payload: Any) -> list[dict[str, Any]]:
    # Lever is the odd one out: a bare list where the others wrap in `jobs`. Getting this wrong
    # fails for exactly one source, which reads like a bad slug rather than a bad assumption.
    if source == "lever":
        if not isinstance(payload, list):
            raise FeedError("the Lever feed did not return a list of postings")
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise FeedError(f"the {source} feed did not contain a jobs list")
    return [row for row in payload["jobs"] if isinstance(row, dict)]


def _one(source: str, row: dict[str, Any]) -> Posting | None:
    external_id = _text(row.get("id"), 200)
    if not external_id:
        return None

    if source == "greenhouse":
        location = row.get("location")
        return Posting(
            source=source,
            external_id=external_id,
            title=_text(row.get("title")) or "(untitled)",
            apply_url=_text(row.get("absolute_url"), 2000) or "",
            location=_text(location.get("name")) if isinstance(location, dict) else None,
            posted_at=_when(row.get("updated_at") or row.get("first_published")),
            # Greenhouse's list response has no description; it needs a second call per job.
            body_text=None,
        )
    if source == "lever":
        categories = row.get("categories")
        return Posting(
            source=source,
            external_id=external_id,
            title=_text(row.get("text")) or "(untitled)",
            apply_url=_text(row.get("hostedUrl") or row.get("applyUrl"), 2000) or "",
            location=(_text(categories.get("location")) if isinstance(categories, dict) else None),
            posted_at=_when(row.get("createdAt")),
            body_text=_text(row.get("descriptionPlain"), MAX_BODY_CHARS),
        )
    if source == "ashby":
        if row.get("isListed") is False:
            return None
        return Posting(
            source=source,
            external_id=external_id,
            title=_text(row.get("title")) or "(untitled)",
            apply_url=_text(row.get("jobUrl") or row.get("applyUrl"), 2000) or "",
            location=_text(row.get("location")),
            posted_at=_when(row.get("publishedAt")),
            body_text=_text(row.get("descriptionPlain"), MAX_BODY_CHARS),
        )
    raise FeedError(f"{source} is not a feed we can parse")


def parse_feed(source: str, raw: bytes | str) -> list[Posting]:
    """Every usable posting in a feed response.

    A row with no id or no apply URL is skipped rather than failing the batch: one malformed
    posting must not cost the other two hundred, and the alternative is a poll that does nothing
    until somebody fixes a board we do not control.
    """
    # Checked here, not only in `_one`: an unknown source whose payload happened to have the
    # right shape otherwise returned zero postings, which is indistinguishable from an empty
    # board. A caller added later must not be able to poll a source nothing can parse.
    if source not in ("greenhouse", "lever", "ashby"):
        raise FeedError(f"{source} is not a feed we can parse")
    if isinstance(raw, bytes) and len(raw) > MAX_FEED_BYTES:
        raise FeedError("the feed response is larger than we will read")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FeedError(f"the {source} feed did not return JSON") from exc

    postings = []
    for row in _rows(source, payload):
        posting = _one(source, row)
        if posting is not None and posting.apply_url:
            postings.append(posting)
    return postings
