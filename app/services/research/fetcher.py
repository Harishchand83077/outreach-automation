"""Fetching company pages, politely and safely.

`ARCHITECTURE_TARGET.md` §12. The limits are not arbitrary — each one stops a specific way a
fetch can go wrong:

- **Redirects are followed manually, one hop at a time, revalidating each.** A public URL that
  302s to `http://127.0.0.1:6379` is the standard SSRF bypass, and it defeats any check done
  only on the original URL. `httpx`'s own `follow_redirects` would hide every hop from us.
- **A size cap enforced while streaming**, not after. Checking `Content-Length` is advisory;
  a hostile or broken server can send gigabytes regardless, so the body is abandoned mid-read.
- **`text/html` only.** A 2 GB video or a PDF is not evidence, and decoding it wastes the
  token budget we are trying to protect.
- **robots.txt is respected**, and a per-host delay is applied. We are an uninvited guest on
  someone else's server; being a well-behaved one is both correct and keeps us unblocked.
- **An honest User-Agent with a contact URL**, so an operator who dislikes us can say so
  instead of having to guess who we are.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.services.research.net import SafeTarget, UnsafeTarget, validate_url

#: Identifies us and says where to complain. The contact URL is the point: an operator who does
#: not want us there can act on it without having to work out who we are.
USER_AGENT = (
    "OutreachResearchBot/1.0 (+https://github.com/Harishchand83077/outreach-automation; "
    "respects robots.txt)"
)

MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
REQUEST_TIMEOUT = 10.0
#: One request per host per this many seconds. We are a guest on someone else's server.
HOST_DELAY_SECONDS = 2.0
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class FetchRefused(Exception):
    """We decided not to fetch this. Distinct from the fetch failing."""


@dataclass
class FetchResult:
    """One page, or the reason there isn't one. A failure is recorded, not raised, because a
    company site being down is normal and must not fail the whole research job."""

    url: str
    status: int | None = None
    html: str = ""
    bytes_read: int = 0
    error: str | None = None
    #: Where we ended up, if redirected. Stored so evidence cites what we actually read.
    final_url: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200 and bool(self.html)


@dataclass
class HostPolitenessState:
    last_request_at: float = 0.0
    robots: RobotFileParser | None = None
    robots_checked: bool = False


@dataclass
class Fetcher:
    """Fetches pages for one research run.

    Per-run rather than global: the robots and delay state is scoped to the work in hand, and a
    long-lived cache of robots files would go stale.
    """

    client: httpx.AsyncClient
    max_bytes: int = MAX_BYTES
    timeout: float = REQUEST_TIMEOUT
    host_delay: float = HOST_DELAY_SECONDS
    respect_robots: bool = True
    _hosts: dict[str, HostPolitenessState] = field(default_factory=dict)

    def _state(self, host: str) -> HostPolitenessState:
        return self._hosts.setdefault(host, HostPolitenessState())

    async def _wait_turn(self, host: str) -> None:
        """Rate-limit ourselves per host, rather than waiting to be rate-limited."""
        state = self._state(host)
        elapsed = time.monotonic() - state.last_request_at
        if state.last_request_at and elapsed < self.host_delay:
            await asyncio.sleep(self.host_delay - elapsed)
        state.last_request_at = time.monotonic()

    async def _robots(self, target: SafeTarget) -> RobotFileParser | None:
        """Fetch and parse robots.txt once per host.

        A missing or unreadable robots.txt means "no stated preference", so we proceed. Treating
        a 404 as a prohibition would make us unable to read most of the web; treating a fetch
        error as permission would be the other extreme, but a transport failure here says
        nothing about the operator's wishes.
        """
        state = self._state(target.host)
        if state.robots_checked:
            return state.robots
        state.robots_checked = True

        scheme = urlparse(target.url).scheme or "https"
        robots_url = f"{scheme}://{target.host}/robots.txt"
        try:
            response = await self.client.get(
                robots_url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return None

        if response.status_code != 200:
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        state.robots = parser
        return parser

    async def _allowed_by_robots(self, target: SafeTarget) -> bool:
        if not self.respect_robots:
            return True
        parser = await self._robots(target)
        if parser is None:
            return True
        return bool(parser.can_fetch(USER_AGENT, target.url))

    async def fetch(self, url: str) -> FetchResult:
        """Fetch one page, following redirects by hand so each hop is revalidated."""
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                target = validate_url(current)
            except UnsafeTarget as exc:
                # On a later hop this is the SSRF bypass being caught in the act.
                return FetchResult(
                    url=url,
                    error=f"refused{' after redirect' if hop else ''}: {exc}",
                    final_url=current if hop else None,
                )

            if not await self._allowed_by_robots(target):
                return FetchResult(url=url, error="robots.txt disallows this path")

            await self._wait_turn(target.host)

            try:
                result, redirect_to = await self._fetch_once(target, original=url)
            except httpx.HTTPError as exc:
                return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")

            if redirect_to is None:
                return result
            current = redirect_to

        return FetchResult(url=url, error=f"more than {MAX_REDIRECTS} redirects")

    async def _fetch_once(
        self, target: SafeTarget, *, original: str
    ) -> tuple[FetchResult, str | None]:
        """One request. Returns (result, next-url-if-redirected)."""
        async with self.client.stream(
            "GET",
            target.url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en",
            },
            timeout=self.timeout,
            follow_redirects=False,
        ) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    return (
                        FetchResult(
                            url=original,
                            status=response.status_code,
                            error="redirect without a Location header",
                        ),
                        None,
                    )
                # Resolved against the current URL, so a relative redirect works and an
                # absolute one is re-checked from scratch on the next pass.
                return FetchResult(url=original), urljoin(target.url, location)

            if response.status_code != 200:
                return (
                    FetchResult(
                        url=original,
                        status=response.status_code,
                        error=f"HTTP {response.status_code}",
                        final_url=str(response.url),
                    ),
                    None,
                )

            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type and not content_type.startswith(ALLOWED_CONTENT_TYPES):
                return (
                    FetchResult(
                        url=original,
                        status=200,
                        error=f"content-type {content_type} is not HTML",
                        final_url=str(response.url),
                    ),
                    None,
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.max_bytes:
                    # Abandoned mid-stream. Content-Length is advisory, and a server can send
                    # far more than it claims.
                    return (
                        FetchResult(
                            url=original,
                            status=200,
                            error=f"larger than {self.max_bytes} bytes",
                            bytes_read=total,
                            final_url=str(response.url),
                        ),
                        None,
                    )
                chunks.append(chunk)

            body = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            return (
                FetchResult(
                    url=original,
                    status=200,
                    html=body.decode(encoding, errors="replace"),
                    bytes_read=total,
                    final_url=str(response.url),
                ),
                None,
            )
