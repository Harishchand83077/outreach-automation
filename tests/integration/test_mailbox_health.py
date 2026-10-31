"""Bounce auto-pause (F3, slice S11.6, ADR-034).

The thresholds matter less than the guards around them. An automatic action that stops somebody's
campaign has to be right, and the three ways it could be wrong are all cheaper to prevent than to
apologise for: pausing on noise, pausing on one bad afternoon, and alerting on every pass for
something already handled.

The most important test in this file is `test_nothing_here_can_resume_a_mailbox_or_raise_a_cap`.
The whole reason this pass is allowed to act without a human is that it can only ever restrict —
so that is asserted over the source rather than left as an intention.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Alert, Classification, Mailbox, MailboxHealth, Message
from app.services.deliverability import health

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


async def a_mailbox(db, org, *, paused: bool = False) -> Mailbox:
    row = Mailbox(
        org_id=org.id,
        from_name="Asha",
        from_email=f"asha-{uuid.uuid4().hex[:6]}@example.com",
        paused=paused,
    )
    db.add(row)
    await db.commit()
    return row


async def sent(db, org, mailbox, count: int, *, lead, when: datetime = NOW) -> None:
    """Outbound messages from this mailbox.

    `lead` is required because `ck_messages_out_has_lead` requires it -- "only inbound mail may
    be orphaned. We never send to nobody." The first version of this helper left it out and CI
    caught the violation, which is the same mistake as inventing a suppression reason: a row
    written without reading the constraint that governs it.
    """
    for index in range(count):
        db.add(
            Message(
                org_id=org.id,
                mailbox_id=mailbox.id,
                lead_id=lead.id,
                direction="out",
                kind="initial",
                subject="hello",
                body_text="hello",
                status="sent",
                sent_at=when,
                idempotency_key=f"{uuid.uuid4()}:{index}",
            )
        )
    await db.commit()


async def bounced(db, org, mailbox, count: int, *, when: datetime = NOW) -> None:
    for _ in range(count):
        inbound = Message(
            org_id=org.id,
            mailbox_id=mailbox.id,
            direction="in",
            subject="Undelivered Mail Returned to Sender",
            body_text="550 user unknown",
            status="received",
            created_at=when,
        )
        db.add(inbound)
        await db.commit()
        db.add(Classification(org_id=org.id, message_id=inbound.id, label="bounce", confidence=1.0))
        await db.commit()


async def alerts_for(db, org) -> list[Alert]:
    rows = await db.scalars(select(Alert).where(Alert.org_id == org.id))
    return list(rows)


class TestTheRollup:
    async def test_it_counts_sends_and_bounces_per_day(self, db, org, lead_factory):
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 10, lead=lead)
        await bounced(db, org, mailbox, 2)

        written = await health.roll_up(db, now=NOW)
        await db.commit()

        assert written == 1
        row = await db.scalar(select(MailboxHealth).where(MailboxHealth.mailbox_id == mailbox.id))
        assert row is not None
        assert (row.sent, row.bounced) == (10, 2)

    async def test_running_it_twice_corrects_rather_than_duplicates(self, db, org, lead_factory):
        """A day is rolled up while it is still in progress, so the second pass must correct."""
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 5, lead=lead)
        await health.roll_up(db, now=NOW)
        await db.commit()
        await sent(db, org, mailbox, 3, lead=lead)

        await health.roll_up(db, now=NOW)
        await db.commit()

        rows = list(
            (
                await db.scalars(
                    select(MailboxHealth).where(MailboxHealth.mailbox_id == mailbox.id)
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].sent == 8

    async def test_complaint_rate_is_left_null(self, db, org, lead_factory):
        """**Deliberately never written.**

        A complaint rate needs a feed from the receiving provider, and Postmaster Tools is a link
        we ask the owner to open rather than an integration (ADR-029). A zero here would read as
        "no complaints" when it means "we cannot see complaints".
        """
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 3, lead=lead)

        await health.roll_up(db, now=NOW)
        await db.commit()

        row = await db.scalar(select(MailboxHealth).where(MailboxHealth.mailbox_id == mailbox.id))
        assert row is not None and row.complaint_rate is None


class TestWhenItPauses:
    async def test_a_high_bounce_rate_pauses_the_mailbox_and_alerts(self, db, org, lead_factory):
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 40, lead=lead)
        await bounced(db, org, mailbox, 6)  # 15%, well over the limit
        await health.roll_up(db, now=NOW)
        await db.commit()

        report = await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        await db.refresh(mailbox)
        assert mailbox.paused is True
        assert report.paused_mailbox_ids == [mailbox.id]
        assert [alert.kind for alert in await alerts_for(db, org)] == [
            f"mailbox_bounce_rate:{mailbox.id}"
        ]

    async def test_the_alert_names_the_counts_and_not_an_address(self, db, org, lead_factory):
        """Counts and a rate reach logs and screenshots; an address should not."""
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 40, lead=lead)
        await bounced(db, org, mailbox, 6)
        await health.roll_up(db, now=NOW)
        await db.commit()

        report = await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        assert "6 of 40" in report.alerts[0]
        assert mailbox.from_email not in report.alerts[0]

    async def test_the_alert_kind_carries_the_mailbox_id(self, db, org, lead_factory):
        """Otherwise the first mailbox paused silences every other one for the cool-down.

        The same reasoning as the OAuth grant warnings: the mailbox that never got its alert is
        the one still bouncing.
        """
        first = await a_mailbox(db, org)
        second = await a_mailbox(db, org)
        lead = await lead_factory()
        for mailbox in (first, second):
            await sent(db, org, mailbox, 40, lead=lead)
            await bounced(db, org, mailbox, 6)
        await health.roll_up(db, now=NOW)
        await db.commit()

        await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        kinds = {alert.kind for alert in await alerts_for(db, org)}
        assert kinds == {
            f"mailbox_bounce_rate:{first.id}",
            f"mailbox_bounce_rate:{second.id}",
        }


class TestWhenItLeavesThingsAlone:
    async def test_too_few_sends_is_not_a_signal(self, db, org, lead_factory):
        """**The guard that matters most.** At five sends, one bounce is 20% and means nothing.

        Alerting on noise is how an operator learns to ignore alerts.
        """
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 5, lead=lead)
        await bounced(db, org, mailbox, 2)  # 40%, and meaningless
        await health.roll_up(db, now=NOW)
        await db.commit()

        report = await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        await db.refresh(mailbox)
        assert mailbox.paused is False
        assert report.paused_mailbox_ids == []

    async def test_a_healthy_rate_is_left_alone(self, db, org, lead_factory):
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        await sent(db, org, mailbox, 100, lead=lead)
        await bounced(db, org, mailbox, 2)  # 2%, under the limit
        await health.roll_up(db, now=NOW)
        await db.commit()

        await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        await db.refresh(mailbox)
        assert mailbox.paused is False

    async def test_an_already_paused_mailbox_is_not_alerted_about_again(
        self, db, org, lead_factory
    ):
        """The alert fires once, not every pass for something somebody is already dealing with."""
        mailbox = await a_mailbox(db, org, paused=True)
        lead = await lead_factory()
        await sent(db, org, mailbox, 40, lead=lead)
        await bounced(db, org, mailbox, 6)
        await health.roll_up(db, now=NOW)
        await db.commit()

        report = await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        assert report.paused_mailbox_ids == []
        assert await alerts_for(db, org) == []

    async def test_old_bounces_fall_out_of_the_window(self, db, org, lead_factory):
        """One bad week should not pause a mailbox for ever."""
        mailbox = await a_mailbox(db, org)
        lead = await lead_factory()
        long_ago = NOW - timedelta(days=30)
        await sent(db, org, mailbox, 40, lead=lead, when=long_ago)
        await bounced(db, org, mailbox, 20, when=long_ago)
        await health.roll_up(db, days=60, now=NOW)
        await db.commit()

        report = await health.pause_unhealthy(db, now=NOW)
        await db.commit()

        assert report.paused_mailbox_ids == []
        await db.refresh(mailbox)
        assert mailbox.paused is False


class TestItCanOnlyRestrict:
    def test_nothing_here_can_resume_a_mailbox_or_raise_a_cap(self):
        """**The reason this pass is allowed to act without a human** (ADR-034).

        A signal that can only restrict cannot cause the outage it would then report. Asserted
        over the source, because the danger is an edit made later for a good-sounding reason --
        "un-pause it once the rate recovers" is exactly the change that would turn this into
        something that can resume sending on its own.
        """
        import ast
        from pathlib import Path

        source = Path(health.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            # `paused = False` anywhere in this module would be a resume.
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == "paused":
                        assert isinstance(node.value, ast.Constant) and node.value.value is True, (
                            "this module may set paused = True and nothing else"
                        )
                    assert not (
                        isinstance(target, ast.Attribute)
                        and target.attr in {"daily_cap", "send_mode", "warmup_started_on"}
                    ), f"this module must not write {target.attr}"

        # And it must not reach the send gate at all. Comments are stripped first, because the
        # prose explaining this rule names the thing it forbids -- the same care
        # `test_mcp_safety.py` takes for the same reason.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "app.services.mail" not in code
        assert "send_mode" not in code
        assert "daily_cap" not in code
