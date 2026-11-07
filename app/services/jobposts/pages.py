"""A careers page with no feed: fetch it, hash it, and say only that it changed (F22, ADR-054).

**What this can say is much less than a feed, and the design is mostly about not overstating
it.** A feed gives a role, a title and an apply URL. A page gives bytes. So a change here
produces a `signal_events` row -- a reason to look -- and deliberately **no graph edge**: "the
text of this page is different today" is not a claim about the company, and ADR-051's columns
would have nothing to hold. A hash is evidence that something happened and no evidence of what.

**`robots.txt` is honoured, and it is the whole permission model here.** A board's terms are a
document somebody read in advance (`terms.py`); a company's own careers page has no such
document, so the only statement of intent available is the one the site publishes for exactly
this purpose. A page that disallows us is not fetched and the watch says so on the screen,
rather than being quietly polled anyway.

**The text is hashed, never stored.** Storing the page would make this a crawler with a cache:
somebody else's copy, in our database, for every company any workspace watches. The hash answers
the only question being asked, and `PRIVACY.md` can say we keep no page text, which would stop
being true the moment this held the body.
"""

from __future__ import annotations

import hashlib
import re
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

TIMEOUT_SECONDS = 20.0
MAX_PAGE_BYTES = 2_000_000
#: Sent so a site owner reading their logs can see who we are and say no. A fetch that cannot be
#: identified is a fetch nobody can refuse.
USER_AGENT = "outreach-automation/1.0 (careers page watcher; respects robots.txt)"

_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_MARKUP = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


class PageUnavailable(RuntimeError):
    """The page could not be read. Transient: worth trying again tomorrow."""


class PageRefused(RuntimeError):
    """The site's own `robots.txt` says not to. Permanent until they change it."""


@dataclass(frozen=True)
class PageState:
    """What one fetch found."""

    url: str
    content_hash: str
    #: Characters of visible text, so a page that went from a listing to a maintenance notice is
    #: distinguishable from one that changed a word. Reported, never interpreted.
    length: int
    fetched_at: datetime


def visible_text(html: str) -> str:
    """The page's words, flattened, so an unchanged page hashes the same twice.

    Scripts and styles are dropped first: a build id or an analytics nonce in a `<script>`
    changes on every deploy, and hashing it would report a change every single day -- which is
    the same as reporting nothing, except that somebody would act on the first few.
    """
    without_code = _TAGS.sub(" ", html or "")
    return _SPACES.sub(" ", _MARKUP.sub(" ", without_code)).strip()


def hash_of(html: str) -> str:
    return hashlib.sha256(visible_text(html).encode("utf-8", "ignore")).hexdigest()


async def allowed_by_robots(url: str, *, client: httpx.AsyncClient | None = None) -> bool:
    """Whether the site's `robots.txt` permits this fetch.

    **A `robots.txt` we cannot read is treated as permission**, which is the convention the
    standard itself describes and the only workable reading: most sites serve no such file, and
    refusing every one of them would mean the feature never runs. A file that exists and
    disallows us is honoured.
    """
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    owned = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    try:
        response = await http.get(robots_url, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError:
        return True
    finally:
        if owned:
            await http.aclose()

    if response.status_code >= 400:
        return True

    parser = urllib.robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())
    return bool(parser.can_fetch(USER_AGENT, url))


async def read_page(url: str, *, client: httpx.AsyncClient | None = None) -> PageState:
    """Fetch one careers page and return its hash. Never returns the text.

    Raises `PageRefused` when `robots.txt` says no, and `PageUnavailable` for anything else --
    two classes rather than one, because they need different answers: a refusal is permanent
    until the site changes its mind and a timeout is worth trying again tomorrow. Collapsing
    them is how a site that said no gets asked again every day, and how one bad afternoon
    permanently stops a watch.
    """
    cleaned = url.strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        raise PageUnavailable("a careers page URL must be http or https")

    owned = client is None
    http = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=True)
    try:
        if not await allowed_by_robots(cleaned, client=http):
            raise PageRefused(
                "this site's robots.txt asks us not to fetch that page, so we do not. Watch "
                "their board on an ATS instead, or paste a posting when you see one."
            )
        try:
            response = await http.get(cleaned, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise PageUnavailable(f"could not reach {cleaned}: {type(exc).__name__}") from exc
    finally:
        if owned:
            await http.aclose()

    if response.status_code >= 400:
        raise PageUnavailable(f"{cleaned} answered HTTP {response.status_code}")
    if len(response.content) > MAX_PAGE_BYTES:
        raise PageUnavailable(
            f"{cleaned} is larger than {MAX_PAGE_BYTES} bytes, which is not a careers page"
        )

    text = visible_text(response.text)
    return PageState(
        url=cleaned,
        content_hash=hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest(),
        length=len(text),
        fetched_at=datetime.now(tz=UTC),
    )
