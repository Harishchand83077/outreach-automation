"""Campaigns, leads and the review queue (§14; issues C1, C7).

Two things these tests are really about.

**Nothing crosses an org boundary.** Every query is scoped by the org on the membership row, and
an id from another org must behave as though it does not exist — 404, not 403, because confirming
that an id is real elsewhere is itself a leak.

**A list row is a summary.** The prototype's `GET /api/leads` returned every lead's full
insights, draft body and reply text, and the dashboard polled it every 2.5 seconds (C7). So the
list endpoints are tested for what they must *not* contain, which is the only way that stays
true as fields are added later.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import deps
from app.api.app import create_app
from app.api.pagination import MAX_LIMIT, Cursor
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Draft, Member, Org

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
    """A member of the fixture org, plus their bearer header."""
    private, _ = keypair
    subject = f"owner-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email="owner@example.com", role="owner"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


@pytest_asyncio.fixture(loop_scope="session")
async def stranger(db, keypair):
    """A member of a *different* org, used to prove nothing crosses the boundary."""
    private, _ = keypair
    other = Org(name="Rival Ltd")
    db.add(other)
    await db.flush()
    subject = f"rival-{uuid.uuid4()}"
    db.add(Member(org_id=other.id, user_id=subject, email="rival@example.com", role="owner"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


class TestCampaigns:
    async def test_a_campaign_is_listed_for_its_own_org(self, api, owner, campaign):
        body = api.get("/api/v1/campaigns", headers=owner).json()
        assert str(campaign.id) in [row["id"] for row in body["campaigns"]]

    async def test_another_orgs_campaigns_are_invisible(self, api, stranger, campaign):
        body = api.get("/api/v1/campaigns", headers=stranger).json()
        assert str(campaign.id) not in [row["id"] for row in body["campaigns"]]

    async def test_fetching_another_orgs_campaign_is_a_404(self, api, stranger, campaign):
        """404 rather than 403: confirming an id exists elsewhere is itself a leak."""
        assert api.get(f"/api/v1/campaigns/{campaign.id}", headers=stranger).status_code == 404

    async def test_the_list_does_not_carry_the_persona(self, api, owner, campaign):
        """A persona is several hundred words. A list row says whether one exists."""
        row = api.get("/api/v1/campaigns", headers=owner).json()["campaigns"][0]
        assert "persona" not in row
        assert "has_persona" in row

    async def test_an_anonymous_caller_gets_nothing(self, api):
        assert api.get("/api/v1/campaigns").status_code == 401


class TestLeadLists:
    async def test_a_list_row_carries_no_bodies(self, api, owner, campaign, lead_factory):
        """C7: the prototype returned every lead's insights, draft and reply on every poll."""
        await lead_factory(email="someone@example.com")
        body = api.get(f"/api/v1/campaigns/{campaign.id}/leads", headers=owner).json()

        row = body["leads"][0]
        for leaked in ("draft", "body", "insights", "reply", "messages", "research"):
            assert leaked not in row, f"a list row must not carry {leaked}"

    async def test_leads_can_be_filtered_by_state(self, api, owner, campaign, lead_factory):
        await lead_factory(email="sent@example.com", state="SENT")
        body = api.get(f"/api/v1/campaigns/{campaign.id}/leads?state=SENT", headers=owner).json()
        assert all(row["state"] == "SENT" for row in body["leads"])

    async def test_search_matches_identity_not_message_bodies(self, api, owner, campaign):
        """A search box that matched bodies would be an export of everything ever written."""
        import inspect

        from app.api.routers import campaigns as module

        source = inspect.getsource(module.list_leads)
        assert "body_text" not in source
        assert "Draft.body" not in source

    async def test_another_orgs_leads_are_not_reachable(self, api, stranger, campaign):
        assert (
            api.get(f"/api/v1/campaigns/{campaign.id}/leads", headers=stranger).status_code == 404
        )


class TestPagination:
    async def test_a_page_is_capped_however_much_is_asked_for(self, api, owner, campaign):
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads?limit=100000", headers=owner)
        assert response.status_code == 422, "the cap is enforced before the query runs"

    async def test_paging_walks_the_whole_set_without_repeats(
        self, api, owner, campaign, lead_factory
    ):
        for index in range(5):
            await lead_factory(email=f"page{index}@example.com")

        seen: list[str] = []
        cursor = None
        for _ in range(10):
            url = f"/api/v1/campaigns/{campaign.id}/leads?limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            body = api.get(url, headers=owner).json()
            seen.extend(row["id"] for row in body["leads"])
            cursor = body["next_cursor"]
            if not cursor:
                break

        assert len(seen) == len(set(seen)), "no lead appeared on two pages"
        assert cursor is None, "paging terminated"

    async def test_a_tampered_cursor_is_rejected_not_ignored(self, api, owner, campaign):
        response = api.get(
            f"/api/v1/campaigns/{campaign.id}/leads?cursor=not-a-cursor", headers=owner
        )
        assert response.status_code == 400

    async def test_the_default_limit_is_not_unbounded(self):
        from app.api.pagination import DEFAULT_LIMIT, clamp_limit

        assert clamp_limit(None) == DEFAULT_LIMIT
        assert clamp_limit(10_000) == MAX_LIMIT

    async def test_a_cursor_round_trips(self):
        from datetime import UTC, datetime

        cursor = Cursor(created_at=datetime.now(tz=UTC), row_id=uuid.uuid4())
        assert Cursor.decode(cursor.encode()) == cursor


class TestLeadDetail:
    async def test_it_carries_the_bodies_and_the_timeline(
        self, db, api, owner, org, campaign, lead_factory
    ):
        lead = await lead_factory(email="detail@example.com")
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="initial",
                version=1,
                subject="quick question",
                body="our pitch",
                status="pending_review",
            )
        )
        await db.commit()

        body = api.get(f"/api/v1/leads/{lead.id}", headers=owner).json()

        assert body["drafts"][0]["body"] == "our pitch"
        assert "timeline" in body

    async def test_another_orgs_lead_is_a_404(self, api, stranger, lead_factory):
        lead = await lead_factory(email="private@example.com")
        assert api.get(f"/api/v1/leads/{lead.id}", headers=stranger).status_code == 404

    async def test_a_made_up_id_is_a_404_not_a_500(self, api, owner):
        assert api.get(f"/api/v1/leads/{uuid.uuid4()}", headers=owner).status_code == 404


class TestReviewQueue:
    async def test_the_queue_carries_the_evidence_beside_the_draft(
        self, db, api, owner, org, campaign, lead_factory
    ):
        """A reviewer who cannot see what a claim rests on cannot judge it, and will approve."""
        lead = await lead_factory(email="review@example.com")
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="initial",
                version=1,
                subject="quick question",
                body="our pitch",
                status="pending_review",
            )
        )
        await db.commit()

        body = api.get("/api/v1/review/drafts", headers=owner).json()
        item = next(row for row in body["drafts"] if row["lead_id"] == str(lead.id))
        assert "hooks" in item
        assert "research_confidence" in item

    async def test_another_orgs_queue_is_empty(self, db, api, stranger, org, lead_factory):
        lead = await lead_factory(email="theirs@example.com")
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="initial",
                version=1,
                subject="s",
                body="b",
                status="pending_review",
            )
        )
        await db.commit()

        body = api.get("/api/v1/review/drafts", headers=stranger).json()
        assert all(row["lead_id"] != str(lead.id) for row in body["drafts"])

    async def test_rejecting_without_a_reason_is_refused(self, db, api, owner, org, lead_factory):
        """The feedback is what the next draft is generated from."""
        lead = await lead_factory(email="noreason@example.com")
        draft = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            version=1,
            subject="s",
            body="b",
            status="pending_review",
        )
        db.add(draft)
        await db.commit()

        response = api.post(
            f"/api/v1/drafts/{draft.id}/reject", headers=owner, json={"feedback": "  "}
        )
        assert response.status_code == 422

    async def test_approving_a_stale_draft_is_a_conflict_not_a_crash(
        self, db, api, owner, org, lead_factory
    ):
        """The reviewer is looking at an old queue and should reload."""
        lead = await lead_factory(email="stale@example.com")
        draft = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            version=1,
            subject="s",
            body="b",
            status="sent",
        )
        db.add(draft)
        await db.commit()

        response = api.post(f"/api/v1/drafts/{draft.id}/approve", headers=owner, json={})
        assert response.status_code == 409

    async def test_another_orgs_draft_cannot_be_approved(
        self, db, api, stranger, org, lead_factory
    ):
        """The tenancy boundary on a *write*, which is the one that matters most."""
        lead = await lead_factory(email="notyours@example.com")
        draft = Draft(
            org_id=org.id,
            lead_id=lead.id,
            kind="initial",
            version=1,
            subject="s",
            body="b",
            status="pending_review",
        )
        db.add(draft)
        await db.commit()

        response = api.post(f"/api/v1/drafts/{draft.id}/approve", headers=stranger, json={})
        assert response.status_code in (404, 409)

        await db.refresh(draft)
        assert draft.status == "pending_review", "it was not approved"

    async def test_approving_sends_nothing(self):
        """Hard rule 1: approval makes a lead eligible; the send loop decides the rest."""
        import inspect

        from app.api.routers import review as module

        source = inspect.getsource(module)
        for forbidden in ("smtplib", "SmtpSender", "send_one", "sendmail"):
            assert forbidden not in source
