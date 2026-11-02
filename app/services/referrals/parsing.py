"""Reading a LinkedIn connections export, and normalising a company name (F10, ADR-042).

A pure module: no database, no network, no clock beyond what is handed in. **Nothing in this
package fetches LinkedIn** -- hard rule 4, and `tests/unit/test_referrals_never_fetch.py`
asserts it over the whole tree rather than trusting it.

Two things about the real file shape, both found by looking at an actual export rather than
assuming:

1. **It begins with a preamble, not with headers.** LinkedIn writes a `Notes:` block and a blank
   line before the header row. A parser that takes line 1 as the header reads the whole file as
   one unnamed column and reports zero connections, which is indistinguishable from an empty
   network -- the same shape of silent wrong answer as the ATS feed that returned zero postings.
   So the header row is **found**, and a file with no recognisable header is an error with a
   sentence, never an empty result.
2. **`Email Address` is present for some connections.** It is dropped here, before anything is
   written. See `Connection`'s docstring: storing it is what would turn a referral finder into
   somebody's address book.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

#: Longer than this is not a connections export. 30,000 connections is the platform's own limit.
MAX_ROWS = 30_000
MAX_BYTES = 12 * 1024 * 1024


class ExportError(Exception):
    """The file is not a connections export, with a sentence saying what was expected."""


@dataclass(frozen=True)
class ParsedConnection:
    full_name: str
    profile_url: str | None
    company: str | None
    position: str | None
    connected_on: date | None


@dataclass(frozen=True)
class ParsedExport:
    connections: list[ParsedConnection]
    #: Rows that had no name at all. Reported rather than silently dropped, because a count of
    #: "4,312 of 4,400" is the only thing that tells somebody the file was partly unreadable.
    skipped: int
    truncated: int


#: Suffixes that are part of a legal name and not of the company anybody says out loud. Matching
#: on the raw string would miss "Acme, Inc." against a posting that says "Acme".
_SUFFIXES = (
    "incorporated",
    "inc",
    "limited",
    "ltd",
    "llp",
    "llc",
    "plc",
    "pvt",
    "private",
    "pte",
    "gmbh",
    "corp",
    "corporation",
    "company",
    "co",
    "holdings",
    "group",
    "technologies",
    "technology",
    "labs",
    "software",
    "solutions",
    "systems",
)

_PUNCTUATION = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalise_company(raw: str | None) -> str | None:
    """A matching key for a company name.

    Lower-cased, punctuation removed, then trailing legal and generic suffixes stripped --
    repeatedly, because "Acme Software Solutions Pvt Ltd" carries four of them.

    **Stripping stops before it would empty the name.** "Technology Group Ltd" is somebody's
    actual company, and reducing it to nothing would match it against every other company whose
    name is also all suffixes. The last remaining word is kept whatever it is.
    """
    if raw is None:
        return None
    text = _SPACES.sub(" ", _PUNCTUATION.sub(" ", raw.strip().lower())).strip()
    if not text:
        return None
    words = text.split(" ")
    while len(words) > 1 and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(words)[:300]


def _header_row(rows: list[list[str]]) -> int:
    """Where the real header is.

    LinkedIn's preamble is three lines and a blank one, but it has changed before and will
    again, so the header is recognised by its own content: a row naming a first name and a
    company. Scanning is bounded, because a 12 MB file of the wrong kind should not be searched
    line by line for something that is not there.
    """
    for index, row in enumerate(rows[:10]):
        lowered = {cell.strip().lower() for cell in row}
        if {"first name", "company"} <= lowered or {"full name", "company"} <= lowered:
            return index
    raise ExportError(
        "This does not look like a LinkedIn connections export. Download Connections.csv from "
        "Settings -> Data privacy -> Get a copy of your data, and upload that file unchanged."
    )


def _date(raw: str) -> date | None:
    """`Connected On` is `21 Mar 2024` in the export, and locale-dependent in older ones."""
    value = raw.strip()
    if not value:
        return None
    for pattern in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%y", "%d/%m/%Y"):
        try:
            # DTZ007 is suppressed below: a calendar date with no time and no zone is what
            # the export holds. "Connected on 21 Mar 2024" happened on that day wherever
            # either party was, and a timezone would invent precision the file does not have.
            return datetime.strptime(value, pattern).date()  # noqa: DTZ007
        except ValueError:
            continue
    # Not fatal: when the connection was made is colour, and refusing the whole row over it
    # would throw away the referral this feature exists to find.
    return None


def parse_export(data: bytes) -> ParsedExport:
    """Every connection in the file. Raises `ExportError` when it is not one."""
    if len(data) > MAX_BYTES:
        raise ExportError("That file is larger than a connections export should be.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 decodes any byte string
            raise ExportError("That file is not text.") from exc

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ExportError("That file is empty.")

    start = _header_row(rows)
    header = [cell.strip().lower() for cell in rows[start]]
    index = {name: position for position, name in enumerate(header)}

    def cell(row: list[str], *names: str) -> str:
        for name in names:
            position = index.get(name)
            if position is not None and position < len(row):
                value = row[position].strip()
                if value:
                    return value
        return ""

    body = rows[start + 1 :]
    truncated = max(len(body) - MAX_ROWS, 0)
    connections: list[ParsedConnection] = []
    skipped = 0
    for row in body[:MAX_ROWS]:
        if not any(part.strip() for part in row):
            continue
        name = cell(row, "full name") or " ".join(
            part for part in (cell(row, "first name"), cell(row, "last name")) if part
        )
        if not name:
            # No name means no referral to ask for. Counted, so a partly unreadable file says so.
            skipped += 1
            continue
        company = cell(row, "company") or None
        position = cell(row, "position", "title")
        connections.append(
            ParsedConnection(
                full_name=name[:300],
                # `url` in current exports, `profile url` in older ones.
                profile_url=(cell(row, "url", "profile url") or None),
                company=company[:300] if company else None,
                position=position[:300] or None,
                connected_on=_date(cell(row, "connected on")),
            )
        )
    return ParsedExport(connections=connections, skipped=skipped, truncated=truncated)
