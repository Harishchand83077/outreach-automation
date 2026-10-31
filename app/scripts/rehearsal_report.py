"""What a rehearsal actually did (F1, slice S10.3).

    python -m app.scripts.rehearsal_report --since 2026-10-01
    python -m app.scripts.rehearsal_report --since 2026-10-01 --org <uuid>

The go-live gate answers "may we send?". This answers "what happened when we did?" — every
outbound message in a window, whether its recipient was on the allowlist, and anything that got
out while not on it, which must be zero.

Recipients are shown because the whole point is to check them, and this is the operator's own
workspace. Nothing else is: no subject, no body, no token.

Exit code 0 when every recipient was allowed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.settings import config  # noqa: E402
from app.db.models import Message, Org, SendAllowlistEntry  # noqa: E402


def _parse_since(raw: str) -> datetime:
    moment = datetime.fromisoformat(raw)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def gather(since: datetime, org_id: uuid.UUID | None) -> tuple[list[str], dict[str, int]]:
    """Report lines and a tally. Returns `(lines, counts)`."""
    engine = create_async_engine(config.DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    lines: list[str] = []
    counts = {"sent": 0, "failed": 0, "other": 0, "off_allowlist": 0}
    try:
        async with maker() as session:
            org = (
                await session.get(Org, org_id)
                if org_id
                else await session.scalar(select(Org).order_by(Org.created_at).limit(1))
            )
            if org is None:
                return ["No workspace found."], counts

            allowed = {
                (entry.email_norm or "").strip().lower()
                for entry in (
                    await session.scalars(
                        select(SendAllowlistEntry).where(SendAllowlistEntry.org_id == org.id)
                    )
                ).all()
            }
            messages = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(
                            Message.org_id == org.id,
                            Message.direction == "out",
                            Message.created_at >= since,
                        )
                        .order_by(Message.created_at)
                    )
                ).all()
            )

            lines.append(f"Workspace: {org.name}")
            lines.append(f"Allowlist: {len(allowed)} address(es)")
            lines.append(f"Outbound messages since {since.isoformat()}: {len(messages)}")
            lines.append("")

            for message in messages:
                recipient = (message.to_addr or "").strip().lower()
                on_list = recipient in allowed
                if message.status == "sent":
                    counts["sent"] += 1
                elif message.status == "failed":
                    counts["failed"] += 1
                else:
                    counts["other"] += 1
                # Only a *delivered* message to an unlisted address is a breach. A blocked one
                # is the gate working, and is what a rehearsal in allowlist mode should be full
                # of.
                breach = message.status == "sent" and not on_list
                if breach:
                    counts["off_allowlist"] += 1
                mark = "OFF-LIST" if breach else ("on list" if on_list else "-")
                when = message.created_at.isoformat(timespec="seconds")
                lines.append(
                    f"{when}  {message.status:<8}  {mark:<9}  {message.kind or '?':<8}  "
                    f"{message.to_addr or '(no recipient)'}"
                )
    finally:
        await engine.dispose()
    return lines, counts


def render(lines: list[str], counts: dict[str, int]) -> str:
    out = list(lines)
    out.append("")
    out.append(
        f"sent {counts['sent']}, failed or blocked {counts['failed']}, other {counts['other']}"
    )
    if counts["off_allowlist"]:
        out.append(
            f"FAIL: {counts['off_allowlist']} message(s) were delivered to an address that was "
            "not on the allowlist."
        )
    else:
        out.append("OK: every delivered message went to an allowlisted address.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report what a rehearsal sent, and to whom.")
    parser.add_argument("--since", required=True, help="ISO date or timestamp, e.g. 2026-10-01")
    parser.add_argument("--org", help="Workspace id. Defaults to the oldest workspace.")
    args = parser.parse_args(argv)

    lines, counts = asyncio.run(
        gather(_parse_since(args.since), uuid.UUID(args.org) if args.org else None)
    )
    print(render(lines, counts))
    return 1 if counts["off_allowlist"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
