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
from app.db.models import Job, Lead, Mailbox, Member, Message, Org, Suppression

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


TEMPLATES = {
    "initial": {
        "subject": "{{company}} and a two-week prototype",
        "body": "Hi {{first_name|there}},\n\nShort note.\n\n{{sender_name}}",
    },
    "followups": [
        {
            "subject": "Re: {{company}}",
            "body": "Following up.\n\n{{sender_name}}",
            "after_days": 3,
        }
    ],
}


class TestCampaignMode:
    """`research` drafts with a model from real research; `template` renders the operator's own
    text with merge fields.

    Two different jobs -- agency outreach, and a job hunt or any list where everybody genuinely
    should get the same message with their own name in it -- and the second one should not have
    a model writing the email at all.
    """

    async def test_research_is_the_default(self, api, owner):
        """Every campaign that already exists is a research campaign, so it has to be the
        default, or the migration would have changed what they send."""
        response = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Default", "persona": PERSONA}
        )
        assert response.status_code == 201
        assert response.json()["mode"] == "research"
        assert response.json()["has_templates"] is False

    async def test_a_template_campaign_carries_its_emails(self, api, owner):
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Job hunt",
                "mode": "template",
                "persona": PERSONA,
                "templates": TEMPLATES,
            },
        )
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["mode"] == "template"
        assert body["has_templates"] is True

        detail = api.get(f"/api/v1/campaigns/{body['id']}", headers=owner).json()
        assert detail["templates"]["initial"]["subject"].startswith("{{company}}")
        assert detail["templates"]["followups"][0]["after_days"] == 3

    async def test_template_mode_without_templates_is_refused(self, api, owner):
        """An empty one looks healthy -- draft status, a mailbox attached, leads imported --
        and then drafts nothing. That exact failure has happened three times in this project."""
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Empty", "mode": "template", "persona": PERSONA},
        )
        assert response.status_code == 422
        assert "templates.initial" in str(response.json()["detail"]["errors"])

    async def test_another_tools_placeholder_syntax_is_refused(self, api, owner):
        """`{first_name}` would be emailed verbatim, and the first person to notice it would be
        a recipient."""
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Bad syntax",
                "mode": "template",
                "persona": PERSONA,
                "templates": {"initial": {"subject": "Hi", "body": "Hi {first_name},"}},
            },
        )
        assert response.status_code == 422
        assert "merge field" in str(response.json()["detail"]["errors"])

    async def test_an_unknown_mode_is_refused(self, api, owner):
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Odd", "mode": "freestyle", "persona": PERSONA},
        )
        assert response.status_code == 422

    async def test_a_research_campaign_may_not_carry_templates(self, api, owner):
        """Two sources of truth for what the email says, with the mode deciding which is read.
        Storing the other one is how they drift."""
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Confused", "persona": PERSONA, "templates": TEMPLATES},
        )
        assert response.status_code == 422

    async def test_the_template_can_be_edited_afterwards(self, api, owner):
        created = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Editable",
                "mode": "template",
                "persona": PERSONA,
                "templates": TEMPLATES,
            },
        ).json()

        response = api.patch(
            f"/api/v1/campaigns/{created['id']}",
            headers=owner,
            json={
                "templates": {
                    "initial": {"subject": "New subject", "body": "Hi {{first_name|there}}"}
                }
            },
        )
        assert response.status_code == 200, response.json()
        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()
        assert detail["templates"]["initial"]["subject"] == "New subject"
        assert detail["templates"]["followups"] == []

    async def test_an_edit_that_breaks_the_template_leaves_the_old_one(self, api, owner):
        created = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Breakable",
                "mode": "template",
                "persona": PERSONA,
                "templates": TEMPLATES,
            },
        ).json()

        response = api.patch(
            f"/api/v1/campaigns/{created['id']}",
            headers=owner,
            json={"templates": {"initial": {"subject": "Hi\nBcc: x@example.com", "body": "Hi"}}},
        )
        assert response.status_code == 422

        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()
        assert detail["templates"]["initial"]["subject"].startswith("{{company}}"), (
            "a refused edit must leave the working template in place"
        )


class TestWhatEachModeAsksOfThePersona:
    """A job hunter has no agency and no offer, and typing something to get past a form is the
    opposite of what hard rule 6 is for: a persona filled in to satisfy a validator is a persona
    the drafter would go on to quote (ADR-013).
    """

    async def test_a_template_campaign_needs_only_a_name(self, api, owner):
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Job hunt",
                "mode": "template",
                "persona": {"sender_name": "Harish Chand"},
                "templates": TEMPLATES,
            },
        )
        assert response.status_code == 201, response.json()
        assert response.json()["sender_name"] == "Harish Chand"

    async def test_a_template_campaign_still_needs_a_name(self, api, owner):
        """The footer has to say who wrote, whoever wrote it."""
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Anonymous",
                "mode": "template",
                "persona": {},
                "templates": TEMPLATES,
            },
        )
        assert response.status_code == 422

    async def test_a_research_campaign_is_refused_without_an_offer(self, api, owner):
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Thin", "persona": {"sender_name": "Harish Chand"}},
        )
        assert response.status_code == 422
        errors = {e["field"]: e["message"] for e in response.json()["detail"]["errors"]}
        assert "persona.offer" in errors
        assert "persona.agency_name" in errors
        assert len(errors["persona.offer"]) > 20, (
            "naming the field is not enough -- it has to say what the field is for"
        )

    async def test_a_research_campaign_cannot_be_patched_into_that_state(self, api, owner):
        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Full", "persona": PERSONA}
        ).json()

        response = api.patch(
            f"/api/v1/campaigns/{created['id']}",
            headers=owner,
            json={"persona": {"sender_name": "Harish Chand"}},
        )

        assert response.status_code == 422
        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()
        assert detail["persona"]["offer"] == PERSONA["offer"], (
            "a refused patch must leave the working persona in place"
        )

    async def test_a_template_campaign_can_be_patched_to_a_light_persona(self, api, owner):
        created = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Switchable",
                "mode": "template",
                "persona": PERSONA,
                "templates": TEMPLATES,
            },
        ).json()

        response = api.patch(
            f"/api/v1/campaigns/{created['id']}",
            headers=owner,
            json={"persona": {"sender_name": "Harish Chand"}},
        )

        assert response.status_code == 200, response.json()

    async def test_a_research_campaign_with_a_thin_persona_says_so(self, api, owner, db):
        """A row edited by hand, or one created before this rule existed. It would otherwise
        draft nothing and give no reason."""
        from app.db.models import Campaign as C

        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Hand edited", "persona": PERSONA}
        ).json()
        campaign = await db.get(C, uuid.UUID(created["id"]))
        campaign.persona = {"sender_name": "Harish Chand"}
        await db.commit()

        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()

        blocker = next(b for b in detail["blockers"] if b["code"] == "persona_incomplete")
        assert "offer" in blocker["message"]


class TestWhyNothingSends:
    """`blockers` answers the one question the dashboard could not: I approved a draft and no
    email arrived -- why?

    The reasons lived in the worker's tick report, which reaches the log and nothing else. So a
    campaign whose persona the send loop could not read showed a running status, a mailbox and
    approved drafts, and gave the operator nothing to act on.
    """

    async def test_a_fresh_campaign_says_what_is_missing(self, api, owner):
        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Fresh", "persona": PERSONA}
        ).json()

        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()

        codes = {b["code"] for b in detail["blockers"]}
        assert "no_mailbox" in codes
        assert "not_running" in codes, "a draft campaign sends nothing, and should say so"

    async def test_an_unreadable_persona_is_named_by_field(self, api, owner, db):
        """A hand-edited row is the only way this happens, and it is how it happened.

        "Unreadable" means the model refuses it, which since ADR-013 is a narrower thing than
        "thin": an empty `sender_name` is unreadable, a missing agency is merely incomplete and
        is reported separately."""
        from app.db.models import Campaign as C

        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Broken", "persona": PERSONA}
        ).json()
        campaign = await db.get(C, uuid.UUID(created["id"]))
        campaign.persona = {"sender_name": "", "max_words": 9000}
        await db.commit()

        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()

        blocker = next(b for b in detail["blockers"] if b["code"] == "persona_unreadable")
        assert "sender_name" in blocker["message"], blocker
        assert "max_words" in blocker["message"], blocker

    async def test_the_message_never_echoes_what_is_stored(self, api, owner, db):
        """Field names travel to a browser and into screenshots; values do not."""
        from app.db.models import Campaign as C

        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Quiet", "persona": PERSONA}
        ).json()
        campaign = await db.get(C, uuid.UUID(created["id"]))
        campaign.settings = {"send_days": ["monday-is-not-an-integer"]}
        await db.commit()

        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()

        blocker = next(b for b in detail["blockers"] if b["code"] == "settings_unreadable")
        assert "send_days" in blocker["message"]
        assert "monday-is-not-an-integer" not in blocker["message"]

    async def test_a_broken_campaign_is_still_openable(self, api, owner, db):
        """`campaign_summary` rendered the settings through `CampaignSettings`, so an unreadable
        row made the detail route a 500 -- and the one campaign an operator most needs to open,
        the broken one, was the only one they could not. Found by the test above failing with a
        `KeyError` rather than an assertion."""
        from app.db.models import Campaign as C

        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Openable", "persona": PERSONA}
        ).json()
        campaign = await db.get(C, uuid.UUID(created["id"]))
        campaign.settings = {"window_start": "half past ten"}
        await db.commit()

        response = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner)

        assert response.status_code == 200, response.text
        body = response.json()
        # The detail route returns the row as stored, on purpose: the settings form edits what
        # is actually there, and showing a default where a bad value sits would hide it. What
        # matters is that the screen renders at all, and says what is wrong.
        assert body["settings"]["window_start"] == "half past ten"
        assert any(b["code"] == "settings_unreadable" for b in body["blockers"])

    async def test_the_list_of_campaigns_survives_a_broken_one(self, api, owner, db):
        """The same function builds every row of the list, so one bad campaign took out the
        whole screen -- including every healthy campaign beside it."""
        from app.db.models import Campaign as C

        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Bad row", "persona": PERSONA}
        ).json()
        campaign = await db.get(C, uuid.UUID(created["id"]))
        campaign.settings = {"daily_cap": "lots"}
        await db.commit()

        response = api.get("/api/v1/campaigns", headers=owner)

        assert response.status_code == 200, response.text
        assert any(row["name"] == "Bad row" for row in response.json()["campaigns"])

    async def test_a_ready_campaign_has_no_blockers(self, api, owner, db):

        mailbox = api.post(
            "/api/v1/mailboxes",
            headers=owner,
            json={"from_name": "Asha", "from_email": "asha@example.com"},
        ).json()
        created = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={"name": "Ready", "persona": PERSONA, "mailbox_id": mailbox["id"]},
        ).json()
        api.patch(f"/api/v1/campaigns/{created['id']}", headers=owner, json={"status": "running"})

        detail = api.get(f"/api/v1/campaigns/{created['id']}", headers=owner).json()

        codes = {b["code"] for b in detail["blockers"]}
        # The send mode is whatever the test settings say, and that is itself a blocker worth
        # reporting -- so assert only that no *campaign* fault is claimed.
        assert codes <= {"send_mode_disabled"}, detail["blockers"]


class TestImportRoutesByMode:
    """A campaign in `template` mode has nothing to research, so importing must not queue it.

    This is the chaining failure in its other form. Research queuing nothing stopped the
    pipeline; research being queued *pointlessly* would spend a page fetch and a model call per
    lead to produce findings that no template reads -- against a free tier whose daily limit is
    the real throughput ceiling.
    """

    def _upload(self, rows: int = 2):
        lines = ["email,full name,company,website,title"]
        for n in range(1, rows + 1):
            lines.append(
                f"lead{n}@example.com,Lead {n},Company {n},https://company{n}.example,Founder"
            )
        return {"file": ("leads.csv", ("\n".join(lines) + "\n").encode(), "text/csv")}

    async def test_a_research_campaign_queues_research(self, api, owner, db):

        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Researching", "persona": PERSONA}
        ).json()
        api.post(
            f"/api/v1/campaigns/{created['id']}/imports/commit",
            headers=owner,
            files=self._upload(),
        )

        kinds = (await db.execute(select(Job.kind))).scalars().all()
        assert "research_lead" in kinds

    async def test_a_template_campaign_queues_drafting_and_no_research(self, api, owner, db):
        from app.db.models import Campaign as C

        created = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Templating",
                "mode": "template",
                "persona": PERSONA,
                "templates": TEMPLATES,
            },
        ).json()
        response = api.post(
            f"/api/v1/campaigns/{created['id']}/imports/commit",
            headers=owner,
            files=self._upload(),
        )
        assert response.status_code == 200, response.text
        assert response.json()["imported"] == 2

        campaign_id = uuid.UUID(created["id"])
        lead_ids = (
            (await db.execute(select(Lead.id).where(Lead.campaign_id == campaign_id)))
            .scalars()
            .all()
        )
        assert lead_ids
        jobs = (await db.execute(select(Job.kind).where(Job.lead_id.in_(lead_ids)))).scalars().all()
        assert set(jobs) == {"draft_lead"}, jobs
        assert await db.scalar(select(C.mode).where(C.id == campaign_id)) == "template"


class TestUndoingAnImport:
    """ "I uploaded the wrong sheet" had no answer. The only way back was `DELETE /leads/{id}`,
    one lead at a time, and that is the **erasure** path for a data-subject request -- it
    deliberately leaves a hashed suppression behind, so undoing a mistaken import through it
    would permanently block every address in the sheet and the corrected upload would silently
    skip exactly the people it was meant to reach.
    """

    def _sheet(self, *addresses: str):
        lines = ["email,full name,company"]
        for n, address in enumerate(addresses, start=1):
            lines.append(f"{address},Lead {n},Company {n}")
        body = ("\n".join(lines) + "\n").encode()
        return {"file": ("leads.csv", body, "text/csv")}

    async def test_the_commit_returns_the_batch_id(self, api, owner, db, org):
        """Without it there is nothing to undo *with*, and a dashboard left to find "the most
        recent batch" would undo the wrong one for anybody with two tabs open."""
        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Undoable", "persona": PERSONA}
        ).json()

        response = api.post(
            f"/api/v1/campaigns/{created['id']}/imports/commit",
            headers=owner,
            files=self._sheet("undo1@example.com"),
        )

        assert response.status_code == 200, response.text
        assert response.json()["batch_id"], response.json()

    async def test_undoing_removes_the_leads(self, api, owner):
        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Undone", "persona": PERSONA}
        ).json()
        batch_id = api.post(
            f"/api/v1/campaigns/{created['id']}/imports/commit",
            headers=owner,
            files=self._sheet("undo2@example.com", "undo3@example.com"),
        ).json()["batch_id"]

        response = api.delete(
            f"/api/v1/campaigns/{created['id']}/imports/{batch_id}", headers=owner
        )

        assert response.status_code == 200, response.text
        assert response.json()["removed"] == 2
        leads = api.get(f"/api/v1/campaigns/{created['id']}/leads", headers=owner).json()
        assert leads["leads"] == []

    async def test_the_corrected_sheet_imports_normally_afterwards(self, api, owner):
        """The consequence of suppressing nothing, demonstrated rather than asserted about."""
        created = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Second go", "persona": PERSONA}
        ).json()
        batch_id = api.post(
            f"/api/v1/campaigns/{created['id']}/imports/commit",
            headers=owner,
            files=self._sheet("undo4@example.com"),
        ).json()["batch_id"]
        api.delete(f"/api/v1/campaigns/{created['id']}/imports/{batch_id}", headers=owner)

        again = api.post(
            f"/api/v1/campaigns/{created['id']}/imports/commit",
            headers=owner,
            files=self._sheet("undo4@example.com"),
        )

        assert again.status_code == 200, again.text
        assert again.json()["imported"] == 1, (
            "an undo that suppressed the address would skip it here, and say nothing"
        )

    async def test_a_member_may_not_undo_an_import(self, api, db, org, keypair):
        """It deletes rows, so it is admin-only -- the same reasoning as erasing a lead. The
        role gate comes before the lookup, so any id demonstrates it."""
        private, _ = keypair
        subject = f"member-{uuid.uuid4()}"
        db.add(Member(org_id=org.id, user_id=subject, email="member@example.com", role="member"))
        await db.commit()
        member = {"Authorization": f"Bearer {token_for(private, subject=subject)}"}

        response = api.delete(
            f"/api/v1/campaigns/{uuid.uuid4()}/imports/{uuid.uuid4()}", headers=member
        )

        assert response.status_code == 403

    async def test_another_orgs_campaign_is_not_found(self, api, stranger, owner):
        mine = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Mine too", "persona": PERSONA}
        ).json()

        response = api.delete(
            f"/api/v1/campaigns/{mine['id']}/imports/{uuid.uuid4()}", headers=stranger
        )

        assert response.status_code == 404


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

    async def test_a_booking_link_alone_is_a_complete_persona(self, api, owner):
        """The dashboard offers a booking link beside the call to action, and either satisfies
        the rule. It only offered the call to action before, and labelled it optional, so the
        obvious way through the form produced a 422 naming a field that was not on screen."""
        persona = {k: v for k, v in PERSONA.items() if k != "cta_text"}
        response = api.post(
            "/api/v1/campaigns",
            headers=owner,
            json={
                "name": "Link only",
                "persona": {**persona, "booking_url": "https://cal.example.com/asha"},
            },
        )
        assert response.status_code == 201, response.json()

    async def test_neither_is_refused_naming_the_rule(self, api, owner):
        """The form guards this itself now; the API must still say which rule was broken, or a
        caller that is not our dashboard gets 'invalid' and nothing to act on.

        Since ADR-013 the rule is asked of a *research* campaign rather than of every persona,
        so this is the same requirement checked one layer out."""
        persona = {k: v for k, v in PERSONA.items() if k != "cta_text"}
        response = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "No ask", "persona": persona}
        )
        assert response.status_code == 422
        errors = {e["field"]: e["message"] for e in response.json()["detail"]["errors"]}
        assert "persona.cta_text" in errors, errors
        assert "next step" in errors["persona.cta_text"]

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


class TestUpdatingACampaign:
    async def test_a_mailbox_can_be_attached_afterwards(self, api, owner):
        """The hole this closes: creation allowed a campaign with no mailbox, and nothing could
        add one later — so it could never send and could never be repaired, only abandoned."""
        campaign = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Later", "persona": PERSONA}
        ).json()
        assert campaign["mailbox_id"] is None

        mailbox = api.post(
            "/api/v1/mailboxes",
            headers=owner,
            json={"from_name": "Asha", "from_email": "asha@example.com"},
        ).json()

        updated = api.patch(
            f"/api/v1/campaigns/{campaign['id']}", headers=owner, json={"mailbox_id": mailbox["id"]}
        )
        assert updated.status_code == 200
        assert updated.json()["mailbox_id"] == mailbox["id"]

    async def test_a_campaign_can_be_renamed(self, api, owner):
        campaign = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "test_campaign", "persona": PERSONA}
        ).json()
        renamed = api.patch(
            f"/api/v1/campaigns/{campaign['id']}", headers=owner, json={"name": "Seed-stage SaaS"}
        ).json()
        assert renamed["name"] == "Seed-stage SaaS"

    async def test_an_unknown_status_is_refused_with_the_allowed_list(self, api, owner):
        """A check-constraint violation would otherwise surface as a 500 saying nothing."""
        campaign = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "S", "persona": PERSONA}
        ).json()
        response = api.patch(
            f"/api/v1/campaigns/{campaign['id']}", headers=owner, json={"status": "sending"}
        )
        assert response.status_code == 422
        assert "running" in response.json()["detail"]

    async def test_another_orgs_campaign_cannot_be_edited(self, api, owner, stranger):
        campaign = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Mine", "persona": PERSONA}
        ).json()
        response = api.patch(
            f"/api/v1/campaigns/{campaign['id']}", headers=stranger, json={"name": "Theirs"}
        )
        assert response.status_code == 404


class TestDeletingACampaign:
    async def test_an_untouched_campaign_is_deleted(self, api, owner):
        campaign = api.post(
            "/api/v1/campaigns", headers=owner, json={"name": "Mistake", "persona": PERSONA}
        ).json()

        response = api.delete(f"/api/v1/campaigns/{campaign['id']}", headers=owner)
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        names = [c["name"] for c in api.get("/api/v1/campaigns", headers=owner).json()["campaigns"]]
        assert "Mistake" not in names

    async def test_one_that_has_sent_is_archived_instead(self, db, api, owner, org, campaign):
        """Its messages are the record of what left the building, and the opt-outs of the people
        it contacted hang off that history. Tidying a list must not erase that."""
        lead = Lead(
            org_id=org.id,
            campaign_id=campaign.id,
            email_norm="sent@example.com",
            email_raw="sent@example.com",
            full_name="Sent Person",
            company_name="Acme",
            state="EMAIL_SENT",
        )
        db.add(lead)
        await db.flush()
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                status="sent",
                subject="Hello",
                body_text="Body",
                idempotency_key=f"archive-{uuid.uuid4()}",
            )
        )
        await db.commit()

        response = api.delete(f"/api/v1/campaigns/{campaign.id}", headers=owner)
        assert response.status_code == 200
        body = response.json()
        assert body["deleted"] is False
        assert body["archived"] is True
        assert body["messages_sent"] == 1

        await db.refresh(campaign)
        assert campaign.status == "archived"

    async def test_force_deletes_but_keeps_the_opt_out(self, db, api, owner, org, campaign):
        """`force` is only safe because every lead goes through the erasure path, which leaves a
        hashed suppression for anyone who had opted out."""
        lead = Lead(
            org_id=org.id,
            campaign_id=campaign.id,
            email_norm="optout@example.com",
            email_raw="optout@example.com",
            full_name="Opted Out",
            company_name="Acme",
            state="OPTED_OUT",
        )
        db.add(lead)
        await db.flush()
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                status="sent",
                subject="Hello",
                body_text="Body",
                idempotency_key=f"force-{uuid.uuid4()}",
            )
        )
        await db.commit()

        response = api.delete(f"/api/v1/campaigns/{campaign.id}?force=true", headers=owner)
        assert response.status_code == 200
        assert response.json()["deleted"] is True

        from app.db import outbox

        assert await outbox.is_suppressed(db, org_id=org.id, email="optout@example.com"), (
            "deleting a campaign must not make the people it contacted reachable again"
        )

    async def test_another_orgs_campaign_cannot_be_deleted(self, api, stranger, campaign):
        assert api.delete(f"/api/v1/campaigns/{campaign.id}", headers=stranger).status_code == 404
