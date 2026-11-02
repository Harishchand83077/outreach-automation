"""The three provider adapters, against responses built from their documentation.

**What these fixtures are, said plainly because it changes what the tests prove.**
`docs/ATS_FEEDS.md` recorded the three ATS feed shapes as **live-tested**: those endpoints are
public and were called. None of these three providers can be called without a paid account, and
hard rule 5 forbids buying one — so these payloads are written from each provider's published
documentation, and `docs/CONTACT_PROVIDERS.md` records which parts are verified and which are
assumed.

So what is proved here is the **reading rules**: a verified address and a guess are told apart,
an Apollo locked address becomes no address at all, a shape we cannot read raises rather than
returning nothing, and a refusal is reported as a refusal. Those are the properties that decide
whether somebody mails a pattern somebody inferred.

The one that matters most is `TestAnUnreadableShape`. The job-post intake taught it expensively:
an unknown source whose payload happened to have the right shape returned **zero postings**,
which is indistinguishable from an empty board. Here the equivalent would read as "nobody works
at this company", and somebody would believe it.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services.contacts import providers

pytestmark = pytest.mark.asyncio


async def _find(
    provider: providers.ContactProvider, **kwargs: object
) -> list[providers.FoundContact]:
    async with httpx.AsyncClient() as client:
        return await provider.find(
            domain=str(kwargs.get("domain", "acme.test")),
            role_hint=kwargs.get("role_hint"),  # type: ignore[arg-type]
            client=client,
        )


class TestHunter:
    @respx.mock
    async def test_a_verified_address_and_a_pattern_are_told_apart(self) -> None:
        """Hunter returns both from one call, and only one of them is safe to mail."""
        respx.get("https://api.hunter.io/v2/domain-search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "domain": "acme.test",
                        "emails": [
                            {
                                "value": "asha@acme.test",
                                "first_name": "Asha",
                                "last_name": "Rao",
                                "position": "Head of Engineering",
                                "confidence": 97,
                                "verification": {"status": "valid"},
                                "linkedin": "https://linkedin.test/in/asha",
                            },
                            {
                                "value": "v.shah@acme.test",
                                "first_name": "Vikram",
                                "last_name": "Shah",
                                "position": "Engineering Manager",
                                "confidence": 55,
                            },
                        ],
                    }
                },
            )
        )

        found = await _find(providers.HunterProvider("key"))

        assert [one.email_status for one in found] == ["verified", "guess"]
        assert found[0].full_name == "Asha Rao"
        assert found[0].confidence == 97

    @respx.mock
    async def test_the_key_travels_in_a_header_and_not_in_the_url(self) -> None:
        """A credential in a query string is a credential in an access log and a proxy log."""
        route = respx.get("https://api.hunter.io/v2/domain-search").mock(
            return_value=httpx.Response(200, json={"data": {"emails": []}})
        )

        await _find(providers.HunterProvider("secret-key"))

        request = route.calls[0].request
        assert "secret-key" not in str(request.url)
        assert request.headers["X-API-KEY"] == "secret-key"


class TestApollo:
    @respx.mock
    async def test_a_locked_address_becomes_no_address(self) -> None:
        """Apollo's search returns a placeholder until an enrichment credit is spent.

        Stored as `None`, because a placeholder in an email column is an address something will
        eventually try to mail -- and `email_not_unlocked@acme.test` would bounce and count
        against the mailbox's bounce rate (ADR-034).
        """
        respx.post(providers.ApolloProvider.ENDPOINT).mock(
            return_value=httpx.Response(
                200,
                json={
                    "people": [
                        {
                            "name": "Asha Rao",
                            "title": "Head of Engineering",
                            "email": "email_not_unlocked@domain.com",
                            "linkedin_url": "https://linkedin.test/in/asha",
                        }
                    ]
                },
            )
        )

        found = await _find(providers.ApolloProvider("key"))

        assert found[0].email is None
        assert found[0].email_status == "unknown"
        # Still useful: a name and a title are something a person can act on.
        assert found[0].full_name == "Asha Rao"

    @respx.mock
    async def test_a_refused_key_is_its_own_failure(self) -> None:
        """Recorded as `invalid_reason`, so the next press refuses before calling."""
        respx.post(providers.ApolloProvider.ENDPOINT).mock(return_value=httpx.Response(401))

        with pytest.raises(providers.ProviderUnauthorised):
            await _find(providers.ApolloProvider("key"))

    @respx.mock
    async def test_the_message_never_quotes_the_key(self) -> None:
        respx.post(providers.ApolloProvider.ENDPOINT).mock(return_value=httpx.Response(403))

        with pytest.raises(providers.ProviderError) as caught:
            await _find(providers.ApolloProvider("super-secret"))

        assert "super-secret" not in str(caught.value)


class TestSnov:
    @respx.mock
    async def test_it_exchanges_credentials_for_a_token_then_searches(self) -> None:
        token = respx.post(providers.SnovProvider.TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        search = respx.get(providers.SnovProvider.SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "emails": [
                        {
                            "email": "asha@acme.test",
                            "firstName": "Asha",
                            "lastName": "Rao",
                            "status": "valid",
                            "jobs": [{"position": "Head of Engineering"}],
                        }
                    ]
                },
            )
        )

        found = await _find(providers.SnovProvider("id", "secret"))

        assert token.called and search.called
        assert search.calls[0].request.headers["Authorization"] == "Bearer tok"
        assert found[0].title == "Head of Engineering"
        assert found[0].email_status == "verified"

    @respx.mock
    async def test_a_200_with_no_token_is_unauthorised_rather_than_unreadable(self) -> None:
        """Snov answers 200 with an error body for bad credentials.

        The distinction is not pedantic: `ProviderUnauthorised` writes `invalid_reason`, so the
        next press refuses instead of exchanging the same dead credentials again.
        """
        respx.post(providers.SnovProvider.TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"error": "invalid_client"})
        )

        with pytest.raises(providers.ProviderUnauthorised):
            await _find(providers.SnovProvider("id", "wrong"))


class TestAnUnreadableShape:
    """A provider that changed its response must say so, not return nobody.

    The lesson from the ATS intake, where an unknown source with a plausible payload returned
    zero postings and read as an empty board.
    """

    @respx.mock
    async def test_hunter_without_data_emails(self) -> None:
        respx.get("https://api.hunter.io/v2/domain-search").mock(
            return_value=httpx.Response(200, json={"data": {"domain": "acme.test"}})
        )

        with pytest.raises(providers.ProviderUnreadable):
            await _find(providers.HunterProvider("key"))

    @respx.mock
    async def test_apollo_without_people(self) -> None:
        respx.post(providers.ApolloProvider.ENDPOINT).mock(
            return_value=httpx.Response(200, json={"pagination": {"total_entries": 0}})
        )

        with pytest.raises(providers.ProviderUnreadable):
            await _find(providers.ApolloProvider("key"))

    @respx.mock
    async def test_a_body_that_is_not_json(self) -> None:
        respx.get("https://api.hunter.io/v2/domain-search").mock(
            return_value=httpx.Response(200, text="<html>maintenance</html>")
        )

        with pytest.raises(providers.ProviderUnreadable):
            await _find(providers.HunterProvider("key"))

    @respx.mock
    async def test_an_empty_list_is_not_an_error(self) -> None:
        """A company with no findable contacts is an ordinary answer, and must stay one."""
        respx.get("https://api.hunter.io/v2/domain-search").mock(
            return_value=httpx.Response(200, json={"data": {"emails": []}})
        )

        assert await _find(providers.HunterProvider("key")) == []


class TestTheAdapterFactory:
    def test_snov_needs_a_client_id_and_secret(self) -> None:
        with pytest.raises(providers.ProviderError):
            providers.build("snov", "just-a-string")

    def test_snov_reads_a_sealed_pair(self) -> None:
        built = providers.build("snov", json.dumps({"client_id": "a", "client_secret": "b"}))

        assert isinstance(built, providers.SnovProvider)

    def test_an_unknown_provider_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(providers.ProviderError):
            providers.build("clearbit", "key")


class TestTheMockProvider:
    async def test_it_returns_one_verified_address_and_one_guess(self) -> None:
        """Exactly the pair the review screen has to tell apart.

        It exists because the owner holds no provider key: without it the whole feature would be
        unreachable and untestable, and the first time it ran would be on a paid account.
        """
        found = await _find(providers.MockProvider())

        assert [one.email_status for one in found] == ["verified", "guess"]

    async def test_it_touches_no_network(self) -> None:
        # No respx mock and no routes: a real request would raise.
        found = await providers.MockProvider().find(
            domain="acme.test",
            role_hint=None,
            client=None,  # type: ignore[arg-type]
        )

        assert len(found) == 2
