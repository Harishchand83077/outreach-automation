"""Provider keys and contact lookups end to end (F9, ADR-041, slice S13.5).

The tests that matter most are the ones about what a lookup *cannot* do: it cannot run without
this workspace's own key, cannot run on another workspace's key, cannot produce something the
send loop will pick up, and cannot create a lead for an address somebody opted out of.

Every provider call goes through the `mock` provider, which touches no network. That is not a
testing convenience bolted on: `mock` is in the check constraint because the owner holds no
provider key, so without it the feature's first run would be against a paid account.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import ContactCandidate, Lead, Member, Org, ProviderCredential, Suppression
from app.db.repo import OrgContext
from app.services import credentials
from app.services.contacts import keys, lookup

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, GROQ_API_KEY="gsk_test", CREDENTIAL_KEY=TEST_KEY
    )


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    """This slice's own app, because it needs a `CREDENTIAL_KEY`.

    The shared `api` fixture builds `Settings` without one, and a provider key is sealed with the
    same Fernet key as a mailbox credential -- so without this the save route fails inside
    `credentials.seal`. It is also the reason `SettingsDep` exists: the route reads the settings
    its app was built with rather than the module-level singleton, which in a test session has
    read the developer's `.env` at import time.
    """
    _, public = keypair
    application = create_app(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            GROQ_API_KEY="gsk_test",
            CREDENTIAL_KEY=TEST_KEY,
            CORS_ALLOWED_ORIGINS="https://dash.example",
        ),
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


async def _with_mock_key(api, owner) -> None:
    response = api.put(
        "/api/v1/provider-keys/mock", json={"api_key": "pretend", "label": "test"}, headers=owner
    )
    assert response.status_code == 200, response.text


class TestTheKeyIsTheWorkspacesOwn:
    async def test_a_lookup_without_a_key_is_refused_with_a_sentence(self, api, owner) -> None:
        """There is no shared key, and the refusal says so rather than erroring."""
        response = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "no mock key" in detail
        assert "no shared key" in detail

    async def test_the_key_is_never_returned_by_any_route(self, api, owner) -> None:
        await _with_mock_key(api, owner)

        listed = api.get("/api/v1/provider-keys", headers=owner)

        assert listed.status_code == 200
        assert "pretend" not in listed.text
        rows = {row["provider"]: row for row in listed.json()["providers"]}
        assert rows["mock"]["connected"] is True
        assert rows["mock"]["usable"] is True
        assert rows["hunter"]["connected"] is False

    async def test_it_is_stored_encrypted_rather_than_as_text(self, api, owner, db) -> None:
        await _with_mock_key(api, owner)

        row = await db.scalar(
            select(ProviderCredential).where(ProviderCredential.provider == "mock")
        )

        assert row is not None
        assert b"pretend" not in row.secret
        assert credentials.unseal(row.secret, _settings()) == "pretend"

    async def test_saving_again_replaces_rather_than_adds(self, api, owner, db, org) -> None:
        """Hard rule 8 in the write path: one account per provider per org, never several keys
        rotated to dodge a free tier. The old row is revoked, not deleted, so the change stays
        auditable."""
        await _with_mock_key(api, owner)
        api.put("/api/v1/provider-keys/mock", json={"api_key": "second"}, headers=owner)

        rows = (
            await db.scalars(select(ProviderCredential).where(ProviderCredential.org_id == org.id))
        ).all()
        live = [row for row in rows if row.revoked_at is None]

        assert len(rows) == 2
        assert len(live) == 1
        assert credentials.unseal(live[0].secret, _settings()) == "second"

    async def test_a_member_cannot_save_a_key_but_can_run_a_lookup(
        self, api, owner, member
    ) -> None:
        """`admin` guards the key; using it is the work.

        The same split as adding a send-allowlist entry against going `live`: one decides what
        the workspace pays for, the other is somebody doing their job.
        """
        refused = api.put("/api/v1/provider-keys/mock", json={"api_key": "x"}, headers=member)
        assert refused.status_code == 403

        await _with_mock_key(api, owner)
        allowed = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=member,
        )
        assert allowed.status_code == 201, allowed.text

    async def test_snov_needs_a_client_id_as_well(self, api, owner) -> None:
        response = api.put(
            "/api/v1/provider-keys/snov", json={"api_key": "secret-only"}, headers=owner
        )

        assert response.status_code == 422
        assert "client id" in response.json()["detail"]

    async def test_an_unknown_provider_is_refused(self, api, owner) -> None:
        response = api.put("/api/v1/provider-keys/clearbit", json={"api_key": "x"}, headers=owner)

        assert response.status_code == 422


class TestOneWorkspacesKeyIsNotAnothers:
    async def test_another_workspace_cannot_use_it(self, db, org) -> None:
        """ADR-004 asserted rather than assumed: `keys.live` is org-scoped.

        A shared key would be the whole feature's worst failure -- one workspace spending
        another's provider credits, with the provider's invoice as the only evidence.
        """
        mine = OrgContext(org_id=org.id, actor_id="me")
        await keys.store(db, mine, provider="mock", secret="mine", settings=_settings())
        other = Org(name="Another agency")
        db.add(other)
        await db.commit()

        theirs = OrgContext(org_id=other.id, actor_id="them")

        assert await keys.live(db, mine, provider="mock") is not None
        assert await keys.live(db, theirs, provider="mock") is None


class TestWhatALookupProduces:
    async def test_a_verified_address_and_a_guess_arrive_labelled(self, api, owner) -> None:
        """The distinction the screen exists to show: a guess is a naming pattern somebody
        inferred, and mailing one is how a bounce rate rises (ADR-034)."""
        await _with_mock_key(api, owner)

        response = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "Example.com"},
            headers=owner,
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["domain"] == "example.com"
        assert sorted(one["email_status"] for one in body["proposed"]) == ["guess", "verified"]
        assert "can be emailed until you import" in body["note"]

    async def test_nothing_it_writes_is_a_lead(self, api, owner, db, org) -> None:
        """The send loop reads `leads`. A proposal is not in it, so a proposal cannot be sent."""
        await _with_mock_key(api, owner)
        api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        )

        candidates = (
            await db.scalars(select(ContactCandidate).where(ContactCandidate.org_id == org.id))
        ).all()
        leads = (await db.scalars(select(Lead).where(Lead.org_id == org.id))).all()

        assert len(candidates) == 2
        assert leads == []

    async def test_a_second_lookup_does_not_propose_the_same_person_twice(self, api, owner) -> None:
        """Including somebody already dismissed. A second press must not resurrect a no."""
        await _with_mock_key(api, owner)
        first = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        ).json()
        api.post(f"/api/v1/contact-candidates/{first['proposed'][0]['id']}/dismiss", headers=owner)

        second = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        ).json()

        assert second["proposed"] == []
        # Said explicitly, because an empty list after a lookup that cost a credit otherwise
        # reads as a failure when it means we already had everybody.
        assert second["already_known"] == 2

    async def test_a_lookup_for_a_posting_in_another_workspace_resolves_to_nothing(
        self, api, owner
    ) -> None:
        await _with_mock_key(api, owner)

        response = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "job_post_id": str(uuid.uuid4())},
            headers=owner,
        )

        assert response.status_code == 422
        assert "does not exist in this workspace" in response.json()["detail"]

    async def test_a_lookup_with_no_domain_is_refused_before_a_credit_is_spent(
        self, api, owner
    ) -> None:
        await _with_mock_key(api, owner)

        response = api.post(
            "/api/v1/contact-candidates/lookup", json={"provider": "mock"}, headers=owner
        )

        assert response.status_code == 422
        assert "needs a company domain" in response.json()["detail"]


class TestTheBudget:
    async def test_it_is_counted_from_the_append_only_audit_trail(self, api, owner, db, org):
        """The budget and the trail are the same fact.

        `events` refuses UPDATE and DELETE by trigger, so this count cannot drift -- and a
        lookup that spent a credit while leaving no trace would be both an accounting hole and
        an audit one.
        """
        await _with_mock_key(api, owner)
        api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "one.example.com"},
            headers=owner,
        )
        api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "two.example.com"},
            headers=owner,
        )

        assert await lookup.used_today(db, org_id=org.id) == 2

    async def test_a_workspace_over_budget_is_refused(self, api, owner, db, org, monkeypatch):
        await _with_mock_key(api, owner)
        monkeypatch.setattr(lookup, "DAILY_LOOKUP_BUDGET", 1)
        first = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "one.example.com"},
            headers=owner,
        )
        assert first.status_code == 201

        second = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "two.example.com"},
            headers=owner,
        )

        assert second.status_code == 422
        # The sentence says whose limit it is, because a user told "you are out of lookups" will
        # go and check their provider dashboard, find credit there, and report a bug.
        assert "The limit is ours, not the provider's" in second.json()["detail"]

    async def test_one_workspace_at_its_limit_does_not_block_another(self, db, org) -> None:
        """The same rule as the job queue and the company cache: a limit is per workspace or it
        is a shared outage."""
        other = Org(name="Unaffected")
        db.add(other)
        await db.commit()

        assert await lookup.used_today(db, org_id=other.id) == 0


class TestImportingOne:
    async def test_it_becomes_a_lead_through_the_ordinary_importer(
        self, api, owner, db, campaign
    ) -> None:
        """One lead-creation path, so there is one place the suppression check lives."""
        await _with_mock_key(api, owner)
        proposed = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        ).json()["proposed"]
        verified = next(one for one in proposed if one["email_status"] == "verified")

        response = api.post(
            f"/api/v1/contact-candidates/{verified['id']}/import",
            json={"campaign_id": str(campaign.id)},
            headers=owner,
        )

        assert response.status_code == 201, response.text
        lead = await db.scalar(select(Lead).where(Lead.id == uuid.UUID(response.json()["lead_id"])))
        assert lead is not None
        assert lead.email_norm == verified["email"]
        # The ordinary starting state. Nothing here approves anything, so the human gate stands.
        assert lead.state == "NEW"
        assert lead.import_batch_id is not None

    async def test_a_suppressed_address_is_refused(self, api, owner, db, org, campaign) -> None:
        """The most important test in this file.

        A provider will happily propose somebody who asked us never to write again -- it has no
        idea they did. Importing through `importer.commit` is what makes that refusal automatic,
        and a second lead-creation path is exactly where it would have been forgotten.
        """
        await _with_mock_key(api, owner)
        proposed = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        ).json()["proposed"]
        verified = next(one for one in proposed if one["email_status"] == "verified")
        db.add(Suppression(org_id=org.id, kind="email", value=verified["email"], reason="opt_out"))
        await db.commit()

        response = api.post(
            f"/api/v1/contact-candidates/{verified['id']}/import",
            json={"campaign_id": str(campaign.id)},
            headers=owner,
        )

        assert response.status_code == 422, response.text
        leads = (await db.scalars(select(Lead).where(Lead.org_id == org.id))).all()
        assert leads == []

    async def test_importing_twice_is_refused(self, api, owner, campaign) -> None:
        await _with_mock_key(api, owner)
        proposed = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        ).json()["proposed"]
        verified = next(one for one in proposed if one["email_status"] == "verified")
        body = {"campaign_id": str(campaign.id)}
        assert (
            api.post(
                f"/api/v1/contact-candidates/{verified['id']}/import", json=body, headers=owner
            ).status_code
            == 201
        )

        again = api.post(
            f"/api/v1/contact-candidates/{verified['id']}/import", json=body, headers=owner
        )

        assert again.status_code == 409

    async def test_an_imported_contact_cannot_be_dismissed(self, api, owner, campaign) -> None:
        await _with_mock_key(api, owner)
        proposed = api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        ).json()["proposed"]
        verified = next(one for one in proposed if one["email_status"] == "verified")
        api.post(
            f"/api/v1/contact-candidates/{verified['id']}/import",
            json={"campaign_id": str(campaign.id)},
            headers=owner,
        )

        response = api.post(f"/api/v1/contact-candidates/{verified['id']}/dismiss", headers=owner)

        assert response.status_code == 409

    async def test_a_candidate_from_another_workspace_is_not_found(
        self, api, owner, campaign
    ) -> None:
        response = api.post(
            f"/api/v1/contact-candidates/{uuid.uuid4()}/import",
            json={"campaign_id": str(campaign.id)},
            headers=owner,
        )

        assert response.status_code == 404


class TestTenantIsolation:
    async def test_a_lookups_proposals_are_invisible_to_another_workspace(
        self, api, owner, db, keypair
    ) -> None:
        await _with_mock_key(api, owner)
        api.post(
            "/api/v1/contact-candidates/lookup",
            json={"provider": "mock", "domain": "example.com"},
            headers=owner,
        )
        private, _ = keypair
        other = Org(name="Separate")
        db.add(other)
        await db.flush()
        subject = f"other-{uuid.uuid4()}"
        db.add(Member(org_id=other.id, user_id=subject, email="o@example.com", role="owner"))
        await db.commit()
        stranger = {"Authorization": f"Bearer {token_for(private, subject=subject)}"}

        listed = api.get("/api/v1/contact-candidates", headers=stranger)

        assert listed.status_code == 200
        assert listed.json()["contact_candidates"] == []
