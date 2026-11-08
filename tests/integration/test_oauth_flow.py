"""The whole flow, and the things it must refuse (F14, ADR-058).

The unit tests cover the rules; these cover what needs rows: that a code is single-use, that a
refresh token rotates and that reusing one revokes the chain, that a token minted for another
resource is refused at `/mcp`, and that an OAuth token is an ordinary credential everywhere
downstream -- same catalogue, same scopes, same membership re-resolution.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters.
from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import ApiToken, Member, OAuthClient, OAuthCode, OAuthRefreshToken
from app.domain import oauth
from app.services.mcp import protocol

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner  # noqa: F401
from .test_mcp_endpoint import MCP, _body, _headers


@pytest.fixture
def issuer():
    """Kept so every test's signature reads as "this needs the public URL set", which the `api`
    fixture above now provides."""
    return RESOURCE


pytestmark = pytest.mark.asyncio(loop_scope="session")

RESOURCE = "https://api.example.test"
REDIRECT = "https://claude.example/callback"
VERIFIER = "v" * 64


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    """An app that knows its own public URL.

    **The shared fixture does not set `PUBLIC_API_URL`**, and the OAuth routes read it through
    `SettingsDep` -- correctly, since ADR-041 found that reaching for the module-level `config`
    makes an app built with its own `Settings` partially ignore them. So monkeypatching the
    singleton here would change nothing, which is how the first version of this file failed
    twenty-six tests while the server worked.
    """
    _, public = keypair
    application = create_app(
        Settings(
            _env_file=None,
            CORS_ALLOWED_ORIGINS="https://dash.example",
            PUBLIC_API_URL=RESOURCE,
            DASHBOARD_URL="https://dash.example",
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


async def _client(
    db, org, *, client_id: str = "claude", uris: list[str] | None = None
) -> OAuthClient:
    row = OAuthClient(
        org_id=org.id,
        client_id=client_id,
        name="Claude",
        redirect_uris=uris or [REDIRECT],
        created_by="tests",
    )
    db.add(row)
    await db.commit()
    return row


def _authorize(api, owner, **overrides):
    payload = {
        "client_id": "claude",
        "redirect_uri": REDIRECT,
        "scope": "mcp:read",
        "resource": RESOURCE,
        "code_challenge": _challenge(),
        "code_challenge_method": "S256",
    }
    payload.update(overrides)
    return api.post("/api/v1/oauth/authorize", json=payload, headers=owner)


def _token(api, **overrides):
    form = {
        "grant_type": "authorization_code",
        "client_id": "claude",
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "resource": RESOURCE,
    }
    form.update(overrides)
    return api.post("/oauth/token", data=form)


def _code_from(response) -> str:
    location = response.json()["redirect_to"]
    return location.split("code=")[1].split("&")[0]


class TestDiscovery:
    async def test_the_protected_resource_document_is_public(self, api, issuer):
        """RFC 9728, required by the 2026-07-28 revision. A client reads it *before* it has any
        credential, which is why it is unauthenticated."""
        response = api.get("/.well-known/oauth-protected-resource")
        assert response.status_code == 200
        body = response.json()
        assert body["resource"] == RESOURCE
        assert body["authorization_servers"] == [RESOURCE]

    async def test_the_server_advertises_s256_and_not_plain(self, api, issuer):
        """Advertising `plain` invites a client to use it, and a challenge equal to its
        verifier protects against nothing."""
        body = api.get("/.well-known/oauth-authorization-server").json()
        assert body["code_challenge_methods_supported"] == ["S256"]

    async def test_there_is_no_registration_endpoint(self, api, issuer):
        """DCR is deprecated in this revision, and would let anything that can reach the server
        ask somebody for their whole workspace."""
        assert (
            "registration_endpoint" not in api.get("/.well-known/oauth-authorization-server").json()
        )

    async def test_no_implicit_grant_is_offered(self, api, issuer):
        body = api.get("/.well-known/oauth-authorization-server").json()
        assert body["response_types_supported"] == ["code"]
        assert "implicit" not in body["grant_types_supported"]


class TestTheHappyPath:
    async def test_a_code_becomes_a_token(self, api, db, org, owner, issuer):
        await _client(db, org)
        granted = _authorize(api, owner)
        assert granted.status_code == 200

        issued = _token(api, code=_code_from(granted))
        assert issued.status_code == 200
        body = issued.json()
        assert body["token_type"] == "Bearer"
        assert body["scope"] == "mcp:read"
        assert body["access_token"].startswith("oa_")
        assert body["refresh_token"].startswith("oar_")
        # The spec requires it, and it matters: a cached token would be presented after it was
        # revoked.
        assert issued.headers["Cache-Control"] == "no-store"

    async def test_the_token_works_at_the_mcp_endpoint(self, api, db, org, owner, issuer):
        """**The point of the whole slice.** What comes out is an ordinary credential: same
        catalogue, same scope gate, same membership re-resolution."""
        await _client(db, org)
        token = _token(api, code=_code_from(_authorize(api, owner))).json()["access_token"]

        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
        )
        assert response.status_code == 200
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert "list_campaigns" in names
        # Read-only scope, so nothing that writes is offered.
        assert "propose_draft" not in names

    async def test_a_refresh_token_rotates(self, api, db, org, owner, issuer):
        await _client(db, org)
        first = _token(api, code=_code_from(_authorize(api, owner))).json()

        second = api.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "claude",
                "refresh_token": first["refresh_token"],
            },
        )
        assert second.status_code == 200
        assert second.json()["refresh_token"] != first["refresh_token"]


class TestWhatItRefuses:
    async def test_a_code_cannot_be_used_twice(self, api, db, org, owner, issuer):
        """Replaying one is how a code read out of a log becomes a session."""
        await _client(db, org)
        code = _code_from(_authorize(api, owner))
        assert _token(api, code=code).status_code == 200

        again = _token(api, code=code)
        assert again.status_code == 400
        assert again.json()["error"] == "invalid_grant"

    async def test_a_wrong_verifier_is_refused(self, api, db, org, owner, issuer):
        await _client(db, org)
        code = _code_from(_authorize(api, owner))
        assert _token(api, code=code, code_verifier="w" * 64).status_code == 400

    async def test_a_missing_verifier_is_refused(self, api, db, org, owner, issuer):
        """PKCE is not optional in OAuth 2.1, and this is the check that makes a stolen code
        useless to whoever stole it."""
        await _client(db, org)
        code = _code_from(_authorize(api, owner))
        assert _token(api, code=code, code_verifier="").status_code == 400

    async def test_a_different_redirect_at_exchange_is_refused(self, api, db, org, owner, issuer):
        await _client(db, org, uris=[REDIRECT, "https://claude.example/other"])
        code = _code_from(_authorize(api, owner))
        refused = _token(api, code=code, redirect_uri="https://claude.example/other")
        assert refused.status_code == 400

    async def test_another_client_cannot_exchange_the_code(self, api, db, org, owner, issuer):
        await _client(db, org)
        await _client(db, org, client_id="someone-else", uris=[REDIRECT])
        code = _code_from(_authorize(api, owner))
        assert _token(api, code=code, client_id="someone-else").status_code == 400

    async def test_an_expired_code_is_refused(self, api, db, org, owner, issuer):
        await _client(db, org)
        code = _code_from(_authorize(api, owner))
        row = await db.scalar(
            select(OAuthCode).where(OAuthCode.code_hash == oauth.hash_secret(code))
        )
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

        assert _token(api, code=code).status_code == 400

    async def test_an_unregistered_redirect_is_refused_at_authorize(
        self, api, db, org, owner, issuer
    ):
        await _client(db, org)
        refused = _authorize(api, owner, redirect_uri="https://evil.test/cb")
        assert refused.status_code == 400
        assert refused.json()["error"] == "invalid_request"

    async def test_a_wrong_resource_is_refused_at_authorize(self, api, db, org, owner, issuer):
        await _client(db, org)
        refused = _authorize(api, owner, resource="https://someone-else.example")
        assert refused.json()["error"] == "invalid_target"

    async def test_plain_is_refused_at_authorize(self, api, db, org, owner, issuer):
        await _client(db, org)
        refused = _authorize(api, owner, code_challenge_method="plain")
        assert refused.status_code == 400

    async def test_an_unknown_client_is_refused(self, api, db, org, owner, issuer):
        refused = _authorize(api, owner, client_id="nobody")
        assert refused.json()["error"] == "invalid_client"

    async def test_a_disabled_client_is_refused_exactly_as_an_unknown_one(
        self, api, db, org, owner, issuer
    ):
        row = await _client(db, org)
        row.disabled_at = datetime.now(UTC)
        await db.commit()
        assert _authorize(api, owner).json()["error"] == "invalid_client"

    async def test_authorize_needs_a_signed_in_person(self, api, db, org, issuer):
        """The thing being decided is "does this person grant this", so there is no version of
        this endpoint that works without knowing who they are."""
        await _client(db, org)
        response = api.post(
            "/api/v1/oauth/authorize",
            json={
                "client_id": "claude",
                "redirect_uri": REDIRECT,
                "code_challenge": _challenge(),
            },
        )
        assert response.status_code in {401, 403}

    async def test_an_unknown_grant_type_is_refused(self, api, issuer):
        refused = api.post("/oauth/token", data={"grant_type": "password"})
        assert refused.json()["error"] == "unsupported_grant_type"


class TestRefreshReuse:
    async def test_reusing_a_rotated_refresh_token_revokes_the_chain(
        self, api, db, org, owner, issuer
    ):
        """**Somebody presenting an already-exchanged token is a stale client or a stolen
        copy**, and the safe answer to both is the same. Letting the real client carry on means
        a stolen token keeps working for ninety days and nothing ever notices."""
        await _client(db, org)
        first = _token(api, code=_code_from(_authorize(api, owner))).json()
        second = api.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "claude",
                "refresh_token": first["refresh_token"],
            },
        ).json()

        reused = api.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "claude",
                "refresh_token": first["refresh_token"],
            },
        )
        assert reused.status_code == 400
        assert "revoked" in reused.json()["error_description"]

        # The newest access token is dead too, not just the refresh token.
        response = api.post(
            MCP,
            json=_body("tools/list"),
            headers=_headers(second["access_token"], method="tools/list"),
        )
        assert response.status_code == 401

    async def test_rotation_kills_the_previous_access_token(self, api, db, org, owner, issuer):
        """Or an hour of the old one stays live beside its replacement for no reason."""
        await _client(db, org)
        first = _token(api, code=_code_from(_authorize(api, owner))).json()
        api.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "claude",
                "refresh_token": first["refresh_token"],
            },
        )
        stale = api.post(
            MCP,
            json=_body("tools/list"),
            headers=_headers(first["access_token"], method="tools/list"),
        )
        assert stale.status_code == 401


class TestTheAudienceCheck:
    async def test_a_token_minted_for_another_resource_is_refused(
        self, api, db, org, owner, issuer
    ):
        """**The reason this deployment runs its own authorization server.** Supabase issues a
        token whose audience is the constant `"authenticated"` whatever `resource` was asked
        for, so a token obtained for any other service would be accepted here."""
        await _client(db, org)
        token = _token(api, code=_code_from(_authorize(api, owner))).json()["access_token"]

        row = await db.scalar(
            select(ApiToken).where(
                ApiToken.token_hash
                == __import__("app.services.api_tokens", fromlist=["hash_token"]).hash_token(token)
            )
        )
        assert row is not None
        row.resource = "https://someone-else.example"
        await db.commit()

        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
        )
        assert response.status_code == 401

    async def test_a_hand_minted_token_still_works(self, api, db, org, issuer):
        """It has no audience because it is only ever presented here, and it is the credential
        this endpoint has always taken. Breaking it would be the migration's real risk."""
        from app.services import api_tokens

        subject = f"hand-{uuid.uuid4()}"
        db.add(Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin"))
        minted = await api_tokens.create_token(db, org_id=org.id, user_id=subject, name="By hand")
        await db.commit()

        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(minted.secret, method="tools/list")
        )
        assert response.status_code == 200

    async def test_the_401_says_where_to_get_a_credential(self, api, issuer):
        """RFC 9728: without `resource_metadata` the only way in is a pasted token, which is
        what this slice exists to stop being the answer."""
        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers("oa_nope", method="tools/list")
        )
        assert response.status_code == 401
        assert "resource_metadata" in response.headers.get("WWW-Authenticate", "")


class TestTheMembershipStillDecides:
    async def test_removing_the_member_kills_the_token(self, api, db, org, owner, issuer):
        """ADR-028's rule, unchanged: the token says who is asking and the membership says what
        they may reach. An ex-colleague's connector must stop working."""
        await _client(db, org)
        token = _token(api, code=_code_from(_authorize(api, owner))).json()["access_token"]
        assert (
            api.post(
                MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
            ).status_code
            == 200
        )

        row = await db.scalar(
            select(ApiToken)
            .where(ApiToken.client_id == "claude")
            .order_by(ApiToken.created_at.desc())
        )
        assert row is not None
        membership = await db.scalar(select(Member).where(Member.user_id == row.user_id))
        await db.delete(membership)
        await db.commit()

        assert (
            api.post(
                MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
            ).status_code
            == 401
        )

    async def test_a_refresh_after_removal_issues_nothing(self, api, db, org, owner, issuer):
        await _client(db, org)
        first = _token(api, code=_code_from(_authorize(api, owner))).json()

        newest = select(OAuthRefreshToken).order_by(OAuthRefreshToken.created_at.desc())
        row = await db.scalar(newest)
        assert row is not None
        membership = await db.scalar(select(Member).where(Member.user_id == row.user_id))
        await db.delete(membership)
        await db.commit()

        refused = api.post(
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "claude",
                "refresh_token": first["refresh_token"],
            },
        )
        assert refused.status_code == 400


class TestConsent:
    async def test_the_screen_is_told_the_clients_registered_name(
        self, api, db, org, owner, issuer
    ):
        """**Never a name the request supplied.** It is the one string a person uses to decide,
        and a name chosen by the caller is a name an attacker chooses."""
        await _client(db, org)
        body = api.get(
            "/api/v1/oauth/consent",
            params={
                "client_id": "claude",
                "scope": "mcp:read",
                "redirect_uri": REDIRECT,
                "resource": RESOURCE,
            },
            headers=owner,
        ).json()
        assert body["ok"] is True
        assert body["client"]["name"] == "Claude"
        assert body["scopes"][0]["describes"]

    async def test_a_request_that_could_never_succeed_is_refused_on_the_screen(
        self, api, db, org, owner, issuer
    ):
        """Rather than after somebody presses Allow."""
        await _client(db, org)
        body = api.get(
            "/api/v1/oauth/consent",
            params={
                "client_id": "claude",
                "scope": "mcp:read",
                "redirect_uri": "https://evil.test/cb",
                "resource": RESOURCE,
            },
            headers=owner,
        ).json()
        assert body["ok"] is False

    async def test_the_extension_scope_cannot_be_obtained_here(self, api, db, org, owner, issuer):
        """ADR-039: a different door. A scope that opens it has no business being grantable to
        a connector."""
        await _client(db, org)
        refused = _authorize(api, owner, scope="ext:suggest")
        assert refused.json()["error"] == "invalid_scope"


class TestClientRegistration:
    async def test_only_an_owner_may_add_a_client(self, api, member, issuer):
        refused = api.post(
            "/api/v1/oauth/clients",
            json={"client_id": "x", "name": "X", "redirect_uris": ["https://x.test/cb"]},
            headers=member,
        )
        assert refused.status_code == 403

    async def test_an_http_redirect_is_refused(self, api, owner, issuer):
        """A code delivered over http is a code read in transit."""
        refused = api.post(
            "/api/v1/oauth/clients",
            json={"client_id": "x", "name": "X", "redirect_uris": ["http://x.test/cb"]},
            headers=owner,
        )
        assert refused.status_code == 422

    async def test_localhost_is_exempt_because_it_does_not_leave_the_machine(
        self, api, owner, issuer
    ):
        created = api.post(
            "/api/v1/oauth/clients",
            json={
                "client_id": f"local-{uuid.uuid4().hex[:8]}",
                "name": "Local",
                "redirect_uris": ["http://localhost:4000/cb"],
            },
            headers=owner,
        )
        assert created.status_code == 201

    async def test_a_client_with_no_redirect_is_refused(self, api, owner, issuer):
        refused = api.post(
            "/api/v1/oauth/clients",
            json={"client_id": "y", "name": "Y", "redirect_uris": []},
            headers=owner,
        )
        assert refused.status_code == 422


def test_the_protocol_scopes_are_what_is_grantable() -> None:
    """The consent screen must not be able to offer a scope the endpoint does not honour."""
    from app.api.routers.oauth import GRANTABLE

    assert set(GRANTABLE) == set(protocol.SCOPES)
