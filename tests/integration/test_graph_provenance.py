"""An edge without a source is refused by the database (F12, ADR-051).

**The first test is the slice.** It writes raw SQL rather than going through the service, because
a rule enforced only by the service is a rule the next writer bypasses -- and the next writer is
always somebody in a hurry with a reasonable-sounding exception. These rows feed a brief that
somebody repeats to a stranger; a claim whose source nobody can produce is the thing this system
exists not to say.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Edge, Entity, JobPost
from app.db.repo import OrgContext
from app.services.graph import store as graph
from app.services.jobposts import intake as jobposts
from app.services.jobposts.feeds import Posting
from app.services.signals import intake, watchlist

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
LEVER_URL = "https://jobs.lever.co/acme/abc-123"


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
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


class TestTheDatabaseRefusesAnUnsourcedClaim:
    """Raw SQL on purpose: a rule only the service enforces is a rule the next writer bypasses."""

    async def test_an_edge_with_no_source_and_no_message_is_refused(self, db, org) -> None:
        company = await graph.entity(
            db, OrgContext(org_id=org.id), kind="company", name="Acme", domain="acme.example"
        )
        await db.commit()

        with pytest.raises(IntegrityError) as caught:
            await db.execute(
                text(
                    "INSERT INTO app.edges (org_id, subject_id, predicate, object_text) "
                    "VALUES (:org, :subject, 'uses', 'Postgres')"
                ),
                {"org": org.id, "subject": company.id},
            )
        await db.rollback()

        assert "ck_edges_provenance" in str(caught.value)

    async def test_a_url_alone_is_enough(self, db, org) -> None:
        company = await graph.entity(
            db, OrgContext(org_id=org.id), kind="company", name="Acme", domain="acme.example"
        )
        await db.commit()

        await db.execute(
            text(
                "INSERT INTO app.edges (org_id, subject_id, predicate, object_text, source_url) "
                "VALUES (:org, :subject, 'uses', 'Postgres', 'https://acme.example/stack')"
            ),
            {"org": org.id, "subject": company.id},
        )
        await db.commit()

        assert await db.scalar(select(Edge).where(Edge.org_id == org.id)) is not None

    async def test_an_edge_that_is_both_a_link_and_a_value_is_refused(self, db, org) -> None:
        """One row, one claim. A row that pointed at a node *and* stated a value would be two
        claims wearing one id, and a brief citing it could not say which it meant."""
        ctx = OrgContext(org_id=org.id)
        company = await graph.entity(db, ctx, kind="company", name="Acme", domain="acme.example")
        role = await graph.entity(db, ctx, kind="role", name="Backend Engineer")
        await db.commit()

        with pytest.raises(IntegrityError) as caught:
            await db.execute(
                text(
                    "INSERT INTO app.edges "
                    "(org_id, subject_id, predicate, object_id, object_text, source_url) "
                    "VALUES (:org, :s, 'hiring_for', :o, 'also text', 'https://x.example')"
                ),
                {"org": org.id, "s": company.id, "o": role.id},
            )
        await db.rollback()

        assert "ck_edges_object" in str(caught.value)

    async def test_an_edge_with_neither_is_refused(self, db, org) -> None:
        company = await graph.entity(
            db, OrgContext(org_id=org.id), kind="company", name="Acme", domain="acme.example"
        )
        await db.commit()

        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO app.edges (org_id, subject_id, predicate, source_url) "
                    "VALUES (:org, :s, 'hiring_for', 'https://x.example')"
                ),
                {"org": org.id, "s": company.id},
            )
        await db.rollback()


class TestTheServiceRefusesFirst:
    """The constraint is the last line of defence, not the first. A caller that forgets should
    be told which claim it was, not handed a constraint violation three frames away from a
    transaction that was storing something else."""

    async def test_a_claim_with_no_provenance_cannot_be_constructed(self, db, org) -> None:
        company = await graph.entity(
            db, OrgContext(org_id=org.id), kind="company", name="Acme", domain="acme.example"
        )

        with pytest.raises(graph.ProvenanceRequired) as caught:
            graph.Claim(subject=company, predicate="uses", object_text="Postgres")

        assert "source url or a message id" in str(caught.value)

    async def test_a_claim_with_both_an_object_and_a_text_cannot_be_constructed(
        self, db, org
    ) -> None:
        ctx = OrgContext(org_id=org.id)
        company = await graph.entity(db, ctx, kind="company", name="Acme", domain="acme.example")
        role = await graph.entity(db, ctx, kind="role", name="Backend Engineer")

        with pytest.raises(graph.ProvenanceRequired):
            graph.Claim(
                subject=company,
                predicate="hiring_for",
                object_entity=role,
                object_text="also text",
                source_url="https://x.example",
            )


class TestEntitiesAreOneThing:
    async def test_two_spellings_of_one_company_are_one_node(self, db, org) -> None:
        ctx = OrgContext(org_id=org.id)

        first = await graph.entity(db, ctx, kind="company", name="Acme, Inc.")
        second = await graph.entity(db, ctx, kind="company", name="ACME Inc")
        await db.commit()

        assert first.id == second.id

    async def test_a_company_is_keyed_on_its_domain_when_it_has_one(self, db, org) -> None:
        """Four tables agreeing on what a company is matters more than any of them being clever
        about names: `domain_research`, the watchlist, the research cache and this."""
        ctx = OrgContext(org_id=org.id)

        typed = await graph.entity(
            db, ctx, kind="company", name="Acme Analytics", domain="https://www.acme.example/x"
        )
        polled = await graph.entity(db, ctx, kind="company", name="Acme", domain="acme.example")
        await db.commit()

        assert typed.id == polled.id
        assert typed.domain == "acme.example"

    async def test_a_name_that_is_all_suffix_is_kept(self, db, org) -> None:
        """Stripping it to nothing would give an empty key, which collides with every other
        empty key -- the referral matcher's lesson, where a company really is called
        "Technology Group Ltd"."""
        ctx = OrgContext(org_id=org.id)

        row = await graph.entity(db, ctx, kind="company", name="Limited")
        await db.commit()

        assert row.norm_key == "limited"

    async def test_another_workspace_has_its_own_nodes(self, db, org) -> None:
        from app.db.models import Org

        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()

        mine = await graph.entity(
            db, OrgContext(org_id=org.id), kind="company", name="Acme", domain="acme.example"
        )
        theirs = await graph.entity(
            db, OrgContext(org_id=other.id), kind="company", name="Acme", domain="acme.example"
        )
        await db.commit()

        assert mine.id != theirs.id


class TestThePostingProducer:
    async def _post(self, db, org) -> JobPost:
        result = await jobposts.store(
            db,
            OrgContext(org_id=org.id),
            [
                Posting(
                    source="lever",
                    external_id="abc-123",
                    title="Backend Engineer",
                    apply_url=LEVER_URL,
                    posted_at=NOW,
                )
            ],
            now=NOW,
        )
        await db.commit()
        return result.rows[0]

    async def test_a_posting_becomes_a_sourced_claim(self, db, org) -> None:
        ctx = OrgContext(org_id=org.id)
        post = await self._post(db, org)

        edges = await graph.from_job_post(
            db, ctx, post=post, domain="acme.example", company_name="Acme"
        )
        await db.commit()

        assert len(edges) == 1
        assert edges[0].source_url == LEVER_URL
        assert edges[0].observed_at == NOW

    async def test_recording_it_twice_is_one_edge(self, db, org) -> None:
        """The daily poll sees yesterday's postings again every morning. Without this the graph
        grows a duplicate claim a day until a brief cites the same sentence six times."""
        ctx = OrgContext(org_id=org.id)
        post = await self._post(db, org)

        await graph.from_job_post(db, ctx, post=post, domain="acme.example", company_name="Acme")
        await db.commit()
        await graph.from_job_post(db, ctx, post=post, domain="acme.example", company_name="Acme")
        await db.commit()

        rows = (await db.scalars(select(Edge).where(Edge.org_id == org.id))).all()
        assert len(rows) == 1

    async def test_the_unique_index_catches_a_duplicate_written_around_the_service(
        self, db, org
    ) -> None:
        """`uq_edges_claim` is `NULLS NOT DISTINCT`, and without that it would deduplicate
        **nothing**: three of its seven columns are null in any row, and under the default rule
        two NULLs never collide."""
        ctx = OrgContext(org_id=org.id)
        post = await self._post(db, org)
        await graph.from_job_post(db, ctx, post=post, domain="acme.example", company_name="Acme")
        await db.commit()

        edge = await db.scalar(select(Edge).where(Edge.org_id == org.id))
        assert edge is not None

        with pytest.raises(IntegrityError) as caught:
            await db.execute(
                text(
                    "INSERT INTO app.edges "
                    "(org_id, subject_id, predicate, object_id, source_url) "
                    "VALUES (:org, :s, 'hiring_for', :o, :url)"
                ),
                {"org": org.id, "s": edge.subject_id, "o": edge.object_id, "url": LEVER_URL},
            )
        await db.rollback()

        assert "uq_edges_claim" in str(caught.value)


class TestASignalWritesTheGraph:
    """A signal and an edge are two readings of one event, written in one transaction."""

    async def _watch_and_poll(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await watchlist.add(
            db, ctx, domain="acme.example", label="Acme", ats_source="lever", ats_slug="acme"
        )
        await db.commit()
        result = await jobposts.store(
            db,
            ctx,
            [
                Posting(
                    source="lever",
                    external_id="abc-123",
                    title="Backend Engineer",
                    apply_url=LEVER_URL,
                    posted_at=NOW,
                )
            ],
            now=NOW,
        )
        produced = await intake.from_postings(
            db, ctx, source="lever", slug="acme", postings=result.rows, now=NOW
        )
        await db.commit()
        return produced

    async def test_a_hiring_signal_records_a_sourced_claim(self, db, org) -> None:
        await self._watch_and_poll(db, org)

        edges = (await db.scalars(select(Edge).where(Edge.org_id == org.id))).all()

        assert len(edges) == 1
        assert edges[0].predicate == "hiring_for"
        assert edges[0].source_url == LEVER_URL

    async def test_the_company_node_uses_the_watchs_label(self, db, org) -> None:
        """A domain is true and a poor label. The watch is where somebody typed the name."""
        await self._watch_and_poll(db, org)

        company = await db.scalar(
            select(Entity).where(Entity.org_id == org.id, Entity.kind == "company")
        )
        assert company is not None
        assert company.name == "Acme"
        assert company.domain == "acme.example"

    async def test_polling_twice_leaves_one_edge(self, db, org) -> None:
        await self._watch_and_poll(db, org)
        await self._watch_and_poll(db, org)

        edges = (await db.scalars(select(Edge).where(Edge.org_id == org.id))).all()
        assert len(edges) == 1


class TestReadingItBack:
    async def test_every_line_carries_its_source(self, api, owner, db, org) -> None:
        """**The property the brief rests on.** A sentence that arrives without a source did not
        come from this table, because the database would have refused to store it."""
        ctx = OrgContext(org_id=org.id)
        await watchlist.add(
            db, ctx, domain="acme.example", label="Acme", ats_source="lever", ats_slug="acme"
        )
        await db.commit()
        result = await jobposts.store(
            db,
            ctx,
            [
                Posting(
                    source="lever",
                    external_id="abc-123",
                    title="Backend Engineer",
                    apply_url=LEVER_URL,
                    posted_at=NOW,
                )
            ],
            now=NOW,
        )
        await intake.from_postings(
            db, ctx, source="lever", slug="acme", postings=result.rows, now=NOW
        )
        await db.commit()

        body = api.get("/api/v1/graph?domain=acme.example", headers=owner).json()

        assert body["domain"] == "acme.example"
        assert len(body["statements"]) == 1
        line = body["statements"][0]
        assert line["sentence"] == "Acme is hiring for Backend Engineer"
        assert line["source_url"] == LEVER_URL

    async def test_a_company_we_know_nothing_about_is_empty_rather_than_an_error(
        self, api, owner
    ) -> None:
        body = api.get("/api/v1/graph?domain=nobody.example", headers=owner).json()

        assert body["statements"] == []

    async def test_a_url_is_reduced_to_the_domain(self, api, owner) -> None:
        body = api.get(
            "/api/v1/graph?domain=https://www.acme.example/careers", headers=owner
        ).json()

        assert body["domain"] == "acme.example"
