"""What one piece of text asserts that another does not. No I/O (§4).

**Extracted rather than copied, because a second caller appeared.** ADR-043 wrote these three
functions to stop a model inventing a skill, a product or an employer while rewording a resume
fact. S15.3's brief needs exactly the same question answered about exactly the same kind of
edit -- a model is handed a sentence and may say it better, and must not say more. Two copies of
this rule is this project's signature defect in the one place where a wrong answer is a sentence
somebody repeats to a stranger, so the rule lives here and both callers ask it.

The check is deliberately crude in a recoverable direction: it catches a *named* thing or a
number that was not in the source, and it cannot catch a lower-case exaggeration ("led a team"
from a fact about writing code). That limit is stated in ADR-043 and is unchanged; what makes it
acceptable in both callers is that a person reads the result before it goes anywhere.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d[\d,_.]*")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+#./_-]*")
_NAMED_PUNCTUATION = re.compile(r"[0-9+#.]")


def numbers(text: str) -> set[str]:
    """Numbers in a string, normalised so the same quantity written two ways is one value.

    "2,000" and "2000" are one number, and "42%" out of nowhere is not in the source.
    """
    found: set[str] = set()
    for raw in _NUMBER.findall(text or ""):
        value = raw.replace(",", "").replace("_", "").rstrip(".")
        if value:
            found.add(value)
    return found


def tokens(text: str) -> set[str]:
    return {match.group(0).lower().strip(".") for match in _TOKEN.finditer(text or "")}


def named(text: str) -> set[str]:
    """Tokens that name a thing rather than describe one.

    Capitalised, all-capitals, or carrying an internal digit or `+`, `#`, `.` -- which between
    them cover the inventions that matter: a skill, a product, an employer, a funding round.
    Position in the sentence is deliberately **not** used: exempting the first word would leave
    a line that opens on a brand new technology unchecked, and that is precisely the line
    somebody would write. The cost is that a rewording must begin with a word the source
    already uses, which is a real constraint in the harmless direction.
    """
    found: set[str] = set()
    for match in _TOKEN.finditer(text or ""):
        token = match.group(0)
        body = token[1:]
        if token[0].isupper() or token.isupper() or _NAMED_PUNCTUATION.search(body):
            found.add(token.lower().strip("."))
    return found


def invented(reworded: str, *, source: str) -> tuple[list[str], list[str]]:
    """`(numbers, named things)` in `reworded` that `source` does not contain.

    Two lists rather than one, because the two refusals read differently to whoever has to act
    on them: a number nobody confirmed and a thing nobody mentioned are different mistakes.
    """
    return (
        sorted(numbers(reworded) - numbers(source)),
        sorted(named(reworded) - tokens(source)),
    )
