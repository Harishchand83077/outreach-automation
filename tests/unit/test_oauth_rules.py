"""The refusals, which is most of what an authorization server is (F14, ADR-058).

`app/domain/oauth.py` has no I/O, so every rule below is a test that needs no database. Each one
exists because accepting the thing it refuses hands somebody else's credential to somebody.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.domain import oauth

RESOURCE = "https://api.arkvane.in"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


VERIFIER = "a" * 64


class TestPkce:
    def test_the_right_verifier_passes(self) -> None:
        assert oauth.verify_challenge(VERIFIER, _challenge(VERIFIER))

    def test_a_wrong_verifier_fails(self) -> None:
        assert not oauth.verify_challenge("b" * 64, _challenge(VERIFIER))

    def test_plain_is_refused_even_when_it_would_match(self) -> None:
        """**A challenge equal to its verifier protects against nothing.** It exists in the spec
        for clients that could not hash, which is no client this server will ever see, and
        accepting it means a code stolen in transit is a code somebody can exchange."""
        assert not oauth.verify_challenge(VERIFIER, VERIFIER, method="plain")

    def test_a_short_verifier_is_refused(self) -> None:
        """RFC 7636 says 43 to 128 characters. A short one is brute-forceable, and a client
        sending one generated it wrong."""
        short = "abc"
        assert not oauth.verify_challenge(short, _challenge(short))

    def test_an_over_long_verifier_is_refused(self) -> None:
        long = "a" * 129
        assert not oauth.verify_challenge(long, _challenge(long))

    def test_an_empty_verifier_never_passes(self) -> None:
        assert not oauth.verify_challenge("", "")
        assert not oauth.verify_challenge("", _challenge(VERIFIER))


class TestRedirectUri:
    def test_an_exact_match_passes(self) -> None:
        assert oauth.check_redirect_uri("https://a.example/cb", ["https://a.example/cb"])

    def test_a_prefix_is_not_a_match(self) -> None:
        """**Every loosening of this rule is a published attack**, because the redirect is where
        the code is delivered. Prefix matching lets `https://a.example/cb.evil.test` through."""
        with pytest.raises(oauth.OAuthError):
            oauth.check_redirect_uri("https://a.example/cb/extra", ["https://a.example/cb"])

    def test_a_different_port_is_not_a_match(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.check_redirect_uri("http://localhost:9999/cb", ["http://localhost:1234/cb"])

    def test_an_added_query_is_not_a_match(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.check_redirect_uri("https://a.example/cb?x=1", ["https://a.example/cb"])

    def test_a_missing_redirect_is_refused(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.check_redirect_uri("", ["https://a.example/cb"])


class TestResource:
    """RFC 8707, and the reason this server exists at all."""

    def test_the_right_resource_passes(self) -> None:
        assert oauth.check_resource(RESOURCE, RESOURCE) == RESOURCE

    def test_a_trailing_slash_is_the_same_resource(self) -> None:
        assert oauth.check_resource(f"{RESOURCE}/", RESOURCE) == RESOURCE

    def test_another_api_is_refused(self) -> None:
        with pytest.raises(oauth.OAuthError, match="not this server"):
            oauth.check_resource("https://someone-else.example", RESOURCE)

    def test_a_missing_resource_is_refused_rather_than_defaulted(self) -> None:
        """**Defaulting it is the whole bug.** A client that never asked for an audience would
        get one anyway, and the token it received would be indistinguishable from one that did
        -- which is exactly Supabase's behaviour and exactly why it cannot be used here."""
        with pytest.raises(oauth.OAuthError, match="required"):
            oauth.check_resource(None, RESOURCE)

    def test_a_deployment_with_no_public_url_refuses_rather_than_guesses(self) -> None:
        with pytest.raises(oauth.OAuthError) as caught:
            oauth.check_resource(RESOURCE, "")
        assert caught.value.status == 500


class TestScopes:
    def test_known_scopes_pass(self) -> None:
        assert oauth.check_scopes("mcp:read mcp:propose", ("mcp:read", "mcp:propose")) == (
            "mcp:read",
            "mcp:propose",
        )

    def test_an_unknown_scope_is_refused_rather_than_trimmed(self) -> None:
        """A client that asked for something and silently got less behaves as though it has it,
        and the failure surfaces later as a tool that does not work for no visible reason."""
        with pytest.raises(oauth.OAuthError, match="mcp:send"):
            oauth.check_scopes("mcp:read mcp:send", ("mcp:read",))

    def test_no_scope_is_refused(self) -> None:
        with pytest.raises(oauth.OAuthError):
            oauth.check_scopes("", ("mcp:read",))


class TestTheShapeOfTheServer:
    def test_there_is_no_implicit_or_password_grant(self) -> None:
        """OAuth 2.1 removes both. `client_credentials` is absent for a reason of our own:
        every token here acts for a person and has to resolve to their membership."""
        assert oauth.GRANT_TYPES == ("authorization_code", "refresh_token")

    def test_the_only_challenge_method_is_s256(self) -> None:
        assert oauth.CHALLENGE_METHOD == "S256"

    def test_a_code_lives_about_a_minute(self) -> None:
        assert oauth.CODE_TTL.total_seconds() <= 120

    def test_an_access_token_is_short_because_a_refresh_token_exists(self) -> None:
        assert oauth.ACCESS_TTL < oauth.REFRESH_TTL

    def test_only_the_hash_of_a_code_is_ever_stored(self) -> None:
        code, digest = oauth.new_code()
        assert digest == oauth.hash_secret(code)
        assert code not in digest
