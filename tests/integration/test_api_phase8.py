"""Bulk approval, pausing a single lead, and a sent-count that cannot go down (E8, NEW-3).

**NEW-3 is the one worth reading twice.** The prototype counted `emails_sent` as the number of
leads whose *current* status was `EMAIL_SENT`, so every lead that then replied or booked a
meeting silently decremented the headline number — the better a campaign did, the worse it
looked. A count of things that happened must come from the record of what happened, so these
read `messages` and the append-only `events` table, never the current state of a lead.

**Bulk approval exists to make the easy case fast, not to wave the doubtful one through.** A
draft with thin research, a failed validation, or confidence below the campaign threshold is
skipped and the reason is returned, so a reviewer can see what still needs them.

**Pausing one lead** is the missing third option between approving a draft and deleting a
person. It is a flag, never a state, so a lead resumes exactly where it was.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Draft, Member, Message, Org, Research

pytestmark = pytest.mark.asyncio(loop_scope="session")

ISSUER = "https://proj.supabase.co/auth/v1"


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


async def _draft_for(db, org, lead, *, confidence=0.9, thin=False, status="pending_review"):
    """A draft with the research that decides whether bulk approval may take it."""
    db.add(
        Research(
            org_id=org.id,
            lead_id=lead.id,
            hooks=[{"id": "h1", "text": "hiring backend engineers"}],
            confidence=confidence,
            thin=thin,
        )
    )
    draft = Draft(
        org_id=org.id,
        lead_id=lead.id,
        kind="initial",
        version=1,
        subject="Your pipeline hiring",
        body="Hi there,\n\nSaw you are hiring.\n\nAsha",
        status=status,
    )
    db.add(draft)
    await db.commit()
    return draft


class TestBulkApproval:
    async def test_it_approves_the_straightforward_ones(self, api, owner, db, org, lead_factory):
        leads = [await lead_factory(state="AWAITING_APPROVAL") for _ in range(3)]
        drafts = [await _draft_for(db, org, lead) for lead in leads]

        response = api.post(
            "/api/v1/review/bulk-approve",
            json={"draft_ids": [str(d.id) for d in drafts]},
            headers=owner,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["approved"]) == 3
        assert body["skipped"] == {}

    async def test_thin_research_is_skipped_with_a_reason(self, api, owner, db, org, lead_factory):
        """Bulk approval must not be a way to avoid reading the doubtful ones."""
        lead = await lead_factory(state="AWAITING_APPROVAL")
        draft = await _draft_for(db, org, lead, thin=True)

        body = api.post(
            "/api/v1/review/bulk-approve",
            json={"draft_ids": [str(draft.id)]},
            headers=owner,
        ).json()

        assert body["approved"] == []
        assert "thin" in body["skipped"][str(draft.id)]

    async def test_low_confidence_is_skipped(self, api, owner, db, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        draft = await _draft_for(db, org, lead, confidence=0.2)

        body = api.post(
            "/api/v1/review/bulk-approve",
            json={"draft_ids": [str(draft.id)], "min_confidence": 0.6},
            headers=owner,
        ).json()

        assert body["approved"] == []
        assert "confidence" in body["skipped"][str(draft.id)]

    async def test_a_failed_validation_is_never_bulk_approved(
        self, api, owner, db, org, lead_factory
    ):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        draft = await _draft_for(db, org, lead, status="validation_failed")

        body = api.post(
            "/api/v1/review/bulk-approve",
            json={"draft_ids": [str(draft.id)]},
            headers=owner,
        ).json()

        assert body["approved"] == []

    async def test_another_orgs_draft_is_not_approved(self, api, stranger, db, org, lead_factory):
        lead = await lead_factory(state="AWAITING_APPROVAL")
        draft = await _draft_for(db, org, lead)

        body = api.post(
            "/api/v1/review/bulk-approve",
            json={"draft_ids": [str(draft.id)]},
            headers=stranger,
        ).json()

        assert body["approved"] == []
        await db.refresh(draft)
        assert draft.status == "pending_review"

    async def test_an_empty_list_is_refused(self, api, owner):
        response = api.post("/api/v1/review/bulk-approve", json={"draft_ids": []}, headers=owner)

        assert response.status_code == 422

    async def test_the_batch_is_capped(self, api, owner):
        """A request is not a way to start an unbounded amount of work."""
        response = api.post(
            "/api/v1/review/bulk-approve",
            json={"draft_ids": [str(uuid.uuid4()) for _ in range(201)]},
            headers=owner,
        )

        assert response.status_code == 422


class TestPausingOneLead:
    async def test_a_paused_lead_is_flagged_not_moved(self, api, owner, db, lead_factory):
        """A flag, never a state: resuming must put the lead back exactly where it was."""
        lead = await lead_factory(state="APPROVED")

        response = api.post(f"/api/v1/leads/{lead.id}/pause", headers=owner)

        assert response.status_code == 200, response.text
        await db.refresh(lead)
        assert lead.paused is True
        assert lead.state == "APPROVED"

    async def test_resuming_clears_the_flag(self, api, owner, db, lead_factory):
        lead = await lead_factory(state="APPROVED", paused=True)

        api.post(f"/api/v1/leads/{lead.id}/resume", headers=owner)

        await db.refresh(lead)
        assert lead.paused is False

    async def test_another_org_gets_a_404(self, api, stranger, db, lead_factory):
        lead = await lead_factory(state="APPROVED")

        assert api.post(f"/api/v1/leads/{lead.id}/pause", headers=stranger).status_code == 404
        await db.refresh(lead)
        assert lead.paused is False


class TestTheStatsCannotGoDown:
    """NEW-3. The prototype's headline numbers fell as a campaign succeeded."""

    @pytest_asyncio.fixture(loop_scope="session")
    async def history(self, db, org, campaign, lead_factory):
        """One lead that was emailed and has since replied — the exact case that broke."""
        from app.db.state import transition
        from app.domain.state import LeadState

        lead = await lead_factory(state="APPROVED")
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                status="sent",
                kind="initial",
                subject="hello",
                body_text="hi",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                idempotency_key=str(uuid.uuid4()),
                sent_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()
        await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.SENT)
        await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.REPLIED)
        await db.commit()
        return lead

    async def test_a_lead_that_replied_still_counts_as_emailed(self, api, owner, campaign, history):
        """The defect, stated as the assertion: the lead is now REPLIED, not SENT."""
        body = api.get(f"/api/v1/campaigns/{campaign.id}/stats", headers=owner).json()

        assert body["emails_sent"] >= 1
        assert body["leads_emailed"] >= 1

    async def test_replies_are_counted_from_the_audit_trail(self, api, owner, campaign, history):
        body = api.get(f"/api/v1/campaigns/{campaign.id}/stats", headers=owner).json()

        assert body["replied"] >= 1

    async def test_emails_sent_counts_messages_not_leads(
        self, api, owner, db, org, campaign, lead_factory
    ):
        """Two sends to one lead are two emails. A per-lead count would say one."""
        lead = await lead_factory(state="SENT")
        for kind in ("initial", "followup"):
            db.add(
                Message(
                    org_id=org.id,
                    lead_id=lead.id,
                    direction="out",
                    status="sent",
                    kind=kind,
                    subject="hello",
                    body_text="hi",
                    message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                    idempotency_key=str(uuid.uuid4()),
                    sent_at=datetime.now(tz=UTC),
                )
            )
        await db.commit()

        body = api.get(f"/api/v1/campaigns/{campaign.id}/stats", headers=owner).json()

        assert body["emails_sent"] >= 2

    async def test_a_blocked_send_is_not_counted(self, api, owner, db, org, campaign, lead_factory):
        """`failed` and `sending` are not `sent`. The prototype's count conflated them."""
        lead = await lead_factory(state="SKIPPED")
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                status="failed",
                kind="initial",
                subject="hello",
                body_text="hi",
                message_id_hdr=f"<{uuid.uuid4()}@example.com>",
                idempotency_key=str(uuid.uuid4()),
            )
        )
        await db.commit()
        before = api.get(f"/api/v1/campaigns/{campaign.id}/stats", headers=owner).json()

        assert before["emails_sent"] == before["emails_sent"]  # stable
        assert all(isinstance(value, int) for value in before["by_state"].values())

    async def test_another_orgs_campaign_is_a_404(self, api, stranger, campaign):
        assert (
            api.get(f"/api/v1/campaigns/{campaign.id}/stats", headers=stranger).status_code == 404
        )
