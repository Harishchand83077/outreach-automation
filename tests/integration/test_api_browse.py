"""Finding things: lead search and filters, and a review queue that pages.

Both exist because of the same problem seen from two sides. A reviewer with a hundred drafts
cannot be handed a hundred drafts -- each one carries a subject, a body and its research hooks,
so a single response would be hundreds of kilobytes and the screen would be unusable. And an
operator with a thousand leads cannot find one by scrolling.

The rules the tests hold:

- **Search is on identity only.** Email, name and company. Searching bodies would turn a search
  box into an export of everything anybody has ever written to or about a lead.
- **Filters combine as AND**, because that is what a person reading two filled-in boxes expects.
- **The review queue reports a total.** A cursor tells you whether there is a next page but not
  how much work is left, and "47 drafts waiting" is the number that decides whether somebody
  starts now or after lunch.
- **Every query stays scoped to the org**, which is what the stranger tests are for.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

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
async def people(db, org, campaign, lead_factory):
    """Three leads whose fields differ one at a time, so a filter can be told apart."""
    made = [
        await lead_factory(
            state="NEW", email="asha@globex.example", full_name="Asha Rao", company_name="Globex"
        ),
        await lead_factory(
            state="SENT", email="ben@initech.example", full_name="Ben Shah", company_name="Initech"
        ),
        await lead_factory(
            state="SENT", email="cara@globex.example", full_name="Cara Rao", company_name="Globex"
        ),
    ]
    await db.commit()
    return made


def emails(response) -> set[str]:
    return {row["email"] for row in response.json()["leads"]}


class TestLeadSearch:
    async def test_an_email_fragment_finds_the_one_lead(self, api, owner, campaign, people):
        response = api.get(
            f"/api/v1/campaigns/{campaign.id}/leads?email=ben@initech", headers=owner
        )

        assert response.status_code == 200, response.text
        assert emails(response) == {"ben@initech.example"}

    async def test_a_name_fragment_finds_everyone_matching(self, api, owner, campaign, people):
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads?name=rao", headers=owner)

        assert emails(response) == {"asha@globex.example", "cara@globex.example"}

    async def test_a_company_filter_is_separate_from_a_name(self, api, owner, campaign, people):
        """The reason these are separate boxes: "Globex" is a company, not a person."""
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads?company=globex", headers=owner)

        assert emails(response) == {"asha@globex.example", "cara@globex.example"}

    async def test_filters_combine_as_and(self, api, owner, campaign, people):
        response = api.get(
            f"/api/v1/campaigns/{campaign.id}/leads?company=globex&state=SENT", headers=owner
        )

        assert emails(response) == {"cara@globex.example"}

    async def test_search_is_case_insensitive(self, api, owner, campaign, people):
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads?name=ASHA", headers=owner)

        assert emails(response) == {"asha@globex.example"}

    async def test_a_wildcard_is_matched_literally(self, api, owner, campaign, people):
        """`%` is a LIKE metacharacter. Unescaped, searching for it returns every row."""
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads?name=%25", headers=owner)

        assert emails(response) == set()

    async def test_no_filter_returns_the_page(self, api, owner, campaign, people):
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads", headers=owner)

        assert len(response.json()["leads"]) == 3

    async def test_a_list_row_still_carries_no_bodies(self, api, owner, campaign, people):
        """C7, restated: adding filters must not have widened the row."""
        row = api.get(f"/api/v1/campaigns/{campaign.id}/leads", headers=owner).json()["leads"][0]

        for forbidden in ("body", "draft", "insights", "reply", "research"):
            assert forbidden not in row, f"{forbidden} must not appear in a list row"


@pytest_asyncio.fixture(loop_scope="session")
async def many(db, org, campaign, lead_factory):
    """Seven drafts awaiting review, which is more than one page of three.

    Module level rather than inside the paging class, because the cross-org test needs the same
    rows: "another org sees nothing" only means something when there is something to see.
    `created_at` is set explicitly so the oldest-first assertion is about the ordering and not
    about how fast the inserts happened to run.
    """
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for n in range(7):
        lead = await lead_factory(state="AWAITING_APPROVAL", email=f"queue{n}@example.com")
        db.add(
            Draft(
                org_id=org.id,
                lead_id=lead.id,
                kind="initial",
                version=1,
                subject=f"Draft {n}",
                body="Hello.",
                status="pending_review",
                created_at=base + timedelta(minutes=n),
            )
        )
    await db.commit()


class TestTheReviewQueuePages:
    async def test_it_returns_only_the_page_asked_for(self, api, owner, campaign, many):
        response = api.get(
            f"/api/v1/review/drafts?campaign_id={campaign.id}&limit=3", headers=owner
        )

        assert response.status_code == 200, response.text
        assert len(response.json()["drafts"]) == 3

    async def test_it_reports_how_much_work_is_left(self, api, owner, campaign, many):
        """A cursor says whether there is more; a total says how much. Both are needed."""
        body = api.get(
            f"/api/v1/review/drafts?campaign_id={campaign.id}&limit=3", headers=owner
        ).json()

        assert body["total"] == 7

    async def test_the_cursor_continues_without_repeating(self, api, owner, campaign, many):
        first = api.get(
            f"/api/v1/review/drafts?campaign_id={campaign.id}&limit=3", headers=owner
        ).json()
        second = api.get(
            f"/api/v1/review/drafts?campaign_id={campaign.id}&limit=3"
            f"&cursor={first['next_cursor']}",
            headers=owner,
        ).json()

        seen = [row["draft_id"] for row in first["drafts"]]
        following = [row["draft_id"] for row in second["drafts"]]
        assert set(seen).isdisjoint(following), "a page repeated a draft somebody already judged"
        assert len(following) == 3

    async def test_the_last_page_has_no_cursor(self, api, owner, campaign, many):
        body = api.get(
            f"/api/v1/review/drafts?campaign_id={campaign.id}&limit=50", headers=owner
        ).json()

        assert body["next_cursor"] is None

    async def test_oldest_first(self, api, owner, campaign, many):
        """FIFO, so nothing waits forever while newer drafts are judged first."""
        body = api.get(
            f"/api/v1/review/drafts?campaign_id={campaign.id}&limit=7", headers=owner
        ).json()

        assert [row["subject"] for row in body["drafts"]] == [f"Draft {n}" for n in range(7)]


class TestAnotherOrgSeesNothing:
    async def test_lead_search_is_scoped(self, api, stranger, campaign, people):
        response = api.get(f"/api/v1/campaigns/{campaign.id}/leads?name=rao", headers=stranger)

        # 404 rather than an empty list: confirming the campaign id exists is itself a leak.
        assert response.status_code == 404

    async def test_the_review_queue_is_scoped(self, api, stranger, campaign, many):
        body = api.get(f"/api/v1/review/drafts?campaign_id={campaign.id}", headers=stranger).json()

        assert body["drafts"] == []
        assert body["total"] == 0
