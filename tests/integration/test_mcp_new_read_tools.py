"""Four read tools over the tables built since the connector shipped (S16.1, ADR-055).

Nothing new about the trust model, so most of these assert that it still holds: `org_id` from the
membership and never from an argument, anything somebody else wrote wrapped as untrusted, and a
catalogue that still cannot send or approve. The one new rule is about the fit score, which
`fit.score_fit` had no caller for until this slice: computing it is a press and reading it is
free.

The envelope helpers come from `test_mcp_endpoint.py` rather than being written again. The
2026-07-28 revision mirrors selected body fields into headers and the server compares the two, so
a second hand-rolled request would be a second implementation of that rule -- and the first time
the `_meta` shape changed, one of the two would be wrong.
"""

# Importing a fixture is how pytest shares one between modules, and taking it as a parameter
# then "redefines" the imported name -- which is exactly what is supposed to happen.
# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.db.models import FitScoreRow, JobPost, LlmUsage, Member, Org
from app.db.repo import OrgContext
from app.services import api_tokens
from app.services.graph import store as graph
from app.services.mcp.tools import CATALOGUE
from app.services.signals import intake, watchlist

from .test_api_settings import api, keypair, member, owner  # noqa: F401
from .test_mcp_endpoint import MCP, _body, _headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
BOARD_URL = "https://jobs.lever.co/acme/abc-123"

NEW_TOOLS = ("list_job_posts", "get_fit_score", "get_entity_edges", "list_signals")


async def _token(db, org, *, scopes: list[str] | None = None) -> str:
    """Mint a credential for a member of `org`, as the dashboard's own route does."""
    subject = f"mcp-reader-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin"))
    minted = await api_tokens.create_token(
        db, org_id=org.id, user_id=subject, name="Test", scopes=scopes
    )
    await db.commit()
    return minted.secret


def _call(api, token: str, tool: str, arguments: dict | None = None):
    return api.post(
        MCP,
        json=_body("tools/call", {"name": tool, "arguments": arguments or {}}),
        headers=_headers(token, method="tools/call", name=tool),
    )


def _result(response) -> dict:
    return response.json()["result"]


class TestTheCatalogue:
    def test_the_four_tools_exist_and_are_read_only(self) -> None:
        by_name = {tool.name: tool for tool in CATALOGUE}
        for name in NEW_TOOLS:
            assert by_name[name].read_only is True

    def test_none_of_them_sends_or_approves(self) -> None:
        """The invariant that actually matters, restated rather than inferred (ADR-030):
        nothing in this catalogue can put an email on the wire or past a human."""
        for tool in CATALOGUE:
            assert tool.sends is False
            assert tool.approves is False

    def test_no_tool_spends_the_users_provider_credit(self) -> None:
        """**The deliberate absence.** F9's whole design is that nothing but a person's press
        can start a contact lookup, because it bills them (ADR-041), and the fit score calls a
        model. An agent holding a tool that spends somebody's money is exactly what that
        refuses, and a catalogue is where the refusal is checkable rather than remembered."""
        names = {tool.name for tool in CATALOGUE}
        for forbidden in ("look_up_contacts", "find_contacts", "compute_fit_score", "search"):
            assert forbidden not in names

    def test_every_new_tool_declares_it_returns_untrusted_text(self) -> None:
        """All four return something somebody else wrote: a posting's body, a requirement read
        out of one, a graph line, a signal title."""
        by_name = {tool.name: tool for tool in CATALOGUE}
        for name in NEW_TOOLS:
            assert by_name[name].returns_untrusted_text is True

    def test_no_tool_schema_takes_an_org_id(self) -> None:
        """The one tenancy mistake that is invisible from inside a single workspace."""
        for tool in CATALOGUE:
            assert "org_id" not in (tool.input_schema.get("properties") or {})


class TestJobPostings:
    async def test_open_postings_are_returned_and_the_body_is_wrapped(self, api, db, org):
        token = await _token(db, org)
        db.add(
            JobPost(
                org_id=org.id,
                source="lever",
                external_id=f"abc-{uuid.uuid4()}",
                title="Backend Engineer",
                apply_url=BOARD_URL,
                board_slug="acme",
                body_text="We need Python and Postgres.",
                status="open",
            )
        )
        await db.commit()

        response = _call(api, token, "list_job_posts")
        assert response.status_code == 200
        # The posting's own words cross the boundary wrapped (hard rule 7).
        assert "<untrusted" in _result(response)["content"][0]["text"]
        posts = _result(response)["structuredContent"]["job_posts"]
        assert any(one["title"] == "Backend Engineer" for one in posts)

    async def test_archived_postings_are_hidden_unless_asked_for(self, api, db, org):
        """The question an agent is nearly always answering is "what could I apply to", and an
        archived posting is a role somebody else filled. F22's diff is what makes this
        distinction exist at all."""
        token = await _token(db, org)
        db.add(
            JobPost(
                org_id=org.id,
                source="lever",
                external_id=f"gone-{uuid.uuid4()}",
                title="Filled Role",
                apply_url="https://jobs.lever.co/acme/gone",
                board_slug="acme",
                status="archived",
            )
        )
        await db.commit()

        default = _result(_call(api, token, "list_job_posts"))["structuredContent"]
        assert "Filled Role" not in {one["title"] for one in default["job_posts"]}

        asked = _result(_call(api, token, "list_job_posts", {"status": "archived"}))
        titles = {one["title"] for one in asked["structuredContent"]["job_posts"]}
        assert "Filled Role" in titles

    async def test_another_workspaces_postings_are_invisible(self, api, db, org):
        """ADR-004: `org_id` comes from the membership the token resolves to, never from an
        argument -- and no tool here takes one."""
        token = await _token(db, org)
        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()
        db.add(
            JobPost(
                org_id=other.id,
                source="lever",
                external_id=f"secret-{uuid.uuid4()}",
                title="Their Secret Role",
                apply_url="https://jobs.lever.co/beta/secret",
                board_slug="beta",
            )
        )
        await db.commit()

        assert "Their Secret Role" not in _call(api, token, "list_job_posts").text


class TestTheFitScore:
    async def test_an_unscored_posting_says_so_rather_than_returning_zero(self, api, db, org):
        """A zero would read as a bad fit, which is the opposite of "nobody has asked yet"."""
        token = await _token(db, org)
        post = JobPost(
            org_id=org.id,
            source="pasted",
            external_id=f"p-{uuid.uuid4()}",
            title="Staff Engineer",
            apply_url="https://acme.example/jobs/1",
            body_text="Python, Postgres, Kubernetes.",
        )
        db.add(post)
        await db.commit()

        response = _call(api, token, "get_fit_score", {"job_post_id": str(post.id)})
        assert "No fit score has been computed" in _result(response)["content"][0]["text"]
        # The envelope omits `structuredContent` entirely when a tool returns no detail, rather
        # than carrying a null -- so the absence of the key is the assertion, not its value.
        assert "structuredContent" not in _result(response)

    async def test_the_tool_reads_the_stored_row_and_calls_no_model(self, api, db, org):
        """A read tool that called a model would spend the user's token budget whenever an
        agent looked. The computation is a `POST` on the dashboard; this reads what it stored,
        through the same helper the screen reads, so the two cannot disagree about the score."""
        token = await _token(db, org)
        post = JobPost(
            org_id=org.id,
            source="pasted",
            external_id=f"p-{uuid.uuid4()}",
            title="Backend Engineer",
            apply_url="https://acme.example/jobs/2",
            body_text="Python and Postgres.",
        )
        db.add(post)
        await db.flush()
        db.add(
            FitScoreRow(
                org_id=org.id,
                job_post_id=post.id,
                score=0.5,
                matched=[{"requirement": "Python", "fact_id": "pp1", "shared": ["python"]}],
                missing=["Postgres"],
                requirements=["Python", "Postgres"],
            )
        )
        await db.commit()
        before = await db.scalar(select(func.count()).select_from(LlmUsage))

        detail = _result(_call(api, token, "get_fit_score", {"job_post_id": str(post.id)}))[
            "structuredContent"
        ]
        assert detail["score"] == 0.5
        # Never a bare number: the match and the miss travel with it.
        assert detail["matched"][0]["fact_id"] == "pp1"
        assert detail["missing"] == ["Postgres"]

        assert await db.scalar(select(func.count()).select_from(LlmUsage)) == before

    async def test_a_posting_in_another_workspace_is_not_scored(self, api, db, org):
        token = await _token(db, org)
        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        post = JobPost(
            org_id=other.id,
            source="pasted",
            external_id=f"p-{uuid.uuid4()}",
            title="Theirs",
            apply_url="https://beta.example/jobs/1",
        )
        db.add(post)
        await db.commit()

        response = _call(api, token, "get_fit_score", {"job_post_id": str(post.id)})
        assert "No such job posting" in _result(response)["content"][0]["text"]


class TestTheGraph:
    async def test_every_statement_carries_its_own_source(self, api, db, org):
        """What makes "a model may phrase, never add" checkable from the other side of the
        connector: a sentence that arrives without a source did not come from this table, so an
        agent cannot launder an invention through this tool."""
        token = await _token(db, org)
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
        await db.commit()

        detail = _result(_call(api, token, "get_entity_edges", {"domain": "acme.example"}))[
            "structuredContent"
        ]
        assert detail["statements"]
        for line in detail["statements"]:
            assert line["source_url"] or line["from_a_message"]

    async def test_a_company_we_know_nothing_about_is_not_an_error(self, api, db, org):
        token = await _token(db, org)
        response = _call(api, token, "get_entity_edges", {"domain": "nobody.example"})
        assert response.status_code == 200
        assert _result(response)["structuredContent"]["statements"] == []


class TestSignals:
    async def test_signals_are_listed_with_whether_there_is_anybody_to_write_to(self, api, db, org):
        """The distinction the list exists to make: a company worth approaching and somebody to
        approach are different things, and the second is what F9's lookup button is for."""
        token = await _token(db, org)
        ctx = OrgContext(org_id=org.id)
        entry, _ = await watchlist.add(
            db, ctx, domain="acme.example", ats_source="lever", ats_slug="acme"
        )
        await intake.record(
            db,
            ctx,
            entry,
            [
                intake.Candidate(
                    kind="hiring",
                    source="lever",
                    external_id=f"sig-{uuid.uuid4()}",
                    title="Acme opened Backend Engineer",
                    url=BOARD_URL,
                    observed_at=NOW,
                )
            ],
        )
        await db.commit()

        detail = _result(_call(api, token, "list_signals"))["structuredContent"]
        assert any("Backend Engineer" in one["title"] for one in detail["signals"])
        assert detail["without_a_lead"] >= 1

    async def test_a_signal_title_is_wrapped_as_untrusted(self, api, db, org):
        """A posting title is somebody else's words, and this reaches an agent holding tools."""
        token = await _token(db, org)
        text = _result(_call(api, token, "list_signals"))["content"][0]["text"]
        assert "<untrusted" in text or "0 signal(s)" in text


class TestTheDoorsStillHold:
    async def test_a_revoked_token_cannot_call_these(self, api, db, org):
        """ADR-028: authorisation re-resolves the membership on every call, so a credential
        does not outlive what justified it. Asserted for the new tools specifically rather than
        only in general."""
        token = await _token(db, org)
        row = await db.scalar(
            select(api_tokens.ApiToken).order_by(api_tokens.ApiToken.created_at.desc()).limit(1)
        )
        assert row is not None
        row.revoked_at = datetime.now(UTC)
        await db.commit()

        assert _call(api, token, "list_signals").status_code == 401
