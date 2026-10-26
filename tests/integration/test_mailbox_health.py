"""Does this mailbox work, and will it still work on Friday?

Two questions that had no answer before. "Did Connect Gmail work?" could only be answered by
approving a draft and waiting for the send loop -- which, with sending disabled, answers nothing.
And "when does this connection lapse?" could only be answered after it had, by a paused mailbox
and a campaign that stopped over a weekend.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from app.api.routers.connect import google_status
from app.core.settings import Settings
from app.db.models import Alert, Mailbox, MailCredential, Org
from app.services.credentials import seal
from app.services.mail.expiry import warn_about_expiring_grants
from app.services.mail.smtp import SendReport, SmtpOutcome
from app.services.mail.verify import verify_mailbox

pytestmark = pytest.mark.asyncio(loop_scope="session")

KEY = Fernet.generate_key().decode("ascii")
#: Just the operator's own address. ADR-023: it is not special any more -- there is no
#: environment account, so this mailbox is connected exactly like anybody else's.
ENV_ADDRESS = "operator@outreach.example"


def settings_with(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


class RecordingSender:
    """Stands in for SMTP. Records what it was asked to do and never opens a socket."""

    def __init__(self, *, verify_ok: bool = True, send: SendReport | None = None) -> None:
        self._verify_ok = verify_ok
        self._send = send or SendReport(outcome=SmtpOutcome.SENT)
        self.verified: list[object] = []
        self.sent_to: list[str] = []

    async def verify(self, login=None):
        self.verified.append(login)
        if self._verify_ok:
            return SendReport(outcome=SmtpOutcome.SENT, detail="accepted")
        return SendReport(outcome=SmtpOutcome.AUTH_FAILED, detail="refused the credentials")

    async def send(self, built, *, to_email, send_mode=None, allowed=None, login=None):
        self.sent_to.append(to_email)
        return self._send


class RecordingPoller:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok
        self.verified: list[object] = []

    async def verify(self, *, login=None):
        self.verified.append(login)
        return (self._ok, "accepted" if self._ok else "refused the credentials")


async def _mailbox(db, org, address: str) -> Mailbox:
    mailbox = Mailbox(org_id=org.id, from_name="Asha Rao", from_email=address, reply_to=address)
    db.add(mailbox)
    await db.commit()
    return mailbox


async def _connect(
    db, org, mailbox, *, kind="google_oauth", granted_at=None, invalid_reason=None
) -> MailCredential:
    credential = MailCredential(
        org_id=org.id,
        mailbox_id=mailbox.id,
        kind=kind,
        account_email=mailbox.from_email,
        secret=seal("refresh-token", settings_with()).token,
        granted_at=granted_at or datetime.now(tz=UTC),
        invalid_reason=invalid_reason,
    )
    db.add(credential)
    await db.commit()
    return credential


class TestCheckingAConnection:
    async def test_an_unconnected_mailbox_says_so_rather_than_erroring(self, db, org):
        """A stranger's mailbox with no credential. Not an error -- an answer."""
        stranger = Org(name="Someone Else")
        db.add(stranger)
        await db.commit()
        mailbox = await _mailbox(db, stranger, "priya@example.com")

        report = await verify_mailbox(
            db,
            mailbox=mailbox,
            settings=settings_with(),
            sender=RecordingSender(),
            poller=RecordingPoller(),
        )

        assert not report.ok
        assert [check.name for check in report.checks] == ["smtp", "imap"]
        assert all("has not been connected" in check.detail for check in report.checks)

    async def test_both_halves_are_checked_separately(self, db, org):
        """SMTP working proves nothing about IMAP, and the reading half fails silently."""
        mailbox = await _mailbox(db, org, ENV_ADDRESS)
        await _connect(db, org, mailbox, kind="app_password")
        sender, poller = RecordingSender(), RecordingPoller(ok=False)

        report = await verify_mailbox(
            db, mailbox=mailbox, settings=settings_with(), sender=sender, poller=poller
        )

        assert not report.ok
        by_name = {check.name: check for check in report.checks}
        assert by_name["smtp"].ok
        assert not by_name["imap"].ok
        assert len(poller.verified) == 1

    async def test_a_healthy_mailbox_passes_and_sends_nothing(self, db, org):
        mailbox = await _mailbox(db, org, ENV_ADDRESS)
        await _connect(db, org, mailbox, kind="app_password")
        sender = RecordingSender()

        report = await verify_mailbox(
            db, mailbox=mailbox, settings=settings_with(), sender=sender, poller=RecordingPoller()
        )

        assert report.ok
        # **The whole reason this is safe to expose as a button.** No message exists, so
        # SEND_MODE has nothing to decide and hard rule 1 is untouched.
        assert sender.sent_to == []

    async def test_the_connected_account_is_what_authenticates(self, db, org):
        """Not the environment's. ADR-020 in the check as well as in the send path."""
        stranger = Org(name="Priya")
        db.add(stranger)
        await db.commit()
        mailbox = await _mailbox(db, stranger, "priya@example.com")
        # An app password rather than an OAuth grant, so resolving the login needs no round trip
        # to Google -- hard rule: never the network in a test. What is being asserted is which
        # credential was chosen, and that is the same decision either way.
        await _connect(db, stranger, mailbox, kind="app_password")

        sender = RecordingSender()
        await verify_mailbox(
            db, mailbox=mailbox, settings=settings_with(), sender=sender, poller=RecordingPoller()
        )

        login = sender.verified[0]
        assert login is not None
        assert login.username == "priya@example.com"


class TestTheTestMessage:
    async def test_it_goes_to_the_mailbox_itself_and_nowhere_else(self, db, org):
        mailbox = await _mailbox(db, org, ENV_ADDRESS)
        await _connect(db, org, mailbox, kind="app_password")
        sender = RecordingSender()

        report = await verify_mailbox(
            db,
            mailbox=mailbox,
            send_test=True,
            settings=settings_with(),
            sender=sender,
            poller=RecordingPoller(),
        )

        assert sender.sent_to == [ENV_ADDRESS]
        assert report.ok

    async def test_a_blocked_send_is_reported_not_raised(self, db, org):
        """`SEND_MODE=disabled` is the default, and a test message is a real email.

        No exception is carved out for it. The report says sending is off, and the two checks
        that prove the account works are unaffected -- which is the useful answer anyway.
        """
        mailbox = await _mailbox(db, org, ENV_ADDRESS)
        await _connect(db, org, mailbox, kind="app_password")
        sender = RecordingSender(send=SendReport(outcome=SmtpOutcome.BLOCKED, detail="disabled"))

        report = await verify_mailbox(
            db,
            mailbox=mailbox,
            send_test=True,
            settings=settings_with(),
            sender=sender,
            poller=RecordingPoller(),
        )

        test_check = next(c for c in report.checks if c.name == "test_message")
        assert not test_check.ok
        assert "Sending is switched off" in test_check.detail

    async def test_nothing_is_sent_unless_it_was_asked_for(self, db, org):
        mailbox = await _mailbox(db, org, ENV_ADDRESS)
        await _connect(db, org, mailbox, kind="app_password")
        sender = RecordingSender()

        await verify_mailbox(
            db, mailbox=mailbox, settings=settings_with(), sender=sender, poller=RecordingPoller()
        )

        assert sender.sent_to == []


class TestWarningBeforeItLapses:
    """Day 6 and day 7 of a seven-day grant, which is what the owner asked for."""

    async def _warn(self, db, **kw):
        raised = await warn_about_expiring_grants(
            db, settings=settings_with(), notifier=_silent, **kw
        )
        await db.commit()
        return raised

    async def test_nothing_is_said_early(self, db, org):
        mailbox = await _mailbox(db, org, f"early-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, mailbox, granted_at=datetime.now(tz=UTC) - timedelta(days=2))

        assert await self._warn(db) == []

    async def test_the_day_before_raises_one(self, db, org):
        mailbox = await _mailbox(db, org, f"soon-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, mailbox, granted_at=datetime.now(tz=UTC) - timedelta(days=6))

        raised = await self._warn(db)

        assert raised == [f"mailbox_grant_expiring:{mailbox.id}:1"]
        alert = await db.scalar(
            Alert.__table__.select().where(Alert.kind == raised[0])  # type: ignore[arg-type]
        )
        assert alert is not None

    async def test_an_expired_grant_is_critical(self, db, org):
        mailbox = await _mailbox(db, org, f"gone-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, mailbox, granted_at=datetime.now(tz=UTC) - timedelta(days=8))

        raised = await self._warn(db)

        assert raised == [f"mailbox_grant_expiring:{mailbox.id}:0"]

    async def test_the_same_warning_is_not_repeated_every_hour(self, db, org):
        mailbox = await _mailbox(db, org, f"twice-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, mailbox, granted_at=datetime.now(tz=UTC) - timedelta(days=6))

        first = await self._warn(db)
        second = await self._warn(db)

        assert first and second == []

    async def test_one_mailbox_warning_does_not_silence_another(self, db, org):
        """The cool-down dedupes by kind, so the mailbox id has to be *in* the kind.

        Without it the first mailbox warned would mute every other mailbox in the deployment
        for twenty hours -- and the one that never got its warning is the one that stops.
        """
        granted = datetime.now(tz=UTC) - timedelta(days=6)
        one = await _mailbox(db, org, f"one-{uuid.uuid4().hex[:8]}@example.com")
        two = await _mailbox(db, org, f"two-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, one, granted_at=granted)
        await _connect(db, org, two, granted_at=granted)

        raised = await self._warn(db)

        assert {f"mailbox_grant_expiring:{one.id}:1", f"mailbox_grant_expiring:{two.id}:1"} <= set(
            raised
        )

    async def test_a_mailbox_already_known_to_be_broken_is_not_warned_about(self, db, org):
        """The dashboard is already showing Reconnect. A second sentence adds only noise."""
        mailbox = await _mailbox(db, org, f"dead-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(
            db,
            org,
            mailbox,
            granted_at=datetime.now(tz=UTC) - timedelta(days=8),
            invalid_reason="Google would not renew this connection.",
        )

        assert await self._warn(db) == []


async def _silent(alert) -> None:
    """A notifier that pushes nowhere. Hard rule: never the network in a test."""
    return None


@dataclass
class _User:
    """Only `org_id` is read by the status route. ADR-004: never from the caller."""

    org_id: uuid.UUID


class TestWhatTheDashboardIsTold:
    async def test_the_status_route_says_when_the_connection_lapses(self, db, org):
        """Before this, the dashboard learned about the seven days by hitting them."""
        mailbox = await _mailbox(db, org, f"status-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, mailbox, granted_at=datetime.now(tz=UTC) - timedelta(days=6))

        body = await google_status(_User(org.id), db, mailbox.id)

        assert body["days_left"] == 1
        assert body["expires_at"] is not None
        assert "expires in 1 day" in body["expiry_notice"]

    async def test_a_fresh_connection_is_not_nagged_about(self, db, org):
        mailbox = await _mailbox(db, org, f"fresh-{uuid.uuid4().hex[:8]}@example.com")
        await _connect(db, org, mailbox, granted_at=datetime.now(tz=UTC))

        body = await google_status(_User(org.id), db, mailbox.id)

        assert body["days_left"] == 7
        assert body["expiry_notice"] is None

    async def test_a_mailbox_with_no_credential_has_no_countdown(self, db, org):
        mailbox = await _mailbox(db, org, f"none-{uuid.uuid4().hex[:8]}@example.com")

        body = await google_status(_User(org.id), db, mailbox.id)

        assert body["days_left"] is None
        assert body["expiry_notice"] is None
