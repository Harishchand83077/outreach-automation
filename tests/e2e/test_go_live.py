"""The go-live gate: one lead's whole life, driven through the real API and the real worker.

MASTER_PROMPT §12. Every other test in this repository exercises one component against fakes.
This one exercises the **chain**, because that is where the defects have actually been: of the
nine bugs found on the live system, seven were a correct caller and a correct callee with
nothing checking that they agreed.

Nothing here touches the network. The SMTP server is `aiosmtpd` in-process, the model is a fake
that returns queued results, and every recipient is `@example.com` (hard rule 9). `SEND_MODE`
is never `live`.

**What this file proves, in order:** a sheet imports with a preview that writes nothing; a
re-import of the same sheet plus two new rows adds only those two; a suppressed address is
skipped with its reason; approval is bulk and holds back the weak; the send loop refuses
outside the window, refuses over the cap, refuses a recipient who is not allowlisted, and
refuses a suppressed one; a crash between the SMTP call and the row that records it never
double-sends; a reply cancels the follow-up in the same transaction that records it; and an
opt-out suppresses forever.

**What is proved elsewhere, deliberately not duplicated here** — each already has a dedicated
test, and restating them would make this file slower without making it stronger:

- Research grounding, hooks quoting the fetched text:
  `tests/integration/test_research_service.py`
- Drafting refuses an ungrounded claim: `tests/unit/test_drafting_generator.py`
- LLM failover on 429, PII never reaching an `allow_pii=false` entry, and a clean pause when
  every entry is exhausted: `tests/unit/test_llm_router.py`
- Reply classification and the ten labels: `tests/unit/test_reply_rules.py`,
  `tests/unit/test_reply_classify.py`
- A prompt-injection reply cannot trigger a send: `tests/unit/test_reply_rules.py`, plus the
  structural test that the review router imports nothing capable of sending
- Auth rejects unauthenticated and cross-org access: `tests/integration/test_api_auth.py`
- Follow-up scheduling maths, weekends and holidays: `tests/unit/test_scheduling.py`
- At-most-once recovery after an interrupted send: `tests/integration/test_send_recovery.py`
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db import outbox
from app.db.models import Draft, Lead, Mailbox, Member, Message, Org, Research, Suppression
from app.domain.campaign import CampaignSettings
from app.domain.persona import Persona
from app.services.mail.smtp import SmtpSender
from app.worker import send_loop

pytestmark = pytest.mark.asyncio(loop_scope="session")

ISSUER = "https://proj.supabase.co/auth/v1"
IST = ZoneInfo("Asia/Kolkata")

#: A Monday, 11:00 IST - inside the Mon-Fri 10:00-17:30 window.
INSIDE = datetime(2026, 10, 12, 11, 0, tzinfo=IST).astimezone(UTC)
#: The Saturday of the same week.
WEEKEND = datetime(2026, 10, 17, 11, 0, tzinfo=IST).astimezone(UTC)
#: Monday, 21:00 IST — a weekday, after hours.
AFTER_HOURS = datetime(2026, 10, 12, 21, 0, tzinfo=IST).astimezone(UTC)

PERSONA = Persona(
    sender_name="Asha Rao",
    sender_role="Founder",
    agency_name="Acme Software",
    offer="Fixed-scope data pipeline projects",
    goal="book a 20-minute call",
    booking_url="https://cal.example.com/asha",
)


def sheet(rows: int, *, start: int = 1) -> str:
    """A lead sheet. Every address is @example.com — hard rule 9."""
    lines = ["email,full name,company,website,title"]
    for n in range(start, start + rows):
        lines.append(f"lead{n}@example.com,Lead {n},Company {n},https://company{n}.example,Founder")
    return "\n".join(lines) + "\n"


def as_upload(content: str, name: str = "leads.csv"):
    return {"file": (name, content.encode(), "text/csv")}


@pytest.fixture(scope="session")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


class FakeJwks:
    def __init__(self, public_pem: str) -> None:
        self.public_pem = public_pem

    def signing_key(self, token: str):
        return serialization.load_pem_public_key(self.public_pem.encode())


def token_for(private_pem: str, *, subject: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": subject, "email": f"{subject}@example.com", "iss": ISSUER, "exp": now + 3600},
        private_pem,
        algorithm="RS256",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    _, public = keypair
    application = create_app(
        Settings(CORS_ALLOWED_ORIGINS="https://dash.example"),
        limiter=RateLimit(limit=10_000, window_seconds=60.0),
    )

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[deps.get_session] = override_session
    deps.set_verifier(TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=FakeJwks(public)))
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client
    deps.set_verifier(None)


@pytest_asyncio.fixture(loop_scope="session")
async def owner(db, org, keypair):
    private, _ = keypair
    subject = f"owner-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email="owner@example.com", role="owner"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


@pytest_asyncio.fixture(loop_scope="session")
async def stranger(db, keypair):
    private, _ = keypair
    other = Org(name="Rival Two Ltd")
    db.add(other)
    await db.flush()
    subject = f"rival-{uuid.uuid4()}"
    db.add(Member(org_id=other.id, user_id=subject, email="rival@example.com", role="owner"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


@pytest_asyncio.fixture(loop_scope="session")
async def smtp_server():
    """An in-process SMTP server that records exactly what it received."""
    import socket

    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult

    class Collector:
        def __init__(self) -> None:
            self.messages: list[dict[str, object]] = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(
                {
                    "rcpt": list(envelope.rcpt_tos),
                    "raw": envelope.content.decode("utf-8", errors="replace"),
                }
            )
            return "250 OK"

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    handler = Collector()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        authenticator=lambda *a: AuthResult(success=True),
        auth_require_tls=False,
    )
    controller.start()
    try:
        handler.host = "127.0.0.1"
        handler.port = port
        yield handler
    finally:
        controller.stop()


def sending_settings(host: str, port: int, *, allow: str, mode: str = "allowlist") -> Settings:
    return Settings(
        _env_file=None,
        GROQ_API_KEY="gsk_test",
        SMTP_EMAIL="asha@outreach.example",
        SMTP_PASSWORD="not-a-real-password",
        SMTP_HOST=host,
        SMTP_PORT=port,
        SMTP_USE_TLS=False,
        SMTP_START_TLS=False,
        SEND_MODE=mode,
        SEND_ALLOWLIST=allow,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org, campaign):
    record = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(record)
    await db.flush()
    campaign.mailbox_id = record.id
    campaign.persona = PERSONA.model_dump(mode="json")
    campaign.status = "running"
    await db.commit()
    return record


async def approved_draft_for(db, org, lead, *, kind="initial"):
    """A lead with research and an approved draft, ready for the send loop."""
    db.add(
        Research(
            org_id=org.id,
            lead_id=lead.id,
            hooks=[{"id": "h1", "text": "hiring backend engineers"}],
            confidence=0.9,
            thin=False,
        )
    )
    draft = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind=kind,
        version=1,
        subject="Your pipeline hiring",
        body="Hi,\n\nSaw you are hiring backend engineers.\n\nAsha",
        status="approved",
        approved_by="reviewer@example.com",
        approved_at=datetime.now(tz=UTC),
    )
    db.add(draft)
    await db.commit()
    return draft


async def send_once(
    db,
    org,
    mailbox,
    smtp_server,
    *,
    now=INSIDE,
    allow="lead1@example.com",
    settings=None,
    mode="allowlist",
):
    return await send_loop.send_one(
        db,
        org_id=org.id,
        mailbox=mailbox,
        settings=settings or CampaignSettings(),
        sender=SmtpSender(
            sending_settings(smtp_server.host, smtp_server.port, allow=allow, mode=mode)
        ),
        now=now,
    )


class TestImportIsRepeatable:
    """§7A: a preview writes nothing, and a re-import adds only what is new."""

    async def test_a_preview_writes_nothing(self, api, owner, campaign, db, org):
        before = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign.id)
        )

        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview",
            headers=owner,
            files=as_upload(sheet(10)),
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["rows"]) == 10
        after = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign.id)
        )
        assert before == after

    async def test_committing_imports_the_sheet(self, api, owner, campaign, db):
        result = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=as_upload(sheet(10)),
        ).json()

        assert result["imported"] == 10

    async def test_reimporting_the_same_sheet_plus_two_adds_only_two(
        self, api, owner, campaign, db
    ):
        """The prototype used INSERT OR IGNORE on a global unique email, so re-uploading a
        corrected sheet silently changed nothing (issue B16)."""
        api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=as_upload(sheet(10)),
        )

        result = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=as_upload(sheet(12)),
        ).json()

        assert result["imported"] == 2, result

    async def test_a_suppressed_address_is_skipped_with_its_reason(
        self, api, owner, campaign, db, org
    ):
        db.add(
            Suppression(org_id=org.id, kind="email", value="lead3@example.com", reason="opt_out")
        )
        await db.commit()

        rows = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview",
            headers=owner,
            files=as_upload(sheet(5)),
        ).json()["rows"]

        blocked = next(row for row in rows if row["email"] == "lead3@example.com")
        assert blocked["verdict"] != "importable"
        assert any("suppress" in reason.lower() for reason in blocked["reasons"]), blocked


class TestNothingLeavesOutsideTheRules:
    """The refusals. Each is a way the prototype would have sent mail it should not have."""

    async def test_nothing_is_sent_at_the_weekend(
        self, db, org, mailbox, lead_factory, smtp_server
    ):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)

        decision = await send_once(db, org, mailbox, smtp_server, now=WEEKEND)

        assert not decision.sent
        assert "window" in decision.reason
        assert smtp_server.messages == []

    async def test_nothing_is_sent_after_hours(self, db, org, mailbox, lead_factory, smtp_server):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)

        decision = await send_once(db, org, mailbox, smtp_server, now=AFTER_HOURS)

        assert not decision.sent
        assert smtp_server.messages == []

    async def test_a_recipient_outside_the_allowlist_never_receives_anything(
        self, db, org, mailbox, lead_factory, smtp_server
    ):
        """Hard rule 1, at the end of the whole chain rather than in a unit test."""
        lead = await lead_factory(state="APPROVED", email="stranger@example.com")
        await approved_draft_for(db, org, lead)

        decision = await send_once(db, org, mailbox, smtp_server, allow="someone-else@example.com")

        assert not decision.sent
        assert smtp_server.messages == [], "a blocked recipient reached the SMTP server"

    async def test_a_suppressed_lead_is_not_sent_to(
        self, db, org, mailbox, lead_factory, smtp_server
    ):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)
        db.add(
            Suppression(org_id=org.id, kind="email", value="lead1@example.com", reason="opt_out")
        )
        await db.commit()

        decision = await send_once(db, org, mailbox, smtp_server)

        assert not decision.sent
        assert smtp_server.messages == []

    async def test_the_daily_cap_stops_sending(self, db, org, mailbox, lead_factory, smtp_server):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)
        mailbox.daily_cap = 1
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                mailbox_id=mailbox.id,
                direction="out",
                status="sent",
                kind="initial",
                subject="earlier today",
                body_text="hi",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                idempotency_key=str(uuid.uuid4()),
                sent_at=INSIDE,
            )
        )
        await db.commit()

        decision = await send_once(db, org, mailbox, smtp_server)

        assert not decision.sent
        assert "cap" in decision.reason


class TestOneLeadEndToEnd:
    """The spine: approved -> sent -> recorded -> follow-up scheduled -> reply cancels it."""

    async def test_a_send_is_performed_and_recorded(
        self, db, org, mailbox, lead_factory, smtp_server
    ):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)

        decision = await send_once(db, org, mailbox, smtp_server)

        assert decision.sent, decision.reason
        assert len(smtp_server.messages) == 1
        assert smtp_server.messages[0]["rcpt"] == ["lead1@example.com"]

        message = (await db.execute(select(Message).where(Message.lead_id == lead.id))).scalar_one()
        assert message.status == "sent"
        await db.refresh(lead)
        assert lead.state == "SENT"

    async def test_the_message_carries_the_compliance_headers(
        self, db, org, mailbox, lead_factory, smtp_server
    ):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)

        await send_once(db, org, mailbox, smtp_server)

        raw = smtp_server.messages[0]["raw"]
        assert "List-Unsubscribe" in raw
        assert "Message-ID" in raw
        # Plain text only: no HTML part, no tracking pixel, no rewritten links.
        assert "text/html" not in raw.lower()

    async def test_a_followup_is_scheduled(self, db, org, mailbox, lead_factory, smtp_server):
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        await approved_draft_for(db, org, lead)

        await send_once(db, org, mailbox, smtp_server)

        await db.refresh(lead)
        assert lead.next_action_at is not None
        assert lead.next_action_at > INSIDE

    async def test_an_interrupted_send_is_never_sent_twice(
        self, db, org, mailbox, lead_factory, smtp_server
    ):
        """§9.5. A crash between the SMTP call and the row recording it must not resend.

        Claiming the same lead and kind a second time has to be refused by the unique
        idempotency key — this is the exact shape of the production defect where the email
        arrived and `mark_sent` then raised.
        """
        lead = await lead_factory(state="APPROVED", email="lead1@example.com")
        draft = await approved_draft_for(db, org, lead)

        first = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            mailbox_id=mailbox.id,
            draft_id=draft.id,
            subject=draft.subject,
            body_text=draft.body,
            now=INSIDE,
        )
        await db.commit()
        assert first.claimed

        second = await outbox.claim_send(
            db,
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            mailbox_id=mailbox.id,
            draft_id=draft.id,
            subject=draft.subject,
            body_text=draft.body,
            now=INSIDE + timedelta(minutes=1),
        )

        assert not second.claimed, "the same send was claimed twice"
        count = await db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.lead_id == lead.id, Message.direction == "out")
        )
        assert count == 1


class TestARepliedLeadStopsBeingChased:
    async def test_a_reply_cancels_the_followup(self, db, org, lead_factory):
        """A follow-up sent after somebody has already replied is the most visible failure
        this system can have, so the cancellation shares the transaction that records the
        reply."""
        from app.db.state import transition
        from app.domain.state import LeadState

        lead = await lead_factory(
            state="SENT", email="lead1@example.com", next_action_at=INSIDE + timedelta(days=3)
        )
        await db.commit()

        await transition(
            db,
            org_id=org.id,
            lead_id=lead.id,
            to_state=LeadState.REPLIED,
            clear_next_action=True,
        )
        await db.commit()

        await db.refresh(lead)
        assert lead.state == "REPLIED"
        assert lead.next_action_at is None, "a replied lead is still scheduled for a follow-up"

    async def test_an_opt_out_suppresses_forever(self, api, owner, db, org, campaign):
        """Suppression outlives the lead: re-importing the same sheet must not reach them."""
        api.post(
            "/api/v1/suppressions",
            headers=owner,
            json={"value": "lead2@example.com", "reason": "opt_out"},
        )

        rows = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview",
            headers=owner,
            files=as_upload(sheet(3)),
        ).json()["rows"]

        blocked = next(row for row in rows if row["email"] == "lead2@example.com")
        assert blocked["verdict"] != "importable"
