"""Authentication and tenancy on the new API (§14, issues C1, C3, C4).

The prototype had **zero** authentication: anyone who found the URL could upload leads, approve
drafts and send email from the operator's mailbox (C1). So these tests are mostly about refusal,
and three of them are about refusing things that look like they should work:

- a token signed with a key we do not trust,
- a token whose header *claims* an algorithm we would be foolish to honour,
- a perfectly valid token belonging to somebody with no membership row.

The last one is the tenancy boundary. `org_id` is never taken from the request, so there is no
parameter to tamper with — the tests prove that by trying.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import deps
from app.api.app import allowed_origins, create_app
from app.api.ratelimit import RateLimit
from app.api.security import AuthError, TokenVerifier
from app.core.settings import Settings
from app.db.models import Member, Org

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
    """Hands back one key, so no test ever reaches the network."""

    def __init__(self, public_pem: str) -> None:
        self.public_pem = public_pem

    def signing_key(self, token: str):
        return serialization.load_pem_public_key(self.public_pem.encode())


def token_for(
    private_pem: str,
    *,
    subject: str = "user-1",
    email: str = "owner@example.com",
    issuer: str = ISSUER,
    expires_in: int = 3600,
    algorithm: str = "RS256",
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "email": email,
            "iss": issuer,
            "iat": now,
            "exp": now + expires_in,
            "aud": "authenticated",
        },
        private_pem,
        algorithm=algorithm,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    _, public = keypair
    settings = Settings(CORS_ALLOWED_ORIGINS="https://dash.example", APP_ENV="production")
    application = create_app(settings, limiter=RateLimit(limit=1000, window_seconds=60.0))

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[deps.get_session] = override_session
    deps.set_verifier(TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=FakeJwks(public)))

    with TestClient(application, raise_server_exceptions=False) as client:
        yield client

    deps.set_verifier(None)


@pytest_asyncio.fixture(loop_scope="session")
async def member(db, org):
    row = Member(org_id=org.id, user_id="user-1", email="owner@example.com", role="owner")
    db.add(row)
    await db.commit()
    return row


def _b64(data: dict) -> str:
    """One JWT segment, base64url without padding."""
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestNoTokenGetsNothing:
    async def test_an_authenticated_route_refuses_an_anonymous_caller(self, api):
        """C1, the headline issue: this used to return data to anyone."""
        response = api.get("/api/v1/me")
        assert response.status_code == 401

    async def test_the_refusal_tells_a_browser_to_re_authenticate(self, api):
        assert api.get("/api/v1/me").headers.get("WWW-Authenticate") == "Bearer"

    @pytest.mark.parametrize(
        "header",
        ["", "token abc", "Bearer", "Bearer    ", "Basic dXNlcjpwYXNz"],
    )
    async def test_a_malformed_authorization_header_is_refused(self, api, header):
        response = api.get("/api/v1/me", headers={"Authorization": header})
        assert response.status_code == 401


class TestForgedTokens:
    async def test_a_token_signed_by_someone_else_is_refused(self, api):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        response = api.get("/api/v1/me", headers=auth(token_for(pem)))
        assert response.status_code == 401

    async def test_an_hmac_token_signed_with_the_public_key_is_refused(self, api, keypair):
        """The classic confusion attack: without an algorithm allowlist this succeeds.

        Built by hand, because PyJWT refuses to *encode* such a token. That guard protects an
        honest caller, not us — an attacker is under no obligation to use PyJWT — so the forgery
        is assembled here to prove our own verifier rejects it.
        """
        _, public = keypair
        header = _b64({"alg": "HS256", "typ": "JWT"})
        payload = _b64({"sub": "user-1", "iss": ISSUER, "exp": int(time.time()) + 3600})
        signing_input = f"{header}.{payload}".encode()
        signature = (
            base64.urlsafe_b64encode(
                hmac.new(public.encode(), signing_input, hashlib.sha256).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        forged = f"{header}.{payload}.{signature}"

        assert api.get("/api/v1/me", headers=auth(forged)).status_code == 401

    async def test_the_alg_none_trick_is_refused(self, api):
        """An unsigned token that claims it needs no signature."""
        header = _b64({"alg": "none", "typ": "JWT"})
        payload = _b64({"sub": "user-1", "iss": ISSUER, "exp": int(time.time()) + 3600})
        assert api.get("/api/v1/me", headers=auth(f"{header}.{payload}.")).status_code == 401

    async def test_an_expired_token_is_refused(self, api, keypair):
        private, _ = keypair
        token = token_for(private, expires_in=-60)
        assert api.get("/api/v1/me", headers=auth(token)).status_code == 401

    async def test_a_token_from_another_project_is_refused(self, api, keypair):
        """Same signing key would not help; a different issuer is a different tenant."""
        private, _ = keypair
        token = token_for(private, issuer="https://someone-else.supabase.co/auth/v1")
        assert api.get("/api/v1/me", headers=auth(token)).status_code == 401

    async def test_the_reason_is_never_in_the_response(self, api, keypair):
        """'Expired' and 'bad signature' are both just 'no' from outside."""
        private, _ = keypair
        body = api.get("/api/v1/me", headers=auth(token_for(private, expires_in=-60))).json()
        assert "expired" not in str(body).lower()
        assert "signature" not in str(body).lower()


class TestMembership:
    async def test_a_valid_token_with_no_membership_is_refused(self, api, keypair, db):
        """Public sign-ups are off, so this means an authenticated stranger."""
        private, _ = keypair
        token = token_for(private, subject=f"nobody-{uuid.uuid4()}")
        response = api.get("/api/v1/me", headers=auth(token))
        assert response.status_code == 403

    async def test_a_member_gets_their_own_org_and_role(self, api, keypair, member, org):
        private, _ = keypair
        response = api.get("/api/v1/me", headers=auth(token_for(private)))

        assert response.status_code == 200
        body = response.json()
        assert body["org_id"] == str(org.id)
        assert body["role"] == "owner"

    async def test_the_org_comes_from_the_database_not_the_request(self, api, keypair, member, org):
        """There is no parameter to tamper with, and this proves it by trying."""
        private, _ = keypair
        other_org = uuid.uuid4()
        response = api.get(
            f"/api/v1/me?org_id={other_org}",
            headers={**auth(token_for(private)), "X-Org-Id": str(other_org)},
        )
        assert response.json()["org_id"] == str(org.id)

    async def test_another_orgs_member_sees_their_own_org(self, api, keypair, db, org, member):
        """Two members, two orgs, one API: each gets only their own."""
        private, _ = keypair
        second = Org(name="Someone Else Ltd")
        db.add(second)
        await db.flush()
        db.add(Member(org_id=second.id, user_id="user-2", email="x@example.com", role="owner"))
        await db.commit()

        first = api.get("/api/v1/me", headers=auth(token_for(private, subject="user-1")))
        other = api.get("/api/v1/me", headers=auth(token_for(private, subject="user-2")))

        assert first.json()["org_id"] != other.json()["org_id"]
        assert other.json()["org_id"] == str(second.id)


class TestPublicEndpoints:
    async def test_health_needs_no_token(self, api):
        """A load balancer cannot hold one."""
        response = api.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_ready_reports_the_database(self, api):
        response = api.get("/ready")
        assert response.status_code in (200, 503)
        assert "database" in response.json()["checks"]

    async def test_health_touches_nothing(self, api):
        """A health check that queries the database restarts the API when the database is ill."""
        import inspect

        from app.api.routers import health as module

        assert "execute" not in inspect.getsource(module.health)


class TestHardening:
    async def test_cors_is_never_a_wildcard(self):
        """C3: the prototype sent `*` with credentials, which browsers reject outright."""
        settings = Settings(CORS_ALLOWED_ORIGINS="https://dash.example,https://www.dash.example")
        origins = allowed_origins(settings)
        assert origins == ["https://dash.example", "https://www.dash.example"]
        assert "*" not in origins

    async def test_an_unlisted_origin_is_not_echoed_back(self, api, keypair, member):
        private, _ = keypair
        response = api.get(
            "/api/v1/me",
            headers={**auth(token_for(private)), "Origin": "https://evil.example"},
        )
        assert response.headers.get("access-control-allow-origin") != "https://evil.example"

    async def test_every_response_carries_a_request_id(self, api):
        assert api.get("/health").headers.get("X-Request-ID")

    async def test_an_error_body_has_the_uniform_shape(self, api):
        body = api.get("/api/v1/me").json()
        assert set(body) >= {"detail", "request_id"}

    async def test_the_interactive_docs_are_closed_in_production(self, api):
        """An unauthenticated schema of every endpoint is a map for whoever finds the URL."""
        assert api.get("/docs").status_code == 404


class TestRateLimiting:
    async def test_it_is_keyed_on_the_caller_not_the_address(self, session_factory, keypair):
        """C4: keyed on client IP, every user behind a proxy shared one bucket."""
        private, public = keypair
        settings = Settings(CORS_ALLOWED_ORIGINS="https://dash.example")
        application = create_app(settings, limiter=RateLimit(limit=2, window_seconds=60.0))

        async def override_session():
            async with session_factory() as session:
                yield session

        application.dependency_overrides[deps.get_session] = override_session
        deps.set_verifier(TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=FakeJwks(public)))

        try:
            with TestClient(application, raise_server_exceptions=False) as client:
                noisy = auth(token_for(private, subject="user-1"))
                quiet = auth(token_for(private, subject="user-2"))

                assert client.get("/api/v1/me", headers=noisy).status_code != 429
                assert client.get("/api/v1/me", headers=noisy).status_code != 429
                assert client.get("/api/v1/me", headers=noisy).status_code == 429
                # Same address, different token: untouched by the noisy caller.
                assert client.get("/api/v1/me", headers=quiet).status_code != 429
        finally:
            deps.set_verifier(None)

    async def test_a_limited_response_says_when_to_come_back(self, session_factory):
        settings = Settings(CORS_ALLOWED_ORIGINS="https://dash.example")
        application = create_app(settings, limiter=RateLimit(limit=1, window_seconds=60.0))
        with TestClient(application, raise_server_exceptions=False) as client:
            client.get("/health")
            response = client.get("/health")
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1

    async def test_idle_callers_are_swept_out_of_memory(self):
        """The prototype's limiter grew for as long as the process lived."""
        limiter = RateLimit(limit=5, window_seconds=1.0)
        for index in range(50):
            limiter.check(f"user-{index}", now=0.0)
        limiter.check("recent", now=100.0)
        assert len(limiter._hits) == 1


class TestTheVerifierItself:
    async def test_an_empty_token_is_refused_without_touching_the_key_set(self, keypair):
        _, public = keypair

        class Exploding(FakeJwks):
            def signing_key(self, token):
                raise AssertionError("must not fetch a key for an empty token")

        verifier = TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=Exploding(public))
        with pytest.raises(AuthError):
            verifier.verify("")

    async def test_hs256_is_not_an_accepted_algorithm(self):
        from app.api.security import ALLOWED_ALGORITHMS

        assert not any(alg.startswith("HS") for alg in ALLOWED_ALGORITHMS)

    async def test_a_token_without_a_subject_is_refused(self, keypair):
        private, public = keypair
        token = jwt.encode(
            {"iss": ISSUER, "exp": int(time.time()) + 60}, private, algorithm="RS256"
        )
        verifier = TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=FakeJwks(public))
        with pytest.raises(AuthError):
            verifier.verify(token)
