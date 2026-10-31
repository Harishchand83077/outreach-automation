"""The go-live gate, as a command that answers GO or NO-GO (F1, slice S10.1).

    python -m app.scripts.preflight
    python -m app.scripts.preflight --org <uuid>

`docs/GO_LIVE.md` is the checklist a person works down. This is the half a machine can answer,
in one place, so "are we ready?" has an answer that is not somebody's memory of reading a page.

**It checks, in this order, because that is the order things go wrong:**

1. Every **sending domain** this workspace actually sends from — SPF, DKIM, DMARC, one-click —
   through `app.services.deliverability.sender_requirements`, the **same function** the admin
   screen uses. A script and a screen that disagreed about whether a domain is ready would be
   this project's signature defect in the one place somebody acts on the answer.
2. Every **mailbox**: connected, and its `from_email` matching the account that authenticates.
   This is the defect that has cost this project the most — mail went out as
   `From: rahul@arkvane.in` while SMTP authenticated as an unrelated Gmail, so the records
   verified for that domain applied to none of it, and the first reply ever received was in Spam.
3. The **send mode** and the allowlist, so nobody discovers at go-live that `live` was never set
   — or, worse, that it was.
4. The **booking link** on each campaign persona, because a link that opens the reader's own
   calendar books nothing and the live campaign had exactly that.

**It prints no secret and no recipient address.** Mailbox addresses are the workspace's own and
are shown, because an operator cannot act on "one of your mailboxes"; lead addresses, tokens and
keys appear nowhere. The structural guard in `tests/unit/test_preflight_script.py` holds that
down the way `test_mcp_smoke_script.py` does for the smoke check.

Exit code 0 on GO, 1 on NO-GO. A warning alone does not fail the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.settings import config  # noqa: E402
from app.db.models import Campaign, Mailbox, Org, SendAllowlistEntry  # noqa: E402
from app.services.deliverability.sender_requirements import (  # noqa: E402
    DnsTxtResolver,
    Verdict,
    check_domain,
)
from app.services.mail.connection import unavailable_reason  # noqa: E402
from app.services.mail.smtp import effective_send_mode  # noqa: E402

PASS = "PASS"  # noqa: S105 - a verdict, not a credential
WARN = "WARN"
FAIL = "FAIL"


@dataclass(frozen=True)
class Line:
    """One finding. `verdict` is PASS, WARN or FAIL; only FAIL stops the gate."""

    area: str
    verdict: str
    detail: str


def verdict_of(report_verdict: Verdict) -> str:
    """Map a DNS verdict onto the gate's three outcomes.

    `UNKNOWN` becomes FAIL here, not WARN, and deliberately: for a go-live decision "we could
    not tell" is not a yes. The admin screen shows the nuance; the gate has to answer.
    """
    if report_verdict is Verdict.PASS:
        return PASS
    if report_verdict is Verdict.WARN:
        return WARN
    return FAIL


def assess_booking_url(booking_url: str | None, cta_text: str | None) -> Line:
    """Whether a campaign can actually be booked with.

    A persona needs one or the other -- `CampaignSettings` has required that since Phase 2 --
    but a *present* booking URL can still be useless. The live campaign's was
    `calendar.google.com/calendar/u/0/r`, which opens the **reader's own** calendar and books
    nothing. A recipient clicking it sees their own week.
    """
    url = (booking_url or "").strip()
    if not url:
        if (cta_text or "").strip():
            return Line(
                "Booking link",
                WARN,
                "No booking URL, only a written ask. That is allowed, but a reply has to be "
                "read and answered by hand before anything is scheduled.",
            )
        return Line("Booking link", FAIL, "Neither a booking URL nor a written ask.")

    lowered = url.lower()
    # Known shapes that open the reader's own calendar rather than the sender's availability.
    own_calendar = (
        "calendar.google.com/calendar/u/",
        "calendar.google.com/calendar/r",
        "outlook.live.com/calendar/0/view",
        "outlook.office.com/calendar/view",
    )
    if any(shape in lowered for shape in own_calendar):
        return Line(
            "Booking link",
            FAIL,
            "This link opens the reader's own calendar and books nothing. A recipient clicking "
            "it sees their own week. Use a scheduling page that shows your availability.",
        )
    return Line("Booking link", PASS, f"A booking URL is set ({url}).")


def gate(lines: list[Line]) -> bool:
    """GO when nothing failed. A warning is information, not a veto."""
    return not any(line.verdict == FAIL for line in lines)


async def collect(org_id: uuid.UUID | None = None) -> list[Line]:
    """Gather every finding for one workspace, or for the oldest if none is named."""
    engine = create_async_engine(config.DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    lines: list[Line] = []
    try:
        async with maker() as session:
            org = (
                await session.get(Org, org_id)
                if org_id
                else await session.scalar(select(Org).order_by(Org.created_at).limit(1))
            )
            if org is None:
                return [Line("Workspace", FAIL, "No workspace found to check.")]
            lines.append(Line("Workspace", PASS, f"Checking {org.name}."))

            mailboxes = list(
                (await session.scalars(select(Mailbox).where(Mailbox.org_id == org.id))).all()
            )
            if not mailboxes:
                lines.append(
                    Line("Mailboxes", FAIL, "No mailbox in this workspace, so nothing can send.")
                )
            for mailbox in mailboxes:
                lines.append(await _mailbox_line(session, mailbox))

            resolver = DnsTxtResolver()
            configured = bool(config.PUBLIC_API_URL.strip())
            domains = sorted(
                {
                    mailbox.from_email.rpartition("@")[2].lower()
                    for mailbox in mailboxes
                    if "@" in (mailbox.from_email or "")
                }
            )
            for domain in domains:
                report = check_domain(domain, resolver=resolver, unsubscribe_configured=configured)
                for check in report.items:
                    lines.append(
                        Line(f"{domain} / {check.name}", verdict_of(check.verdict), check.detail)
                    )

            lines.append(await _send_mode_line(session, org))
            lines.extend(await _campaign_lines(session, org))
    finally:
        await engine.dispose()
    return lines


async def _mailbox_line(session: AsyncSession, mailbox: Mailbox) -> Line:
    """Connected, and not paused.

    **`unavailable_reason` is asked rather than re-derived.** It already answers "why can this
    mailbox not authenticate?" for the campaign screen and for `login_for`, and it never touches
    the network. A second copy of that rule here is exactly the shape of defect this project
    keeps producing -- and the gate disagreeing with the dashboard about whether a mailbox works
    would be the worst place for it.
    """
    reason = await unavailable_reason(session, mailbox=mailbox)
    if reason is not None:
        # The stored `invalid_reason` can quote a provider's reply, so the text is never echoed
        # (ADR-021) -- only the fact that there is one.
        return Line(
            f"Mailbox {mailbox.from_email}",
            FAIL,
            "This mailbox cannot authenticate. Reconnect it in Settings.",
        )
    if mailbox.paused:
        return Line(
            f"Mailbox {mailbox.from_email}",
            FAIL,
            "This mailbox is paused, so it will not send. Deliverability pauses one "
            "automatically when its bounce rate crosses the limit (ADR-034).",
        )
    return Line(f"Mailbox {mailbox.from_email}", PASS, "Connected and not paused.")


async def _send_mode_line(session: AsyncSession, org: Org) -> Line:
    mode = effective_send_mode(config, org.send_mode)
    source = "the dashboard" if org.send_mode else "the environment"
    if mode == "disabled":
        return Line(
            "Send mode",
            WARN,
            f"disabled, from {source}. Nothing will be sent at all, which is the right "
            "setting until the rest of this list is green.",
        )
    if mode == "allowlist":
        count = await session.scalar(
            select(SendAllowlistEntry).where(SendAllowlistEntry.org_id == org.id).limit(1)
        )
        detail = "with at least one address allowed" if count else "but the allowlist is EMPTY"
        return Line(
            "Send mode",
            PASS if count else FAIL,
            f"allowlist, from {source}, {detail}. Only listed addresses receive anything.",
        )
    return Line(
        "Send mode",
        WARN,
        f"LIVE, from {source}. Every approved draft goes to a real recipient.",
    )


async def _campaign_lines(session: AsyncSession, org: Org) -> list[Line]:
    campaigns = list(
        (await session.scalars(select(Campaign).where(Campaign.org_id == org.id))).all()
    )
    if not campaigns:
        return [Line("Campaigns", WARN, "No campaigns yet, so there is nothing to send.")]

    lines: list[Line] = []
    for campaign in campaigns:
        persona = campaign.persona or {}
        line = assess_booking_url(persona.get("booking_url"), persona.get("cta_text"))
        lines.append(Line(f"{campaign.name} / {line.area}", line.verdict, line.detail))
    return lines


def render(lines: list[Line]) -> str:
    width = max((len(line.area) for line in lines), default=0)
    out = [f"{line.verdict:<4}  {line.area:<{width}}  {line.detail}" for line in lines]
    out.append("")
    failed = sum(1 for line in lines if line.verdict == FAIL)
    warned = sum(1 for line in lines if line.verdict == WARN)
    if failed:
        out.append(f"NO-GO: {failed} blocking problem(s), {warned} warning(s).")
    else:
        out.append(f"GO: nothing blocking, {warned} warning(s).")
        out.append("")
        out.append(
            "A machine cannot check the last step: send one message to a Gmail address and "
            "open Show original. DKIM: PASS there is the only proof that mail is signed."
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Answer GO or NO-GO for sending real email.")
    parser.add_argument("--org", help="Workspace id. Defaults to the oldest workspace.")
    args = parser.parse_args(argv)

    org_id = uuid.UUID(args.org) if args.org else None
    lines = asyncio.run(collect(org_id))
    print(render(lines))
    return 0 if gate(lines) else 1


if __name__ == "__main__":
    raise SystemExit(main())
