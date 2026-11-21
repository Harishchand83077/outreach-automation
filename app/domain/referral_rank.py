"""S19.6 — referral ranking (ADR-072).

Ranks connections for a given company by tie strength, with the reason shown.
Tie strength is the user's own declared value (1-5); when not set, the row
ranks below all rated ties. Moderately weak ties (3) are ranked first over
strong ties (5) because a weaker connection is more likely to pass a referral
along — Granovetter's strength-of-weak-ties result.

The module is pure: no I/O, no Settings, injected clock. It operates on plain
dicts so the API route and tests can supply them without constructing ORM rows.
"""

from __future__ import annotations

from dataclasses import dataclass

# Preferred order: moderate ties (3) → weak (2, 1) → strong (4, 5) → unrated.
_RANK_BY_STRENGTH: dict[int | None, int] = {
    3: 0,
    2: 1,
    1: 2,
    4: 3,
    5: 4,
    None: 5,
}


@dataclass(frozen=True)
class RankedConnection:
    """One connection with its rank and the human-readable reason."""

    connection_id: str
    full_name: str
    company: str | None
    position: str | None
    profile_url: str | None
    strength: int | None
    tags: list[str]
    relationship_note: str | None
    rank: int
    reason: str


def rank_for_company(
    connections: list[dict[str, object]],
    *,
    company_norm: str,
) -> list[RankedConnection]:
    """Return connections at a company, ranked by tie strength.

    Args:
        connections: Dicts with keys matching Connection model fields.
        company_norm: The normalised company name to filter by (case-insensitive).

    Returns:
        Ranked list, most recommendable first. Empty if no match.
    """
    matching = [
        c for c in connections
        if _norm(str(c.get("company_norm") or "")) == _norm(company_norm)
    ]

    ranked: list[RankedConnection] = []
    for c in matching:
        strength = c.get("strength")
        strength_int: int | None = int(strength) if isinstance(strength, (int, float)) else None
        rank = _RANK_BY_STRENGTH.get(strength_int, 5)
        reason = _reason(strength_int)
        tags_raw = c.get("tags")
        tags = list(tags_raw) if isinstance(tags_raw, list) else []
        ranked.append(
            RankedConnection(
                connection_id=str(c.get("id", "")),
                full_name=str(c.get("full_name", "")),
                company=str(c.get("company")) if c.get("company") else None,
                position=str(c.get("position")) if c.get("position") else None,
                profile_url=str(c.get("profile_url")) if c.get("profile_url") else None,
                strength=strength_int,
                tags=tags,
                relationship_note=(
                    str(c.get("relationship_note")) if c.get("relationship_note") else None
                ),
                rank=rank,
                reason=reason,
            )
        )

    ranked.sort(key=lambda r: r.rank)
    return ranked


def _reason(strength: int | None) -> str:
    if strength is None:
        return "Tie strength not rated. Rate it in Connections to improve ranking."
    if strength <= 2:
        return (
            f"Weak tie (strength {strength}/5) — research shows weak ties are most effective."
        )
    if strength == 3:
        return f"Moderate tie (strength {strength}/5) — a good balance of rapport and reach."
    return f"Strong tie (strength {strength}/5) — high rapport; ask directly but expect less reach."


def _norm(s: str) -> str:
    return s.strip().lower()
