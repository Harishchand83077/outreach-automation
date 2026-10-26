"""Checking that a mailbox's connection actually works, before anybody relies on it.

Asked for as *"a test email to his other address, just to check the connection is well
established"*. What that really asks is **did Connect Gmail work?**, and until now the only way
to find out was to approve a draft and wait for the send loop — which, with `SEND_MODE=disabled`,
answers nothing at all. Somebody signing up today could connect their account, see a green tick,
and have no way to tell the difference between a working connection and a broken one until their
first campaign quietly did not go out.

**Three checks, in order of how much they prove and how little they risk.**

1. **SMTP** — authenticate and hang up, before `MAIL FROM`. No envelope, no recipient, no
   message. Proves the credential is accepted by the server that sends.
2. **IMAP** — authenticate and open INBOX read-only. Proves the credential is accepted by the
   server that *reads*, which is the half that fails silently: a mailbox that cannot be read
   looks healthy while every reply, bounce and opt-out goes unseen.
3. **A real message**, optional, and **only to the mailbox's own address**.

The first two send nothing and are therefore available whatever `SEND_MODE` says — there is no
decision for the gate to make, so hard rule 1 is untouched and this is safe to put behind a
button anybody can press.

**The third one goes through `SmtpSender.send` like everything else.** It is a real email, so it
is gated like a real email: with sending disabled it comes back `blocked`, and the report says so
in a sentence rather than failing. No exception is carved out, because the value of hard rule 1
is that there are none — a "but this one is only a test" path is how the gate stops meaning
anything, and the test message is worth strictly less than the rule.

**Why only the mailbox's own address.** It needs no allowlist entry per person, it cannot be used
to mail anybody who did not ask for it, and it proves the thing a self-test can prove: that the
account can send *and* that the message arrives. Letting the caller name a recipient would make
this an open form for sending arbitrary mail from an authenticated account, which is the
prototype's worst endpoint (issue C2) with a friendlier name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings, config
from app.db.models import Mailbox, Org
from app.services.mail.connection import MailboxUnavailable, login_for
from app.services.mail.imap import ImapPoller
from app.services.mail.message import FooterIdentity, build_message
from app.services.mail.message import Mailbox as MessageMailbox
from app.services.mail.smtp import SendReport, SmtpOutcome, SmtpSender

logger = logging.getLogger(__name__)

TEST_SUBJECT = "Connection test"


@dataclass(frozen=True)
class Check:
    """One answer, and a sentence a person can act on."""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class VerificationReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        """True only when every check passed.

        A skipped test message is not a check — `send_test` adds one only when it was asked for —
        so a verification with sending disabled still reports `ok` on the strength of the two
        that matter.
        """
        return all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [check.as_dict() for check in self.checks]}


async def verify_mailbox(
    session: AsyncSession,
    *,
    mailbox: Mailbox,
    send_test: bool = False,
    settings: Settings | None = None,
    sender: SmtpSender | None = None,
    poller: ImapPoller | None = None,
    now: datetime | None = None,
) -> VerificationReport:
    """Check that this mailbox can send and be read. The caller commits.

    May write: resolving the login refreshes an expired access token, which is stored so the next
    send does not mint another. That is the same write `login_for` performs on the send path and
    not an extra side effect of checking — but it is why this is a `POST`.
    """
    settings = settings or config
    sender = sender or SmtpSender(settings)
    poller = poller or ImapPoller(settings)
    now = now or datetime.now(tz=UTC)

    try:
        login = await login_for(session, mailbox=mailbox, settings=settings, now=now)
    except MailboxUnavailable as exc:
        # The expected failure, and the one with the clearest fix. Reported as both checks
        # failing rather than as an error, because "it is not connected" is an answer to the
        # question, not a refusal to answer it.
        reason = str(exc)
        return VerificationReport(
            checks=[Check("smtp", False, reason), Check("imap", False, reason)]
        )

    smtp_report = await sender.verify(login)
    checks = [Check("smtp", smtp_report.sent, smtp_report.detail or smtp_report.outcome.value)]

    imap_ok, imap_detail = await poller.verify(login=login)
    checks.append(Check("imap", imap_ok, imap_detail))

    if send_test:
        checks.append(await _send_test_message(session, mailbox, sender, settings, now))

    logger.info(
        "mailbox verified",
        extra={
            "mailbox_id": str(mailbox.id),
            "smtp_ok": checks[0].ok,
            "imap_ok": imap_ok,
        },
    )
    return VerificationReport(checks=checks)


async def _send_test_message(
    session: AsyncSession,
    mailbox: Mailbox,
    sender: SmtpSender,
    settings: Settings,
    now: datetime,
) -> Check:
    """One real message, from this mailbox to itself, through the ordinary gate.

    `send_mode` travels with it exactly as it does for a lead, so an org that has restricted its
    own sending has restricted this too (ADR-010). Nothing here decides anything about the mode:
    it is read off the row and handed to the one function allowed to decide.
    """
    org = await session.get(Org, mailbox.org_id)
    built = build_message(
        # `build_message` takes plain values, not the ORM row: it is the most heavily tested
        # thing in this package and nothing it needs should require a session to construct.
        mailbox=MessageMailbox(
            from_name=mailbox.from_name,
            from_email=mailbox.from_email,
            reply_to=mailbox.reply_to,
        ),
        to_email=mailbox.from_email,
        subject=TEST_SUBJECT,
        body=(
            "This is an automated check that your mailbox is connected correctly.\n\n"
            "If you are reading it in the inbox of the account you connected, then sending "
            "works, the address is right, and the message is being accepted.\n\n"
            "Nobody else received this."
        ),
        identity=FooterIdentity(
            sender_name=mailbox.from_email,
            agency_name="",
            # A self-test has no recipient to offer an opt-out to, and a footer telling somebody
            # how to unsubscribe from their own connection test is nonsense. The slot is
            # overridden rather than left to default, because the default is written for a
            # stranger receiving cold email and this is not that.
            opt_out_line="Sent by your outreach dashboard to check this connection.",
        ),
        now=now,
    )

    # The login is resolved again rather than passed in: it may have been refreshed by the SMTP
    # check moments ago, and sending with a token this function captured before that would fail
    # for a reason nobody could reconstruct from the report.
    login = await login_for(session, mailbox=mailbox, settings=settings, now=now)
    report: SendReport = await sender.send(
        built,
        to_email=mailbox.from_email,
        send_mode=org.send_mode if org else None,
        login=login,
    )

    if report.outcome is SmtpOutcome.BLOCKED:
        return Check(
            "test_message",
            False,
            (
                "Sending is switched off for this workspace, so no test message was sent. "
                "The connection checks above still prove the account works."
            ),
        )
    if report.sent:
        return Check("test_message", True, f"A test message was sent to {mailbox.from_email}.")
    return Check("test_message", False, report.detail or report.outcome.value)
