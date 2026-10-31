"""Fetching a feed. Separate from parsing so the parser never needs the network."""

from __future__ import annotations

import httpx

from app.services.jobposts.feeds import MAX_FEED_BYTES, FeedError, Posting, feed_url, parse_feed

TIMEOUT_SECONDS = 20.0


async def fetch_board(
    source: str, slug: str, *, client: httpx.AsyncClient | None = None
) -> list[Posting]:
    """Read one board. Raises `FeedError` with a sentence an operator can act on.

    A 404 is almost always a wrong slug rather than a moved endpoint -- `api.lever.co` answers
    404 for a company that is simply not on Lever -- so the message says so.
    """
    url = feed_url(source, slug)
    owned = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = await http.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise FeedError(f"could not reach the {source} board {slug}: {type(exc).__name__}") from exc
    finally:
        if owned:
            await http.aclose()

    if response.status_code == 404:
        raise FeedError(
            f"{source} has no board called {slug}. Check the slug in the board's own URL; a "
            "company that is not on this provider answers 404 too."
        )
    if response.status_code >= 400:
        raise FeedError(f"the {source} board {slug} answered HTTP {response.status_code}")

    body = response.content
    if len(body) > MAX_FEED_BYTES:
        raise FeedError(f"the {source} board {slug} returned more than we will read")
    return parse_feed(source, body)
