"""The settings surface: editable campaign rules, and a read-only view of the send guardrails.

Two things here are different in kind, and the split is the whole point.

**Campaign settings are editable.** The window, the send days, the cap and the follow-up rules
live in `campaigns.settings` as JSONB, and nothing in the API wrote them until now -- so the only
way to change a send window was raw SQL against production. That is exactly how a malformed
`send_days` got in and took the send loop down for every mailbox. Editing them through
`CampaignSettings` makes a bad value a 422 that names the field, instead of a worker that
crash-loops with the reason visible only in a traceback.

**`SEND_MODE` is not editable, by anything, ever.** It is read from the environment at process
start and checked in exactly one function (hard rule 1). A UI toggle for it would mean a stolen
token, or one mistaken click, could begin sending real mail to real people -- and the whole
design is that starting to send is a deliberate act by the owner on the server. The settings
endpoint *reports* the mode so an operator can see what the running system will do, and offers
no way to change it. The last test asserts that.
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
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Member

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


class TestCampaignSettingsCanBeEdited:
    async def test_the_window_can_be_changed(self, api, owner, campaign):
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"window_start": "09:00:00", "window_end": "18:00:00"}},
            headers=owner,
        )

        assert response.status_code == 200, response.text
        assert response.json()["settings"]["window_start"] == "09:00:00"

    async def test_weekday_names_are_refused_with_the_field_named(self, api, owner, campaign):
        """The production incident, as a 422.

        `send_days` is a list of integers (0 = Monday). The hand-written UPDATE that put names
        there stopped sending for every mailbox until the row was corrected.
        """
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"send_days": ["mon", "tue"]}},
            headers=owner,
        )

        assert response.status_code == 422
        body = response.json()["detail"]
        assert any("send_days" in error["field"] for error in body["errors"]), body

    async def test_a_window_that_is_not_an_interval_is_refused(self, api, owner, campaign):
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"window_start": "18:00:00", "window_end": "09:00:00"}},
            headers=owner,
        )

        assert response.status_code == 422

    async def test_an_empty_send_days_is_refused(self, api, owner, campaign):
        """A campaign that can never send is a silent failure, so it is refused at the door."""
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"send_days": []}},
            headers=owner,
        )

        assert response.status_code == 422

    async def test_auto_send_cannot_be_enabled_through_the_api(self, api, owner, campaign):
        """A locked product rule, and a settings form is the obvious place to try it."""
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"approval_mode": "auto_send_if_confidence"}},
            headers=owner,
        )

        assert response.status_code == 422

    async def test_a_partial_edit_keeps_the_rest(self, api, owner, campaign):
        """A form that posts one field must not reset the others to their defaults."""
        api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"daily_cap": 40, "followup_after_days": 5}},
            headers=owner,
        )
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"daily_cap": 55}},
            headers=owner,
        )

        settings = response.json()["settings"]
        assert settings["daily_cap"] == 55
        assert settings["followup_after_days"] == 5

    async def test_what_is_stored_is_always_readable_by_the_send_loop(
        self, api, owner, campaign, db
    ):
        """The guarantee that matters: whatever this endpoint accepts, the worker can parse.

        `SendLoop` validates `campaigns.settings` on every cycle, so anything this route can
        write and the loop cannot read is an outage.
        """
        from app.domain.campaign import CampaignSettings

        api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"send_days": [0, 2, 4], "daily_cap": 30}},
            headers=owner,
        )
        await db.refresh(campaign)

        parsed = CampaignSettings.model_validate(campaign.settings)
        assert parsed.send_days == [0, 2, 4]
        assert parsed.daily_cap == 30


class TestTheSendingConfigIsReadOnly:
    async def test_it_reports_the_mode_the_worker_will_use(self, api, owner):
        response = api.get("/api/v1/admin/sending-config", headers=owner)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["send_mode"] in ("disabled", "allowlist", "live")
        assert body["editable"] is False

    async def test_it_never_returns_the_allowlist_itself(self, api, owner):
        """A count is what an operator needs. The addresses themselves are personal data."""
        response = api.get("/api/v1/admin/sending-config", headers=owner)

        body = response.json()
        assert isinstance(body["allowlist_size"], int)
        assert "allowlist" not in body
        assert "send_allowlist" not in body

    async def test_it_returns_no_credential(self, api, owner):
        """It names the sending account; it reveals nothing that authenticates as it."""
        rendered = api.get("/api/v1/admin/sending-config", headers=owner).text.lower()

        for forbidden in ("password", "api_key", "secret", "token"):
            assert forbidden not in rendered

    async def test_it_needs_authentication(self, api):
        assert api.get("/api/v1/admin/sending-config").status_code == 401

    async def test_no_route_can_change_the_send_mode(self, api, owner, campaign):
        """Hard rule 1: the mode comes from the environment, and nothing else sets it."""
        from app.core.settings import config

        before = config.SEND_MODE
        for payload in (
            {"send_mode": "live"},
            {"status": "running", "send_mode": "live"},
            {"settings": {"send_mode": "live"}},
        ):
            api.patch(f"/api/v1/campaigns/{campaign.id}", json=payload, headers=owner)

        assert config.SEND_MODE == before
        reported = api.get("/api/v1/admin/sending-config", headers=owner).json()["send_mode"]
        assert reported == before


@pytest_asyncio.fixture(loop_scope="session")
async def member(db, org, keypair):
    """Somebody in the org with the lowest role."""
    private, _ = keypair
    subject = f"member-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email="member@example.com", role="member"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


class TestOnlyAnAdminTouchesSettings:
    """Settings are gated by role, not merely hidden in the UI.

    A hidden button is a hidden button; the endpoint is what decides. Everything reachable from
    the settings screen changes what gets sent or who may be contacted again, so `member` can
    read the dashboard and judge drafts but cannot change the rules underneath them.
    """

    async def test_a_member_cannot_read_the_sending_config(self, api, member):
        assert api.get("/api/v1/admin/sending-config", headers=member).status_code == 403

    async def test_a_member_cannot_change_campaign_settings(self, api, member, campaign):
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}",
            json={"settings": {"daily_cap": 5}},
            headers=member,
        )

        assert response.status_code == 403

    async def test_a_member_cannot_start_a_campaign(self, api, member, campaign):
        """Setting a campaign running is a sending decision, so it sits behind the same gate."""
        response = api.patch(
            f"/api/v1/campaigns/{campaign.id}", json={"status": "running"}, headers=member
        )

        assert response.status_code == 403

    async def test_a_member_cannot_remove_a_suppression(self, api, member, db, org):
        """Adding to the list is protective; removing it undoes somebody's opt-out."""
        from app.db.models import Suppression

        row = Suppression(org_id=org.id, kind="email", value="gone@example.com", reason="opt_out")
        db.add(row)
        await db.commit()

        assert api.delete(f"/api/v1/suppressions/{row.id}", headers=member).status_code == 403

    async def test_a_member_can_still_review(self, api, member):
        """The gate is on the rules, not on the work. A reviewer must still be able to review."""
        assert api.get("/api/v1/review/drafts", headers=member).status_code == 200

    async def test_an_admin_can_do_all_of_it(self, api, owner, campaign):
        assert api.get("/api/v1/admin/sending-config", headers=owner).status_code == 200
        assert (
            api.patch(
                f"/api/v1/campaigns/{campaign.id}",
                json={"settings": {"daily_cap": 60}},
                headers=owner,
            ).status_code
            == 200
        )


class TestChangingTheSendMode:
    """ADR-010. The most consequential request the API accepts, so every gate is tested.

    The gates live where the value is *written*, not in the guard that reads it: owner only,
    `live` refused outside production, a typed confirmation for `live`, and an audit row.
    """

    async def test_an_owner_can_restrict_sending(self, api, owner, db, org):
        """Restricting is the case that must always work. A kill switch that needs an SSH
        session is not a kill switch."""
        response = api.patch(
            "/api/v1/admin/sending-config", json={"send_mode": "disabled"}, headers=owner
        )

        assert response.status_code == 200, response.text
        assert response.json()["send_mode"] == "disabled"
        await db.refresh(org)
        assert org.send_mode == "disabled"

    async def test_it_records_who_changed_it(self, api, owner, db, org):
        api.patch("/api/v1/admin/sending-config", json={"send_mode": "allowlist"}, headers=owner)
        await db.refresh(org)

        assert org.send_mode_set_by == "owner@example.com"
        assert org.send_mode_set_at is not None

    async def test_null_hands_authority_back_to_the_environment(self, api, owner, db, org):
        api.patch("/api/v1/admin/sending-config", json={"send_mode": "disabled"}, headers=owner)
        response = api.patch(
            "/api/v1/admin/sending-config", json={"send_mode": None}, headers=owner
        )

        assert response.status_code == 200
        assert response.json()["source"] == "environment"
        await db.refresh(org)
        assert org.send_mode is None

    async def test_live_is_refused_outside_production(self, api, owner, db, org):
        """`CLAUDE.md` says the owner sets `live` only in production. This is that as code.

        A staging box restored from a copy of this database cannot be talked into sending.
        """
        response = api.patch(
            "/api/v1/admin/sending-config",
            json={"send_mode": "live", "confirm": "LIVE"},
            headers=owner,
        )

        assert response.status_code == 403
        await db.refresh(org)
        assert org.send_mode != "live"

    async def test_live_needs_the_typed_confirmation(self, api, owner, db, org, monkeypatch):
        """So that enabling real sending cannot be a mis-click or a replayed request."""
        from app.core import settings as settings_module

        monkeypatch.setattr(settings_module.config, "APP_ENV", "production")
        response = api.patch(
            "/api/v1/admin/sending-config", json={"send_mode": "live"}, headers=owner
        )

        assert response.status_code == 400
        await db.refresh(org)
        assert org.send_mode != "live"

    async def test_an_unknown_mode_is_refused(self, api, owner, db, org):
        for rubbish in ("enabled", "LIVE ", "true", "on"):
            response = api.patch(
                "/api/v1/admin/sending-config", json={"send_mode": rubbish}, headers=owner
            )
            assert response.status_code == 422, rubbish
        await db.refresh(org)
        assert org.send_mode in (None, "disabled", "allowlist")

    async def test_an_admin_cannot_change_it(self, api, db, org, keypair):
        """Owner only, not admin: helping review drafts must not hand somebody the switch."""
        private, _ = keypair
        subject = f"admin-{uuid.uuid4()}"
        db.add(Member(org_id=org.id, user_id=subject, email="admin@example.com", role="admin"))
        await db.commit()
        headers = {"Authorization": f"Bearer {token_for(private, subject=subject)}"}

        response = api.patch(
            "/api/v1/admin/sending-config", json={"send_mode": "disabled"}, headers=headers
        )

        assert response.status_code == 403

    async def test_a_member_cannot_change_it(self, api, member):
        response = api.patch(
            "/api/v1/admin/sending-config", json={"send_mode": "disabled"}, headers=member
        )

        assert response.status_code == 403

    async def test_it_needs_authentication(self, api):
        assert (
            api.patch("/api/v1/admin/sending-config", json={"send_mode": "live"}).status_code == 401
        )

    async def test_the_report_shows_where_the_mode_came_from(self, api, owner):
        """An operator needs to know whether the dashboard or the server is in charge."""
        api.patch("/api/v1/admin/sending-config", json={"send_mode": "allowlist"}, headers=owner)
        body = api.get("/api/v1/admin/sending-config", headers=owner).json()

        assert body["send_mode"] == "allowlist"
        assert body["source"] == "dashboard"
        assert "environment_default" in body
