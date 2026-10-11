"""Imports, suppressions, mailbox and admin (§7A, §14).

The import tests are the ones that matter most, because importing is the only endpoint that
takes a file from a user and turns it into people who will be emailed. Two properties:

- **Preview writes nothing**, so a person can look before committing.
- **Commit does not trust the preview.** It re-reads the file and re-validates against the
  database, because a preview can be minutes old — somebody may have opted out since — or
  simply forged by whoever is posting.

The suppression tests cover the other direction: the list is how "never contact this person
again" is expressed, so adding is idempotent and removal is deliberate.
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
from sqlalchemy import func, select

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.routers.imports import MAX_UPLOAD_BYTES
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Job, Lead, Mailbox, Member, Org, Suppression

pytestmark = pytest.mark.asyncio(loop_scope="session")

ISSUER = "https://proj.supabase.co/auth/v1"

SHEET = (
    "email,full name,company,website,title\n"
    "ben@globex.example,Ben Shah,Globex,https://globex.example,Founder\n"
    "mira@initech.example,Mira Rao,Initech,https://initech.example,CTO\n"
)


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


def upload(content: str = SHEET, name: str = "leads.csv"):
    return {"file": (name, content.encode(), "text/csv")}


class TestImportPreview:
    async def test_it_reports_a_verdict_for_every_row(self, api, owner, campaign):
        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview", headers=owner, files=upload()
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["rows"]) == 2
        assert all("verdict" in row for row in body["rows"])

    async def test_it_writes_nothing(self, db, api, owner, campaign, org):
        before = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign.id)
        )
        api.post(f"/api/v1/campaigns/{campaign.id}/imports/preview", headers=owner, files=upload())
        after = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.campaign_id == campaign.id)
        )
        assert before == after, "a preview must not create leads"

    async def test_an_unreadable_file_is_a_422_not_a_500(self, api, owner, campaign):
        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview",
            headers=owner,
            files=upload("not a spreadsheet at all", "junk.xlsx"),
        )
        assert response.status_code == 422

    async def test_an_empty_file_is_refused(self, api, owner, campaign):
        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview",
            headers=owner,
            files={"file": ("empty.csv", b"", "text/csv")},
        )
        assert response.status_code == 422

    async def test_an_oversized_upload_is_refused(self, api, owner, campaign):
        """A hostile upload must not be read into memory without a bound."""
        giant = "a," * (MAX_UPLOAD_BYTES // 2 + 10)
        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview",
            headers=owner,
            files=upload(giant),
        )
        assert response.status_code == 413

    async def test_another_orgs_campaign_cannot_be_imported_into(self, api, stranger, campaign):
        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/preview", headers=stranger, files=upload()
        )
        assert response.status_code == 404

    async def test_an_anonymous_caller_cannot_upload(self, api, campaign):
        response = api.post(f"/api/v1/campaigns/{campaign.id}/imports/preview", files=upload())
        assert response.status_code == 401

    async def test_a_traversing_filename_never_reaches_the_filesystem(
        self, api, owner, campaign, tmp_path, monkeypatch
    ):
        """Issue C2, re-proved against the new endpoint.

        The prototype did `path = _root/"uploads"/file.filename` with the client's own filename,
        so `../../x.csv` escaped the upload directory. The replacement parses bytes in memory and
        uses the filename only to pick a parser — so the test worth having is not "the name is
        sanitised" but **"nothing is written at all"**, which stays true however the parser
        changes later.

        `tests/test_upload_security.py` proved this about the deleted `api_server.py` and went
        with it in PR #26. This is its replacement; without it, C2 is a verdict with no test.
        """
        monkeypatch.chdir(tmp_path)
        before = {path for path in tmp_path.rglob("*")}

        for name in ("../../escaped.csv", r"..\..\escaped.csv", "/etc/passwd.csv"):
            response = api.post(
                f"/api/v1/campaigns/{campaign.id}/imports/preview",
                headers=owner,
                files=upload(SHEET, name),
            )
            # Accepted or refused is beside the point; writing a file is not.
            assert response.status_code in (200, 422)

        assert {path for path in tmp_path.rglob("*")} == before
        assert not list(tmp_path.rglob("escaped.csv"))
        assert not list(tmp_path.rglob("*passwd*"))


class TestImportCommit:
    async def test_it_creates_the_leads(self, db, api, owner, campaign):
        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit", headers=owner, files=upload()
        )
        assert response.status_code == 200
        assert response.json()["imported"] == 2

        emails = (
            (await db.execute(select(Lead.email_norm).where(Lead.campaign_id == campaign.id)))
            .scalars()
            .all()
        )
        assert "ben@globex.example" in emails

    async def test_each_lead_gets_research_queued_in_the_same_transaction(
        self, db, api, owner, campaign, org
    ):
        """A lead must never exist without the work that moves it forward."""
        api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=upload(
                "email,full name,company\nqueued@globex.example,Queued Person,Globex\n",
                "one.csv",
            ),
        )
        lead = await db.scalar(select(Lead).where(Lead.email_norm == "queued@globex.example"))
        assert lead is not None
        job = await db.scalar(select(Job).where(Job.lead_id == lead.id))
        assert job is not None, "research was queued alongside the insert"

    async def test_a_suppressed_address_is_not_imported(self, db, api, owner, campaign, org):
        """Re-validated at commit time: the preview may have been taken before the opt-out."""
        db.add(
            Suppression(
                org_id=org.id, kind="email", value="optout@globex.example", reason="opt_out"
            )
        )
        await db.commit()

        response = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=upload(
                "email,full name,company\noptout@globex.example,Opted Out,Globex\n", "s.csv"
            ),
        )
        assert response.json()["imported"] == 0

        lead = await db.scalar(select(Lead).where(Lead.email_norm == "optout@globex.example"))
        assert lead is None

    async def test_committing_twice_does_not_duplicate(self, db, api, owner, campaign):
        content = "email,full name,company\ntwice@globex.example,Twice Person,Globex\n"
        api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=upload(content, "t.csv"),
        )
        second = api.post(
            f"/api/v1/campaigns/{campaign.id}/imports/commit",
            headers=owner,
            files=upload(content, "t.csv"),
        )
        assert second.status_code == 200, second.text
        assert second.json()["imported"] == 0

        count = await db.scalar(
            select(func.count()).select_from(Lead).where(Lead.email_norm == "twice@globex.example")
        )
        assert count == 1

    async def test_commit_re_reads_the_file_rather_than_trusting_a_verdict(self):
        """Structural: the endpoint takes a file, never a row list from the caller."""
        import inspect

        from app.api.routers import imports as module

        signature = inspect.signature(module.commit_import)
        assert "file" in signature.parameters
        assert not any(name in signature.parameters for name in ("rows", "preview", "verdicts"))


class TestSuppressions:
    async def test_an_address_can_be_added_and_listed(self, api, owner):
        api.post("/api/v1/suppressions", headers=owner, json={"value": "No@Globex.Example"})
        body = api.get("/api/v1/suppressions", headers=owner).json()
        assert "no@globex.example" in [row["value"] for row in body["suppressions"]]

    async def test_adding_twice_is_not_an_error(self, api, owner):
        """An opt-out may be detected by keyword and by the classifier; both must be safe."""
        first = api.post("/api/v1/suppressions", headers=owner, json={"value": "dup@x.example"})
        second = api.post("/api/v1/suppressions", headers=owner, json={"value": "dup@x.example"})
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]

    async def test_an_empty_value_is_refused(self, api, owner):
        response = api.post("/api/v1/suppressions", headers=owner, json={"value": "   "})
        assert response.status_code == 422

    async def test_an_unknown_reason_is_a_422_not_a_500(self, api, owner):
        """The column has a check constraint; a violation would surface as a server error."""
        response = api.post(
            "/api/v1/suppressions",
            headers=owner,
            json={"value": "reason@x.example", "reason": "because-i-said-so"},
        )
        assert response.status_code == 422

    async def test_another_orgs_list_is_separate(self, api, owner, stranger):
        api.post("/api/v1/suppressions", headers=owner, json={"value": "mine@x.example"})
        body = api.get("/api/v1/suppressions", headers=stranger).json()
        assert "mine@x.example" not in [row["value"] for row in body["suppressions"]]

    async def test_another_orgs_entry_cannot_be_deleted(self, db, api, owner, stranger, org):
        created = api.post(
            "/api/v1/suppressions", headers=owner, json={"value": "protected@x.example"}
        ).json()

        response = api.delete(f"/api/v1/suppressions/{created['id']}", headers=stranger)
        assert response.status_code == 404

        still_there = await db.scalar(
            select(Suppression).where(Suppression.id == uuid.UUID(created["id"]))
        )
        assert still_there is not None

    async def test_removal_works_for_the_owner(self, api, owner):
        created = api.post(
            "/api/v1/suppressions", headers=owner, json={"value": "remove@x.example"}
        ).json()
        assert api.delete(f"/api/v1/suppressions/{created['id']}", headers=owner).status_code == 204


class TestMailbox:
    @pytest_asyncio.fixture(loop_scope="session")
    async def mailbox(self, db, org):
        row = Mailbox(
            org_id=org.id,
            from_name="Asha Rao",
            from_email=f"asha-{uuid.uuid4().hex[:8]}@outreach.example",
            daily_cap=100,
        )
        db.add(row)
        await db.commit()
        return row

    async def test_it_reports_the_cap_and_todays_position(self, api, owner, mailbox):
        body = api.get("/api/v1/mailbox", headers=owner).json()
        assert body["daily_cap"] == 100
        assert "sent_today" in body

    async def test_it_never_returns_a_secret(self, api, owner, mailbox):
        """Credentials live in the environment; the API does not read them at all."""
        body = api.get("/api/v1/mailbox", headers=owner).json()
        for leaked in ("password", "smtp_password", "secret", "token"):
            assert leaked not in {key.lower() for key in body}

    async def test_pausing_stops_the_send_loop_picking_it_up(self, db, api, owner, mailbox):
        response = api.post(
            f"/api/v1/mailbox/{mailbox.id}/pause", headers=owner, json={"reason": "on holiday"}
        )
        assert response.status_code == 200

        await db.refresh(mailbox)
        assert mailbox.paused is True
        assert mailbox.pause_reason == "on holiday"

    async def test_resuming_clears_the_reason(self, db, api, owner, mailbox):
        api.post(f"/api/v1/mailbox/{mailbox.id}/pause", headers=owner, json={})
        api.post(f"/api/v1/mailbox/{mailbox.id}/resume", headers=owner)

        await db.refresh(mailbox)
        assert mailbox.paused is False
        assert mailbox.pause_reason is None

    async def test_another_org_cannot_pause_it(self, db, api, stranger, mailbox):
        response = api.post(f"/api/v1/mailbox/{mailbox.id}/pause", headers=stranger, json={})
        assert response.status_code == 404

        await db.refresh(mailbox)
        assert mailbox.paused is False


class TestAdmin:
    async def test_needs_attention_counts_what_a_person_must_look_at(self, api, owner):
        body = api.get("/api/v1/admin/needs-attention", headers=owner).json()
        assert set(body) == {"unknown_sends", "dead_jobs", "paused_mailboxes"}

    async def test_dead_jobs_are_listed(self, db, api, owner, org):
        db.add(
            Job(
                org_id=org.id,
                kind="research_lead",
                status="dead",
                attempts=5,
                last_error="gave up",
                payload={},
            )
        )
        await db.commit()

        body = api.get("/api/v1/admin/jobs?status=dead", headers=owner).json()
        assert any(job["last_error"] == "gave up" for job in body["jobs"])

    async def test_usage_is_scoped_to_the_org(self, api, stranger):
        body = api.get("/api/v1/admin/usage", headers=stranger).json()
        assert body["today"] == []

    async def test_admin_views_need_a_token(self, api):
        assert api.get("/api/v1/admin/usage").status_code == 401
        assert api.get("/api/v1/admin/needs-attention").status_code == 401


class TestCreatingAMailbox:
    async def test_it_creates_one(self, api, owner):
        response = api.post(
            "/api/v1/mailboxes",
            headers=owner,
            json={"from_name": "Asha Rao", "from_email": "Asha@Example.com", "daily_cap": 80},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["from_email"] == "asha@example.com", "the address is normalised"
        assert body["daily_cap"] == 80

    async def test_it_stores_no_credentials(self, api, owner):
        """A mailbox row is policy — who it is from, how fast, whether paused — never a login.
        The credentials stay in `.env` where the operator put them."""
        body = api.post(
            "/api/v1/mailboxes",
            headers=owner,
            json={
                "from_name": "Asha",
                "from_email": "asha@example.com",
                "smtp_password": "hunter2-should-be-ignored",
            },
        ).json()
        assert "smtp_password" not in body
        assert "hunter2-should-be-ignored" not in str(body)

    async def test_an_address_is_required(self, api, owner):
        response = api.post("/api/v1/mailboxes", headers=owner, json={"from_name": "Asha"})
        assert response.status_code == 422

    async def test_a_nonsense_cap_is_refused(self, api, owner):
        """`daily_cap > 0` is a check constraint; a violation would otherwise surface as a 500."""
        response = api.post(
            "/api/v1/mailboxes",
            headers=owner,
            json={"from_name": "A", "from_email": "a@example.com", "daily_cap": 0},
        )
        assert response.status_code == 422


PERSONA = {
    "sender_name": "Asha Rao",
    "sender_role": "Founder",
    "agency_name": "Arkvane",
    "offer": "a two-week prototype",
    "goal": "book a 20-minute call",
    # Required by the Persona model: a draft needs a concrete next step to ask for.
    "cta_text": "Worth a 20-minute call next week?",
}


class TestCreatingACampaign:
    async def test_it_creates_one_in_draft(self, api, owner):
        response = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Spring", "persona": PERSONA}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "draft", "a new campaign never starts running"
        assert body["has_persona"] is True
        assert body["sender_name"] == "Asha Rao"

    async def test_an_invalid_persona_is_refused_with_its_field_errors(self, api, owner):
        """Rejected at the door rather than at drafting time: the persona is the list of things
        a draft may assert about us, and a malformed one would quietly produce a weaker draft."""
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Spring", "persona": {"sender_name": "Asha"}},
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["errors"], "the owner needs to know which field"

    async def test_an_unknown_field_in_the_persona_is_refused(self, api, owner):
        """`extra="forbid"`: a typo like `sender_rol` must not silently become no role at all."""
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Spring", "persona": {**PERSONA, "sender_rol": "Founder"}},
        )
        assert response.status_code == 422

    async def test_a_name_is_required(self, api, owner):
        response = api.post("/api/v1/campaigns", headers=owner, json={"persona": PERSONA})
        assert response.status_code == 422

    async def test_another_orgs_mailbox_cannot_be_attached(self, api, owner, stranger):
        """404 rather than 403: a mailbox elsewhere must look like one that is not there."""
        theirs = api.post(
            "/api/v1/mailboxes",
            headers=stranger,
            json={"from_name": "Rival", "from_email": "rival@example.com"},
        ).json()["id"]

        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Spring", "persona": PERSONA, "mailbox_id": theirs},
        )
        assert response.status_code == 404

    async def test_it_appears_in_the_list_afterwards(self, api, owner):
        api.post("/api/v1/campaigns", headers=owner, json={"name": "Autumn", "persona": PERSONA})
        names = [
            row["name"] for row in api.get("/api/v1/campaigns", headers=owner).json()["campaigns"]
        ]
        assert "Autumn" in names

    async def test_an_anonymous_caller_cannot_create_one(self, api):
        assert (
            api.post("/api/v1/campaigns", json={"name": "X", "persona": PERSONA}).status_code == 401
        )
