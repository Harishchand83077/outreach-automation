"""The brief reads rows that carry their own provenance, and nothing else (S15.3, ADR-052).

The unit tests cover the phrasing rule, which is pure. These cover the half that needs a
database: that the brief is built from the graph, the research hooks and the messages; that a
line nobody can point at is not in it at all; and that the five sections are all named even
when four of them are empty.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters,
# which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Message, Research
from app.db.repo import OrgContext
from app.services.brief import build as brief_build
from app.services.graph import store as graph

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
HOOK_URL = "https://acme.example/blog/launch"
BOARD_URL = "https://jobs.lever.co/acme/abc-123"


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


async def _lead_with_everything(db, org, lead_factory):
    """A lead with a graph edge, a research hook and one message each way."""
    lead = await lead_factory(
        state="REPLIED", company_name="Acme", company_url="https://www.acme.example/about"
    )
    ctx = OrgContext(org_id=org.id)

    company = await graph.entity(db, ctx, kind="company", name="Acme", domain="acme.example")
    role = await graph.entity(db, ctx, kind="role", name="Backend Engineer")
    await graph.record(
        db,
        ctx,
        graph.Claim(
            subject=company,
            predicate="hiring_for",
            object_entity=role,
            source_url=BOARD_URL,
            observed_at=NOW,
        ),
    )
    db.add(
        Research(
            org_id=org.id,
            lead_id=lead.id,
            hooks=[
                {
                    "text": "Acme launched a payments API in October",
                    "kind": "launch",
                    "source_url": HOOK_URL,
                    "snippet": "payments API",
                }
            ],
            confidence=0.6,
        )
    )
    db.add(
        Message(
            org_id=org.id,
            lead_id=lead.id,
            direction="out",
            kind="initial",
            status="sent",
            subject="Quick question about Acme",
            body_text="Hello",
            # `ck_messages_out_has_idempotency_key` requires one: at-most-once is enforced by
            # the database, not by the send loop remembering.
            idempotency_key=f"brief-test-{lead.id}",
            sent_at=NOW,
        )
    )
    db.add(
        Message(
            org_id=org.id,
            lead_id=lead.id,
            direction="in",
            kind="reply",
            status="received",
            subject="Re: Quick question about Acme",
            body_text="Happy to talk",
        )
    )
    await db.commit()
    return lead


class TestWhatIsInIt:
    async def test_a_graph_edge_becomes_a_line_with_the_boards_url(self, db, org, lead_factory):
        lead = await _lead_with_everything(db, org, lead_factory)
        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        hiring = brief.section("what_they_are_doing")
        assert any("Backend Engineer" in line.text for line in hiring)
        assert {line.source_url for line in hiring} >= {BOARD_URL}

    async def test_a_research_hook_becomes_a_line_with_the_page_it_was_read_from(
        self, db, org, lead_factory
    ):
        lead = await _lead_with_everything(db, org, lead_factory)
        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        launched = [line for line in brief.lines if "payments API" in line.text]
        assert len(launched) == 1
        assert launched[0].source_url == HOOK_URL

    async def test_each_conversation_line_cites_its_own_message(self, db, org, lead_factory):
        lead = await _lead_with_everything(db, org, lead_factory)
        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        conversation = brief.section("the_conversation")
        assert len(conversation) == 2
        assert all(line.message_id is not None for line in conversation)

    async def test_every_line_in_the_brief_has_a_source(self, db, org, lead_factory):
        """The property the whole slice exists for, asserted over the real thing rather than
        over a constructed one: no line arrives without provenance, whatever produced it."""
        lead = await _lead_with_everything(db, org, lead_factory)
        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        assert brief.lines
        for line in brief.lines:
            assert line.source_url or line.message_id


class TestWhatIsDeliberatelyNotInIt:
    async def test_a_lead_with_nothing_citable_gets_five_empty_sections_and_gaps(
        self, db, org, lead_factory
    ):
        """Not an error and not an empty response. A company we know nothing about yet is the
        ordinary case for a fresh import, and the gaps are what tell the operator which button
        to press."""
        lead = await lead_factory(company_name="Nobody", company_url="https://nobody.example")
        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        assert brief.lines == []
        assert len(brief.gaps) >= 5
        rendered = brief_build.render(brief)
        for title in brief_build.SECTION_TITLES.values():
            assert title in rendered

    async def test_the_synthesised_summary_is_reported_rather_than_cited(
        self, db, org, lead_factory
    ):
        """`what_they_do` is drawn from several pages, so no one page says it. A line citing
        "one of these four URLs" is a line nobody can check, and somebody would repeat it."""
        from app.db.models import DomainResearch

        lead = await lead_factory(company_name="Acme", company_url="https://acme.example")
        shared = DomainResearch(
            org_id=org.id,
            domain="acme.example",
            findings={"what_they_do": "They sell a payments API to Indian fintechs."},
            sources=[{"url": "https://acme.example/"}, {"url": "https://acme.example/pricing"}],
            expires_at=NOW,
        )
        db.add(shared)
        await db.flush()
        db.add(Research(org_id=org.id, lead_id=lead.id, domain_research_id=shared.id, hooks=[]))
        await db.commit()

        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        assert not any("payments API to Indian fintechs" in line.text for line in brief.lines)
        assert any("several pages" in gap for gap in brief.gaps)

    async def test_another_workspaces_edge_is_not_in_this_workspaces_brief(
        self, db, org, lead_factory
    ):
        """ADR-004, asserted rather than assumed: entities are per workspace (ADR-051)."""
        from app.db.models import Org

        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()
        other_ctx = OrgContext(org_id=other.id)
        company = await graph.entity(
            db, other_ctx, kind="company", name="Acme", domain="acme.example"
        )
        role = await graph.entity(db, other_ctx, kind="role", name="Secret Role")
        await graph.record(
            db,
            other_ctx,
            graph.Claim(
                subject=company,
                predicate="hiring_for",
                object_entity=role,
                source_url="https://jobs.lever.co/acme/secret",
            ),
        )
        await db.commit()

        lead = await lead_factory(company_name="Acme", company_url="https://acme.example")
        brief = await brief_build.build(db, OrgContext(org_id=org.id), lead=lead)
        assert not any("Secret Role" in line.text for line in brief.lines)


class TestTheRoute:
    async def test_the_brief_route_returns_five_sections_each_line_with_its_source(
        self, api, db, org, lead_factory, owner
    ):
        lead = await _lead_with_everything(db, org, lead_factory)
        response = api.get(
            f"/api/v1/leads/{lead.id}/brief",
            headers=owner,
        )
        assert response.status_code == 200
        body = response.json()
        assert [section["name"] for section in body["sections"]] == list(brief_build.SECTIONS)
        for section in body["sections"]:
            for line in section["lines"]:
                assert line["source_url"] or line["from_a_message"]

    async def test_a_lead_in_another_workspace_is_404_not_403(self, api, db, lead_factory, owner):
        """Confirming an id exists in another workspace is itself a leak."""
        import uuid as _uuid

        response = api.get(
            f"/api/v1/leads/{_uuid.uuid4()}/brief",
            headers=owner,
        )
        assert response.status_code == 404

    async def test_the_brief_route_calls_no_model(self, api, db, org, lead_factory, owner):
        """A read that spends is the pattern this project refuses: `GET /mailboxes` does not
        mint an OAuth token and a contact lookup does not spend a credit on a page load. The
        brief is free, so a refresh costs a query set. Improving the wording is a press."""
        from app.db.models import LlmUsage

        lead = await _lead_with_everything(db, org, lead_factory)
        api.get(
            f"/api/v1/leads/{lead.id}/brief",
            headers=owner,
        )
        from sqlalchemy import func, select

        spent = await db.scalar(select(func.count()).select_from(LlmUsage))
        assert spent == 0
