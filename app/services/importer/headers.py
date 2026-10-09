"""Mapping a spreadsheet's headers onto our fields.

The prototype normalised headers with `str.title()` and then demanded exactly `Name`, `Email` and
`Company`. Measured failures: `E-mail` became `E-Mail`, and `Company Name`, `First Name` and
`full_name` matched nothing — so a perfectly ordinary sheet was rejected with "Missing required
columns" (issue B7).

Two deliberate design points:

- Matching is by *shape*, not by exact string: case, spaces, hyphens and underscores are all
  stripped before comparison, so `E-mail`, `e_mail` and `E Mail` are one key.
- An unmapped required column is **reported, not guessed**. The user confirms a mapping before
  anything is imported (§7A), because silently importing the wrong column means emailing the
  wrong people.

Unrecognised columns are not dropped — they go to `leads.extra` so research and drafting can use
them (issue A5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Our fields, and the header shapes that map to each. Longer, more specific aliases must not be
#: shadowed by shorter ones, so lookup is exact-on-normalised-form rather than substring.
ALIASES: dict[str, tuple[str, ...]] = {
    "full_name": (
        "name",
        "fullname",
        "full name",
        "contact",
        "contactname",
        "person",
        "founder",
        "foundername",
        "leadname",
    ),
    "email": (
        "email",
        "emailaddress",
        "e-mail",
        "mail",
        "emailid",
        "workemail",
        "businessemail",
        "contactemail",
    ),
    "company_name": (
        "company",
        "companyname",
        "organisation",
        "organization",
        "org",
        "startup",
        "business",
        "businessname",
        "account",
    ),
    "company_url": (
        "companyurl",
        "website",
        "url",
        "site",
        "web",
        "domain",
        "companywebsite",
        "websiteurl",
        "homepage",
        "companydomain",
    ),
    "job_title": ("title", "jobtitle", "role", "designation", "position"),
    "linkedin_url": ("linkedin", "linkedinurl", "linkedinprofile", "li", "liurl"),
    "notes": ("notes", "note", "comment", "comments", "context", "background"),
}

#: Required before an import may be committed.
REQUIRED_FIELDS: tuple[str, ...] = ("full_name", "email", "company_name")

#: Strongly recommended — research is thin without them, but they do not block an import.
RECOMMENDED_FIELDS: tuple[str, ...] = ("company_url", "job_title")

#: First and last name arrive as separate columns often enough to be worth handling.
FIRST_NAME_ALIASES = frozenset({"firstname", "first", "givenname", "forename"})
LAST_NAME_ALIASES = frozenset({"lastname", "last", "surname", "familyname"})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise(header: str) -> str:
    """Reduce a header to its comparable shape.

    `E-mail`, `e_mail`, ` E Mail ` and `EMAIL` all become `email`, which is the whole point —
    the prototype compared capitalisation instead and failed on real sheets.
    """
    return _NON_ALNUM.sub("", (header or "").strip().lower())


#: Reverse index, built once. Keyed by normalised alias.
_LOOKUP: dict[str, str] = {
    normalise(alias): field_name for field_name, aliases in ALIASES.items() for alias in aliases
}


@dataclass
class HeaderMapping:
    """What each spreadsheet column was understood to be.

    Returned to the user for confirmation *before* any row is imported, so a wrong guess is
    caught by a person rather than discovered by a recipient.
    """

    #: Column index → our field name.
    mapped: dict[int, str] = field(default_factory=dict)
    #: Column index → the original header, for columns we did not recognise.
    unmapped: dict[int, str] = field(default_factory=dict)
    #: Original headers, by index, as they appeared in the sheet.
    headers: dict[int, str] = field(default_factory=dict)
    #: Indices of the columns holding a split first/last name, if that is how the sheet is built.
    first_name_column: int | None = None
    last_name_column: int | None = None
    #: Our fields that appear in more than one column — ambiguous, so the user must choose.
    duplicates: dict[str, list[int]] = field(default_factory=dict)

    @property
    def missing_required(self) -> list[str]:
        present = set(self.mapped.values())
        if self.first_name_column is not None:
            present.add("full_name")
        return [f for f in REQUIRED_FIELDS if f not in present]

    @property
    def missing_recommended(self) -> list[str]:
        present = set(self.mapped.values())
        return [f for f in RECOMMENDED_FIELDS if f not in present]

    @property
    def is_importable(self) -> bool:
        return not self.missing_required and not self.duplicates

    def column_for(self, field_name: str) -> int | None:
        return next((i for i, f in self.mapped.items() if f == field_name), None)


def map_headers(headers: list[str]) -> HeaderMapping:
    """Work out which column is which, reporting anything ambiguous or unrecognised."""
    mapping = HeaderMapping()
    claimed: dict[str, list[int]] = {}

    for index, raw in enumerate(headers):
        mapping.headers[index] = raw
        shape = normalise(raw)
        if not shape:
            continue

        if shape in FIRST_NAME_ALIASES:
            mapping.first_name_column = index
            continue
        if shape in LAST_NAME_ALIASES:
            mapping.last_name_column = index
            continue

        field_name = _LOOKUP.get(shape)
        if field_name is None:
            # Kept, not discarded: it goes into `extra` and may feed research (issue A5).
            mapping.unmapped[index] = raw
            continue

        claimed.setdefault(field_name, []).append(index)

    for field_name, indices in claimed.items():
        if len(indices) == 1:
            mapping.mapped[indices[0]] = field_name
        else:
            # Two columns both look like `email`. Guessing which one is real is exactly the kind
            # of decision that must belong to a person.
            mapping.duplicates[field_name] = indices

    return mapping


def build_full_name(mapping: HeaderMapping, row: list[object]) -> str | None:
    """Assemble a name from a split first/last pair, if that is how the sheet is laid out."""
    if mapping.first_name_column is None:
        return None
    parts = [
        str(row[i]).strip()
        for i in (mapping.first_name_column, mapping.last_name_column)
        if i is not None and i < len(row) and row[i] is not None
    ]
    joined = " ".join(part for part in parts if part)
    return joined or None
