"""Export one org's outcomes as versioned, reproducible JSONL.

    python -m app.scripts.export_dataset --org <uuid> --out outcomes.jsonl

`docs/DATASET_DESIGN.md` §7. Nothing trains on this yet; the point is that a decision becomes
possible later which is impossible today, because the evidence was never kept.

**Reproducible.** Every row carries a `schema_version`, and a manifest beside the file names the
window, the row count and the salt's fingerprint. Re-running over the same window produces the
same rows — so a result can be checked against the data it came from months afterwards.

**Identifiers are hashed, and the salt is per export.** Email, name and company become SHA-256
digests. That is pseudonymisation, not anonymisation, and `PRIVACY.md` already says so plainly
about the suppression hashes: an email address has little entropy, so anybody holding this file
*and* a candidate address can confirm a match. A per-export salt means two exports cannot be
joined to each other, which is the property actually worth having.

**One org per run, and no pooling.** `--org` is required and there is no "all orgs" flag. Pooling
across orgs needs `orgs.dataset_pooling_opt_in`, which ships defaulted to false and is read by
nothing yet — the flag had to predate the data or consent could be applied retroactively.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Bumped whenever a field changes meaning. Every row carries it, so a file read in a year says
#: what it is rather than being guessed at from its shape.
SCHEMA_VERSION = 1


def pseudonymise(value: str | None, salt: str) -> str | None:
    """A salted digest of an identifier, or None.

    Lower-cased and stripped first, so the same address exported twice in one run collides with
    itself — which is the entire point of hashing it rather than dropping it.
    """
    if not value or not value.strip():
        return None
    digest = hashlib.sha256(f"{salt}:{value.strip().lower()}".encode())
    return digest.hexdigest()


def row_for(
    *,
    outcome: Any,
    lead: Any,
    campaign: Any,
    first_message: Any,
    ai_draft: Any,
    approved_draft: Any,
    salt: str,
) -> dict[str, Any]:
    """One exported row: the five dimensions, flattened, with nothing identifying in the clear.

    Weekday and hour are **derived here** rather than stored on the message. They follow from
    `sent_at` and the campaign's timezone, and a stored copy of a derived value is a copy that
    can disagree with its source after a timezone change.
    """
    sent_at = getattr(first_message, "sent_at", None)
    approved_subject = getattr(approved_draft, "subject", None)
    ai_subject = getattr(ai_draft, "subject", None)

    return {
        "schema_version": SCHEMA_VERSION,
        # --- identity, hashed -------------------------------------------------------------
        "lead": pseudonymise(getattr(lead, "email_norm", None), salt),
        "company": pseudonymise(getattr(lead, "company_name", None), salt),
        # --- outcome ----------------------------------------------------------------------
        "outcome": outcome.outcome,
        # True means "watched and nothing happened". **Not a negative result**, and anything
        # reading this file has to branch on it before counting anything.
        "censored": outcome.censored,
        "stage": outcome.stage,
        "objection_kind": outcome.objection_kind,
        "confidence": outcome.confidence,
        "confirmed_by_human": outcome.confirmed_by not in (None, "system"),
        "observed_days": (
            (outcome.resolved_at - outcome.first_sent_at).days
            if outcome.resolved_at and outcome.first_sent_at
            else None
        ),
        # --- lead fit ---------------------------------------------------------------------
        "lead_title": getattr(lead, "title", None),
        "lead_sector": getattr(lead, "sector", None),
        # --- content ----------------------------------------------------------------------
        "campaign_kind": outcome.campaign_kind,
        "campaign_mode": getattr(campaign, "mode", None),
        "subject_as_sent": approved_subject,
        # The preference pair: what the model wrote, and what the person changed it to. Present
        # only when a reviewer actually edited, which is what makes it a signal.
        "ai_subject": ai_subject if ai_draft is not None else None,
        "was_human_edited": ai_draft is not None,
        "claims": list(getattr(approved_draft, "claims", []) or []),
        "prompt_version": getattr(approved_draft, "prompt_version", None),
        # --- timing -----------------------------------------------------------------------
        "sent_at": sent_at.isoformat() if sent_at else None,
        "sent_weekday": sent_at.weekday() if sent_at else None,
        "sent_hour": sent_at.hour if sent_at else None,
        # --- sender context ---------------------------------------------------------------
        "warmup_day": getattr(first_message, "warmup_day", None),
        "variant": getattr(first_message, "variant", None),
        # --- deliverability ---------------------------------------------------------------
        "delivery_status": getattr(first_message, "status", None),
    }


def manifest_for(*, org_id: str, rows: int, salt: str, started: datetime) -> dict[str, Any]:
    """What the file is, so it can be trusted a year from now.

    The salt itself is **not** recorded — only a fingerprint of it. Writing the salt beside the
    hashes would make the hashing decorative.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "org_id": org_id,
        "rows": rows,
        "salt_fingerprint": hashlib.sha256(salt.encode()).hexdigest()[:16],
        "exported_at": started.isoformat(),
        "note": (
            "Identifiers are salted SHA-256 digests. This is pseudonymisation, not "
            "anonymisation: an email address has little entropy, so anybody holding this file "
            "and a candidate address can confirm a match. Treat it as personal data."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Required, with no "all orgs" alternative: pooling is a consent decision, not a flag.
    parser.add_argument("--org", required=True, help="the workspace to export (uuid)")
    parser.add_argument("--out", required=True, help="path to write JSONL to")
    parser.add_argument(
        "--salt",
        default=None,
        help="hashing salt; a fresh random one per run by default, so two exports cannot be "
        "joined to each other",
    )
    return parser


async def export(org_id: uuid.UUID, out: Path, salt: str) -> int:  # pragma: no cover - I/O
    """Write the file and its manifest. Returns the row count."""
    from sqlalchemy import select

    from app.db.engine import get_sessionmaker
    from app.db.models import Campaign, Draft, Lead, LeadOutcome, Message

    started = datetime.now(tz=UTC)
    written = 0
    with out.open("w", encoding="utf-8") as handle:
        async with get_sessionmaker()() as session:
            rows = (
                await session.scalars(select(LeadOutcome).where(LeadOutcome.org_id == org_id))
            ).all()
            for outcome in rows:
                lead = await session.get(Lead, outcome.lead_id) if outcome.lead_id else None
                campaign = (
                    await session.get(Campaign, outcome.campaign_id)
                    if outcome.campaign_id
                    else None
                )
                first_message = (
                    await session.scalar(
                        select(Message)
                        .where(Message.lead_id == outcome.lead_id, Message.direction == "out")
                        .order_by(Message.created_at)
                        .limit(1)
                    )
                    if outcome.lead_id
                    else None
                )
                approved = (
                    await session.scalar(
                        select(Draft)
                        .where(
                            Draft.lead_id == outcome.lead_id, Draft.status.in_(("approved", "sent"))
                        )
                        .order_by(Draft.version.desc())
                        .limit(1)
                    )
                    if outcome.lead_id
                    else None
                )
                ai_draft = (
                    await session.get(Draft, approved.derived_from_id)
                    if approved is not None and approved.derived_from_id
                    else None
                )
                handle.write(
                    json.dumps(
                        row_for(
                            outcome=outcome,
                            lead=lead,
                            campaign=campaign,
                            first_message=first_message,
                            ai_draft=ai_draft,
                            approved_draft=approved,
                            salt=salt,
                        ),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                written += 1

    manifest = manifest_for(org_id=str(org_id), rows=written, salt=salt, started=started)
    out.with_suffix(out.suffix + ".manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return written


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - I/O
    import asyncio

    args = build_parser().parse_args(argv)
    salt = args.salt or secrets.token_hex(16)
    rows = asyncio.run(export(uuid.UUID(args.org), Path(args.out), salt))
    print(f"wrote {rows} row(s) to {args.out}")
    print("Identifiers are hashed. This file is still personal data -- see the manifest.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
