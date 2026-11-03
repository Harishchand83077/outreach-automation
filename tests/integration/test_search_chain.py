"""The search chain against a real database: its keys, its cache, and who pays (F17, ADR-046).

The unit tests cover ordering. These cover the parts that need rows: that the chain is built from
**this** workspace's keys and nobody else's, that a cached answer calls no provider at all --
the slice's second acceptance criterion -- and that the research handler now actually passes a
chain, which until this slice it did not.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
from app.db.models import Org, ProviderCredential, SearchCache, SearchUsage
from app.db.repo import OrgContext
from app.services.contacts import keys
from app.services.research import chain, search_cache
from app.services.research.search import SearchBudget, SearchHit

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": TEST_KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    """This slice's own app, for the same reason F9's has one: sealing a key needs a
    `CREDENTIAL_KEY`, and the route reads the settings its app was built with."""
    _, public = keypair
    application = create_app(
        _settings(CORS_ALLOWED_ORIGINS="https://dash.example"),
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


class Recorder:
    def __init__(self, name: str, *hits: SearchHit) -> None:
        self.name = name
        self._hits = list(hits)
        self.calls: list[str] = []

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        self.calls.append(query)
        return list(self._hits)


def hit(url: str = "https://news.example/acme") -> SearchHit:
    return SearchHit(title="Acme raised", url=url, snippet="Acme raised a round")


class TestBuildingTheChainFromTheWorkspacesOwnKeys:
    async def test_members_come_out_in_the_tuple_order(self, db, org) -> None:
        """`SEARCH_PROVIDERS` is the order, so a workspace that saved its keys in any sequence
        gets the same chain. A chain whose order depended on when somebody pasted a key would
        be a chain nobody could predict the cost of."""
        ctx = OrgContext(org_id=org.id)
        await keys.store(
            db, ctx, provider="serper", secret="s", settings=_settings(), allowed=("serper",)
        )
        await keys.store(
            db, ctx, provider="tavily", secret="t", settings=_settings(), allowed=("tavily",)
        )
        await db.commit()

        import httpx

        async with httpx.AsyncClient() as client:
            members = await chain.build_chain(db, ctx, client=client, settings=_settings())

        assert [member.usage_name for member in members] == ["tavily", "serper"]

    async def test_another_workspaces_key_is_never_in_this_chain(self, db, org) -> None:
        """ADR-004 asserted rather than assumed. A key is money, so a chain that reached across
        a workspace boundary would be spending somebody else's."""
        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()
        await keys.store(
            db,
            OrgContext(org_id=other.id),
            provider="brave",
            secret="theirs",
            settings=_settings(),
            allowed=("brave",),
        )
        await db.commit()

        import httpx

        async with httpx.AsyncClient() as client:
            members = await chain.build_chain(
                db, OrgContext(org_id=org.id), client=client, settings=_settings()
            )

        assert [member.usage_name for member in members] == []

    async def test_a_key_the_provider_refused_is_left_out_rather_than_tried(self, db, org) -> None:
        """The same refusal `lookup.run` makes before a contact lookup: calling with a key we
        recorded as dead spends a request to be told what we wrote down last time."""
        ctx = OrgContext(org_id=org.id)
        row = await keys.store(
            db, ctx, provider="brave", secret="b", settings=_settings(), allowed=("brave",)
        )
        await keys.mark_invalid(db, credential_id=row.id, reason="brave refused the key")
        await db.commit()

        import httpx

        async with httpx.AsyncClient() as client:
            members = await chain.build_chain(db, ctx, client=client, settings=_settings())

        assert members == []

    async def test_the_deployment_entry_goes_last_and_is_named_as_shared(self, db, org) -> None:
        """It is the only member that is not somebody's own key, and every workspace using it
        draws on one free tier -- so it must not be able to shadow a key the workspace paid
        for, and the ledger has to be able to say which account a credit came out of."""
        ctx = OrgContext(org_id=org.id)
        await keys.store(
            db, ctx, provider="brave", secret="b", settings=_settings(), allowed=("brave",)
        )
        await db.commit()

        import httpx

        async with httpx.AsyncClient() as client:
            members = await chain.build_chain(
                db, ctx, client=client, settings=_settings(TAVILY_API_KEY="tvly-deployment")
            )

        assert [member.usage_name for member in members] == ["brave", chain.DEPLOYMENT_MEMBER]
        assert members[-1].credential_id is None, "there is no row a route could mark invalid"

    async def test_no_key_anywhere_is_a_chain_of_none(self, db, org) -> None:
        """A supported configuration, not an error: research degrades to the company's own
        pages, which is exactly where it was before F17."""
        import httpx

        async with httpx.AsyncClient() as client:
            members = await chain.build_chain(
                db, OrgContext(org_id=org.id), client=client, settings=_settings()
            )

        assert members == []


class TestACacheHitCallsNobody:
    """The slice's second acceptance criterion."""

    async def test_a_stored_answer_is_served_without_a_provider(self, db, org) -> None:
        await search_cache.store(
            db,
            org_id=org.id,
            query="Acme Analytics company news",
            provider="tavily",
            hits=[hit()],
            now=NOW,
        )
        await db.commit()

        found = await search_cache.lookup(
            db, org_id=org.id, query="acme analytics  company news", now=NOW
        )

        assert found is not None, "case and spacing are one question, not two credits"
        assert [h.url for h in found.hits] == ["https://news.example/acme"]
        assert found.provider == "tavily"

    async def test_an_expired_answer_is_a_miss(self, db, org) -> None:
        await search_cache.store(
            db, org_id=org.id, query="Stale Co news", provider="tavily", hits=[hit()], now=NOW
        )
        await db.commit()

        later = NOW + timedelta(days=30)
        assert (
            await search_cache.lookup(db, org_id=org.id, query="Stale Co news", now=later) is None
        )

    async def test_another_workspace_cannot_read_this_answer(self, db, org) -> None:
        """It was paid for out of one workspace's credits, which is ADR-038's line: public facts
        about a company are shared, a provider's billable answer is not."""
        other = Org(name="Not Us")
        db.add(other)
        await db.commit()
        await search_cache.store(
            db, org_id=org.id, query="Acme news", provider="tavily", hits=[hit()], now=NOW
        )
        await db.commit()

        assert await search_cache.lookup(db, org_id=other.id, query="Acme news", now=NOW) is None

    async def test_a_second_answer_replaces_rather_than_colliding(self, db, org) -> None:
        """The unique key is the question. A cache that errors on its second write is a cache
        that silently stops being one."""
        for provider in ("tavily", "brave"):
            await search_cache.store(
                db,
                org_id=org.id,
                query="Acme news",
                provider=provider,
                hits=[hit()],
                now=NOW,
            )
        await db.commit()

        rows = (
            await db.scalars(
                select(SearchCache).where(
                    SearchCache.org_id == org.id, SearchCache.query_norm == "acme news"
                )
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].provider == "brave"

    async def test_a_confirmed_empty_is_cached_and_a_total_failure_is_not(self, db, org) -> None:
        """The distinction the whole cache rests on. A provider that answered and found nothing
        has told us something true; a chain that reached nobody has not, and storing that would
        stop this workspace searching for a week over one bad afternoon."""
        await search_cache.store(
            db, org_id=org.id, query="Quiet Co news", provider="tavily", hits=[], now=NOW
        )
        await db.commit()

        found = await search_cache.lookup(db, org_id=org.id, query="Quiet Co news", now=NOW)

        assert found is not None
        assert found.found_nothing

    async def test_expired_answers_are_purged_by_the_sweep(self, db, org) -> None:
        await search_cache.store(
            db, org_id=org.id, query="Old news", provider="tavily", hits=[hit()], now=NOW
        )
        await db.commit()

        purged = await search_cache.purge_expired(db, now=NOW + timedelta(days=30))
        await db.commit()

        assert purged >= 1
        assert (
            await db.scalar(
                select(SearchCache).where(
                    SearchCache.org_id == org.id, SearchCache.query_norm == "old news"
                )
            )
            is None
        )


class TestWhoPays:
    async def test_only_the_member_that_answered_is_billed(self, db, org) -> None:
        """The ledger and the short circuit have to agree: a provider that was never called
        must not appear in `search_usage`, or the credits-left readout overstates the cost of
        every search."""
        first = Recorder("tavily", hit())
        second = Recorder("brave", hit("https://brave.example"))
        budget = SearchBudget(db, org.id, monthly=10)

        answer = await chain.SearchChain(
            [chain.ChainMember(provider=first), chain.ChainMember(provider=second)],
            session=db,
            org_id=org.id,
            budget=budget,
        ).run("Acme company news", domain="acme.example", now=NOW)
        await db.commit()

        assert answer.provider == "tavily"
        rows = (await db.scalars(select(SearchUsage).where(SearchUsage.org_id == org.id))).all()
        assert [row.provider for row in rows] == ["tavily"]

    async def test_a_spent_ceiling_on_one_provider_does_not_refuse_another(self, db, org) -> None:
        """Per provider rather than pooled. Each member is a separate account, so a shared
        total would refuse a workspace's second key for credits its first key spent."""
        budget = SearchBudget(db, org.id, monthly=1)
        await budget.record(provider="tavily", credits=1)
        await db.commit()

        assert not await budget.can_spend(1, provider="tavily", now=NOW)
        assert await budget.can_spend(1, provider="brave", now=NOW)

    async def test_the_pooled_total_is_still_what_the_readout_shows(self, db, org) -> None:
        budget = SearchBudget(db, org.id, monthly=10)
        await budget.record(provider="tavily", credits=1)
        await budget.record(provider="brave", credits=1)
        await db.commit()

        assert await budget.used_this_month(now=NOW) == 2
        assert await budget.used_this_month(provider="brave", now=NOW) == 1


class TestTheTwoFamiliesShareATableAndNothingElse:
    async def test_a_search_key_cannot_be_saved_through_the_contact_route(self, api, owner) -> None:
        """Both names are legal in the column, so the database cannot refuse this. A Tavily key
        stored as a contact provider would be handed to a contact adapter, which would post a
        search credential to somebody else's endpoint."""
        response = api.put(
            "/api/v1/provider-keys/tavily", json={"api_key": "tvly-x"}, headers=owner
        )

        assert response.status_code == 422, response.text
        assert "tavily" in response.json()["detail"]

    async def test_a_contact_key_cannot_be_saved_through_the_search_route(self, api, owner) -> None:
        response = api.put(
            "/api/v1/search-keys/hunter", json={"api_key": "hunter-x"}, headers=owner
        )

        assert response.status_code == 422, response.text

    async def test_each_screen_lists_only_its_own_family(self, api, owner) -> None:
        contact = api.get("/api/v1/provider-keys", headers=owner)
        searchable = api.get("/api/v1/search-keys", headers=owner)

        assert contact.status_code == 200 and searchable.status_code == 200
        assert {row["provider"] for row in contact.json()["providers"]} == {
            "apollo",
            "hunter",
            "snov",
            "mock",
        }
        assert {row["provider"] for row in searchable.json()["providers"]} == {
            "tavily",
            "brave",
            "serper",
        }


class TestTheSearchKeyRoutes:
    async def test_a_saved_key_is_never_returned_and_is_stored_sealed(self, api, owner, db) -> None:
        saved = api.put(
            "/api/v1/search-keys/brave",
            json={"api_key": "brave-secret", "label": "mine"},
            headers=owner,
        )
        assert saved.status_code == 200, saved.text

        listed = api.get("/api/v1/search-keys", headers=owner)
        assert "brave-secret" not in listed.text

        row = await db.scalar(
            select(ProviderCredential).where(
                ProviderCredential.provider == "brave",
                ProviderCredential.revoked_at.is_(None),
            )
        )
        assert row is not None and b"brave-secret" not in row.secret

    async def test_a_member_cannot_save_a_key(self, api, member) -> None:
        """`admin`, like the send allowlist: a key spends the workspace's money, and a member
        reviews drafts rather than deciding what the workspace pays for."""
        response = api.put("/api/v1/search-keys/brave", json={"api_key": "x"}, headers=member)

        assert response.status_code == 403, response.text

    async def test_the_order_shown_is_the_order_that_would_run(self, api, owner) -> None:
        """The question the screen exists to answer. A list of configured keys does not say
        which one pays for the next search."""
        api.put("/api/v1/search-keys/serper", json={"api_key": "s"}, headers=owner)
        api.put("/api/v1/search-keys/tavily", json={"api_key": "t"}, headers=owner)

        body = api.get("/api/v1/search-keys", headers=owner).json()

        assert [row["provider"] for row in body["order"]] == ["tavily", "serper"]
        assert all(row["shared"] is False for row in body["order"])

    async def test_revoking_removes_it_from_the_order(self, api, owner) -> None:
        api.put("/api/v1/search-keys/brave", json={"api_key": "b"}, headers=owner)
        api.delete("/api/v1/search-keys/brave", headers=owner)

        body = api.get("/api/v1/search-keys", headers=owner).json()

        assert "brave" not in [row["provider"] for row in body["order"]]

    async def test_verifying_without_a_key_says_so_rather_than_erroring(self, api, owner) -> None:
        response = api.post("/api/v1/search-keys/serper/verify", headers=owner)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert "no serper key" in body["reason"]

    async def test_no_get_on_this_router_talks_to_a_provider(self, api, owner) -> None:
        """`GET /mailboxes` is non-networked because refreshing it would mint OAuth tokens.
        Here a networked `GET` would spend one of the user's credits on every F5. respx with no
        route registered turns any outbound request into a failure, so a listing that reached a
        provider could not come back 200."""
        import respx

        api.put("/api/v1/search-keys/tavily", json={"api_key": "t"}, headers=owner)

        with respx.mock(assert_all_called=False):
            response = api.get("/api/v1/search-keys", headers=owner)

        assert response.status_code == 200, response.text
