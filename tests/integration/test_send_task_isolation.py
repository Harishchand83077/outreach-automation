"""One campaign with unusable settings must not stop sending for every other mailbox.

`campaigns.settings` is JSONB. Nothing in the API writes it today, so the only way a value gets
there is a hand-written `UPDATE` — which is exactly how it went wrong in production: a widened
send window was applied with `send_days` as `["mon","tue",...]` where `CampaignSettings` wants
integers (0 = Monday). `CampaignSettings.model_validate` raised inside `SendLoop._send_for`, the
exception escaped `tick()`, and `WorkerTask.run` recorded a failure and backed off.

The loop recovered, retried, and failed again every cycle. Nothing was sent, from any mailbox,
for as long as that one row stayed malformed — and the reason was visible only in a traceback
in the worker log, not in anything the dashboard shows.

Per-mailbox isolation is the fix: a campaign whose settings cannot be read is one mailbox's
problem, reported as that mailbox's reason, and the remaining mailboxes still send.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.core.settings import config
from app.db.models import Campaign, Mailbox
from app.worker.context import WorkerContext
from app.worker.send_task import SendLoop

pytestmark = pytest.mark.asyncio(loop_scope="session")

IST = ZoneInfo("Asia/Kolkata")
#: A Monday, 11:00 IST — inside the default window, so nothing else explains a refusal.
INSIDE = datetime(2026, 10, 12, 11, 0, tzinfo=IST).astimezone(UTC)

PERSONA = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Acme Software",
    "offer": "Fixed-scope data pipeline projects",
    "goal": "book a 20-minute call",
    "booking_url": "https://cal.example.com/asha",
}


class FakePoller:
    async def find_by_message_id(self, message_id, *, folders=()):
        return None


class RefusingSender:
    """Fails the test if the loop ever tries to send. Nothing here is sendable."""

    async def send(self, built, *, to_email):  # pragma: no cover - must never run
        raise AssertionError(f"nothing should have been sent, but a message went to {to_email}")


@pytest_asyncio.fixture(loop_scope="session")
async def ctx(session_factory):
    return WorkerContext(
        session_factory=session_factory,
        settings=config,
        poller=FakePoller(),  # type: ignore[arg-type]
        sender=RefusingSender(),  # type: ignore[arg-type]
    )


async def _mailbox_with_settings(db, org, settings: dict, *, label: str) -> Mailbox:
    """A mailbox and the one campaign that sends for it, carrying `settings` verbatim."""
    mailbox = Mailbox(
        id=uuid.uuid4(),
        org_id=org.id,
        from_name="Asha Rao",
        from_email=f"{label}@outreach.example",
    )
    db.add(mailbox)
    await db.flush()
    db.add(
        Campaign(
            id=uuid.uuid4(),
            org_id=org.id,
            mailbox_id=mailbox.id,
            name=f"campaign-{label}",
            status="running",
            persona=PERSONA,
            settings=settings,
        )
    )
    await db.commit()
    return mailbox


class TestUnreadableCampaignSettings:
    async def test_a_tick_does_not_raise(self, db, ctx, org):
        """The production symptom: `send_days` as weekday names instead of integers."""
        await _mailbox_with_settings(
            db, org, {"send_days": ["mon", "tue"], "window_end": "23:59:00"}, label="broken"
        )

        report = await SendLoop(ctx).send_pass(INSIDE)

        assert report.sent == 0
        assert any("settings" in reason for reason in report.reasons), report.reasons

    async def test_the_reason_names_the_mailbox_and_the_field(self, db, ctx, org):
        """A traceback in a log is not a diagnosis. The reason has to say which field."""
        await _mailbox_with_settings(db, org, {"send_days": ["mon"]}, label="named")

        report = await SendLoop(ctx).send_pass(INSIDE)

        reason = next(r for r in report.reasons if r.startswith("named@outreach.example"))
        assert "send_days" in reason

    async def test_an_unrelated_mailbox_still_gets_its_turn(self, db, ctx, org):
        """The defect that mattered: one bad row stopped sending for everybody."""
        await _mailbox_with_settings(db, org, {"send_days": ["mon"]}, label="broken")
        await _mailbox_with_settings(db, org, {}, label="healthy")

        report = await SendLoop(ctx).send_pass(INSIDE)

        # Both were considered. The healthy one got as far as having nothing to send, which is
        # the normal refusal — not "we never looked".
        assert report.mailboxes == 2
        assert any(
            r.startswith("healthy@outreach.example") and "nothing is ready" in r
            for r in report.reasons
        ), report.reasons
