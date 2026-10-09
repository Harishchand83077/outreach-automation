"""Turning HTML into the evidence a draft may cite.

Two jobs. First, pull out the main content: `trafilatura` strips navigation, footers, cookie
banners and sidebars, so the token budget is spent on prose rather than menus. Second, keep the
result inside that budget — §20 puts the whole research-plus-draft cost at 9-12k tokens per
lead, and an unbounded "about us" page can exceed that on its own.

Pages under ~300 characters of extracted text are marked `thin`. That is usually a
JavaScript-rendered site, and it matters because thin evidence must produce an honestly generic
draft or a flagged lead — never an invented specific (§13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Roughly four characters per token. Deliberately crude: it only has to be close enough to keep
#: a budget, and measuring exactly would mean a tokeniser per provider.
CHARS_PER_TOKEN = 4

#: Hard ceiling on evidence handed to the model, in characters (~4k tokens).
MAX_EVIDENCE_CHARS = 16_000

#: Below this, a page told us nothing useful.
THIN_THRESHOLD_CHARS = 300

#: Same-domain paths worth trying, in priority order. "About" and "product" say what a company
#: does; "blog" and "news" say what it is doing *now*, which is what makes a hook timely;
#: "careers" reveals what it is building.
CANDIDATE_PATHS: tuple[str, ...] = (
    "/about",
    "/about-us",
    "/company",
    "/products",
    "/product",
    "/services",
    "/solutions",
    "/platform",
    "/blog",
    "/news",
    "/press",
    "/careers",
    "/jobs",
    "/customers",
    "/case-studies",
    "/clients",
)

_WHITESPACE = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass
class ExtractedPage:
    """One page's usable text."""

    url: str
    title: str = ""
    text: str = ""

    @property
    def is_thin(self) -> bool:
        return len(self.text) < THIN_THRESHOLD_CHARS

    @property
    def approx_tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN


def tidy(text: str) -> str:
    """Collapse the whitespace that survives extraction, without joining paragraphs.

    Paragraph breaks are kept because they carry meaning: a snippet quoted as evidence should
    not silently span two unrelated sections.
    """
    cleaned = _WHITESPACE.sub(" ", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    cleaned = "\n".join(line.strip() for line in cleaned.split("\n"))
    return _BLANK_LINES.sub("\n\n", cleaned).strip()


def extract(html: str, url: str) -> ExtractedPage:
    """Pull the main content out of one page.

    Falls back to a crude tag strip if `trafilatura` declines — some valid pages defeat it, and
    a thin result is more useful than none.
    """
    if not html or not html.strip():
        return ExtractedPage(url=url)

    title = ""
    text = ""

    try:
        import trafilatura

        extracted = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=True,
            # Favours recall: we would rather over-collect and trim than miss the one paragraph
            # that says what the company actually does.
            favor_recall=True,
            url=url,
        )
        if extracted:
            text = extracted
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None and metadata.title:
            title = str(metadata.title)
    except Exception:
        # Extraction is best-effort: a library failure must not fail the research job.
        text = ""

    if not text:
        text = _strip_tags(html)
    if not title:
        title = _title_from_html(html)

    return ExtractedPage(url=url, title=tidy(title)[:300], text=tidy(text))


def _strip_tags(html: str) -> str:
    """Last-resort text recovery. Script and style content is dropped first, or their contents
    would end up quoted as though a human had written it."""
    without_scripts = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S
    )
    return re.sub(r"<[^>]+>", " ", without_scripts)


def _title_from_html(html: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    return match.group(1) if match else ""


def build_evidence(
    pages: list[ExtractedPage],
    *,
    offer_keywords: list[str] | None = None,
    max_chars: int = MAX_EVIDENCE_CHARS,
) -> str:
    """Assemble the evidence block, prioritised and budgeted.

    Paragraphs matching the campaign's offer are kept first. The reason is practical: a long
    "about us" page mostly describes history, while the one paragraph mentioning the problem we
    solve is what makes a draft specific — and under a budget, that paragraph is the one worth
    the tokens.

    Every page is labelled with its URL, because a hook must cite a source and the model can
    only cite what it was shown.
    """
    keywords = [k.strip().lower() for k in (offer_keywords or []) if k.strip()]
    sections: list[str] = []
    remaining = max_chars

    for page in pages:
        if not page.text or remaining <= 0:
            continue

        body = _prioritise(page.text, keywords) if keywords else page.text
        header = f"--- SOURCE: {page.url}"
        if page.title:
            header += f" ({page.title})"
        header += " ---\n"

        allowance = remaining - len(header)
        if allowance <= 200:
            break

        sections.append(header + body[:allowance])
        remaining -= len(header) + min(len(body), allowance)

    return "\n\n".join(sections)


def _prioritise(text: str, keywords: list[str]) -> str:
    """Reorder paragraphs so offer-relevant ones survive truncation."""
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return text

    matching: list[str] = []
    rest: list[str] = []
    for paragraph in paragraphs:
        lowered = paragraph.lower()
        (matching if any(k in lowered for k in keywords) else rest).append(paragraph)

    return "\n\n".join(matching + rest)


def candidate_urls(base_url: str, html: str, *, limit: int = 4) -> list[str]:
    """Same-site pages worth fetching next.

    Links present in the page are preferred over guessed paths, because a real link is evidence
    the page exists; guesses only fill the remaining slots and each miss costs a request.
    """
    from urllib.parse import urljoin, urlparse

    from app.services.research.net import same_registrable_domain

    base_host = urlparse(base_url).hostname or ""
    found: list[str] = []
    seen: set[str] = {base_url.rstrip("/")}

    for match in re.finditer(r'<a\b[^>]*href=["\']([^"\'#]+)["\']', html or "", flags=re.I):
        href = match.group(1).strip()
        if href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if not same_registrable_domain(parsed.hostname or "", base_host):
            continue

        path = (parsed.path or "/").rstrip("/").lower()
        if not any(path.startswith(candidate) for candidate in CANDIDATE_PATHS):
            continue

        key = absolute.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        found.append(absolute)
        if len(found) >= limit:
            return found

    for path in CANDIDATE_PATHS:
        if len(found) >= limit:
            break
        guess = urljoin(base_url, path)
        if guess.rstrip("/") not in seen:
            seen.add(guess.rstrip("/"))
            found.append(guess)

    return found[:limit]
