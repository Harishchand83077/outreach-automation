"""The served tool list is exactly what the token's scopes allow (F24, ADR-056).

**The test is the acceptance criterion for the slice**, and it tests the mechanism rather than
restating the data: for each scope, mint a token carrying only that scope and assert `tools/list`
returns exactly the tools whose `required_scope` is in it. A hand-written map of names to scopes
would be a second copy of the catalogue, and the first time the two disagreed the test would be
describing the wrong one.

A tool a credential cannot call is worse than an absent one -- the model tries it, is refused, and
tries again differently -- so the filtering is not politeness, it is what makes the catalogue
honest about what this credential can do.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import API_TOKEN_SCOPES, AnswerBankEntry, ApiToken, Edge, Member, ProfileFact
from app.db.repo import OrgContext
from app.services import api_tokens
from app.services.mcp import protocol
from app.services.mcp.tools import CATALOGUE

from .test_api_settings import api, keypair, member, owner  # noqa: F401
from .test_mcp_endpoint import MCP, _body, _headers

pytestmark = pytest.mark.asyncio(loop_scope="session")

MCP_SCOPES = tuple(name for name in API_TOKEN_SCOPES if name.startswith("mcp:"))


async def _token(db, org, *scopes: str) -> str:
    subject = f"scoped-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin"))
    minted = await api_tokens.create_token(
        db, org_id=org.id, user_id=subject, name="Test", scopes=list(scopes)
    )
    await db.commit()
    return minted.secret


def _listed(api, token: str) -> set[str]:
    response = api.post(MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list"))
    assert response.status_code == 200
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def _call(api, token: str, tool: str, arguments: dict | None = None):
    return api.post(
        MCP,
        json=_body("tools/call", {"name": tool, "arguments": arguments or {}}),
        headers=_headers(token, method="tools/call", name=tool),
    )


class TestTheCatalogueAndTheScopesAgree:
    def test_every_tools_scope_is_one_this_deployment_can_grant(self) -> None:
        """A tool whose scope no token can hold is a tool nobody can call, and an absence
        nobody would notice."""
        for tool in CATALOGUE:
            assert tool.required_scope in MCP_SCOPES

    def test_every_grantable_mcp_scope_has_at_least_one_tool(self) -> None:
        """And the other direction: a scope with no tool is a tick box that does nothing, which
        is worse than no tick box -- somebody ticks it and believes they granted something."""
        covered = {tool.required_scope for tool in CATALOGUE}
        assert covered == set(MCP_SCOPES)

    def test_the_scope_vocabulary_is_read_from_one_place(self) -> None:
        """`models.API_TOKEN_SCOPES` is what the check constraint is generated from, so the
        constraint, the minting route and each tool's gate cannot disagree. This literal used to
        be written out in `models.py` *and* in two migrations, and the copy in `models.py` was
        two migrations stale."""
        assert api_tokens.ALL_SCOPES == API_TOKEN_SCOPES
        assert set(protocol.SCOPES) <= set(API_TOKEN_SCOPES)


class TestTheServedListMatchesTheGrant:
    @pytest.mark.parametrize("scope", MCP_SCOPES)
    async def test_one_scope_serves_exactly_its_own_tools(self, api, db, org, scope):
        expected = {tool.name for tool in CATALOGUE if tool.required_scope == scope}
        assert expected, f"{scope} has no tools, which the other test should have caught"
        assert _listed(api, await _token(db, org, scope)) == expected

    async def test_two_scopes_serve_the_union(self, api, db, org):
        token = await _token(db, org, protocol.READ_SCOPE, protocol.PROFILE_SCOPE)
        expected = {
            tool.name
            for tool in CATALOGUE
            if tool.required_scope in {protocol.READ_SCOPE, protocol.PROFILE_SCOPE}
        }
        assert _listed(api, token) == expected

    async def test_every_scope_serves_the_whole_catalogue(self, api, db, org):
        token = await _token(db, org, *MCP_SCOPES)
        assert _listed(api, token) == {tool.name for tool in CATALOGUE}

    async def test_a_tool_outside_the_grant_is_refused_and_not_merely_hidden(self, api, db, org):
        """Hiding it is for the model; refusing it is for the security. A list that omitted a
        tool while the endpoint still ran it would be a filter in the wrong layer."""
        token = await _token(db, org, protocol.READ_SCOPE)
        assert "get_profile_facts" not in _listed(api, token)

        # The refusal arrives as `isError` inside a 200 result, which is the MCP convention: a
        # tool failure is a result the client shows the model, not a transport failure. What
        # matters is that the handler never ran.
        result = _call(api, token, "get_profile_facts").json()["result"]
        assert result["isError"] is True
        assert "not" in result["content"][0]["text"].lower()


class TestTheProfileScope:
    async def test_confirmed_facts_are_returned_by_id(self, api, db, org):
        db.add(
            ProfileFact(
                org_id=org.id,
                fact_id="pp1",
                kind="skill",
                statement="Five years of Python experience.",
                source="user",
                confirmed_at=datetime.now(UTC),
            )
        )
        await db.commit()
        token = await _token(db, org, protocol.PROFILE_SCOPE)

        detail = _call(api, token, "get_profile_facts").json()["result"]["structuredContent"]
        assert {one["fact_id"] for one in detail["facts"]} == {"pp1"}

    async def test_an_unconfirmed_fact_is_not_returned(self, api, db, org):
        """A proposed fact is one nobody has agreed to say yet, and the person asked about a
        claim in an interview is the person who has to have agreed to it (ADR-037). Handing an
        agent the unconfirmed ones would route around the only check that rule has."""
        db.add(
            ProfileFact(
                org_id=org.id,
                fact_id="pp9",
                kind="skill",
                statement="Ten years of Kubernetes.",
                source="resume",
                confirmed_at=None,
            )
        )
        await db.commit()
        token = await _token(db, org, protocol.PROFILE_SCOPE)

        assert "Kubernetes" not in _call(api, token, "get_profile_facts").text

    async def test_the_answer_itself_is_never_returned(self, api, db, org):
        """**The exclusion is the tool.** `answer_bank` holds expected pay, notice period and
        work authorisation: answers given to a form the person is filling in. A model drafting a
        cover letter has no use for the number and every opportunity to put it somewhere it
        should not be, so the question is returned and the value is not."""
        db.add(
            AnswerBankEntry(
                org_id=org.id,
                question_pattern="expected salary",
                answer="2400000 INR",
                fact_ids=["pp1"],
                approved_at=datetime.now(UTC),
            )
        )
        await db.commit()
        token = await _token(db, org, protocol.PROFILE_SCOPE)

        response = _call(api, token, "list_answer_questions")
        assert "2400000" not in response.text
        detail = response.json()["result"]["structuredContent"]
        assert [one["question"] for one in detail["questions"]] == ["expected salary"]
        # And the shape carries no field an answer could arrive in later by accident.
        assert "answer" not in detail["questions"][0]
        # The field is `ready_to_use` rather than `approved`, because that whole literal is
        # reserved by the safety guard that stops this package naming a draft status.
        assert detail["questions"][0]["ready_to_use"] is True

    async def test_the_profile_scope_does_not_open_the_rest_of_the_workspace(self, api, db, org):
        token = await _token(db, org, protocol.PROFILE_SCOPE)
        assert "list_leads" not in _listed(api, token)


class TestTheNotesScope:
    async def test_a_note_with_a_source_is_recorded_as_an_edge(self, api, db, org):
        token = await _token(db, org, protocol.NOTES_SCOPE)
        response = _call(
            api,
            token,
            "propose_company_note",
            {
                "domain": "acme.example",
                "note": "They opened a Bengaluru office",
                "source_url": "https://acme.example/blog/bengaluru",
            },
        )
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"]["created"] is True

        rows = (await db.scalars(select(Edge).where(Edge.org_id == org.id))).all()
        assert any(row.source_url == "https://acme.example/blog/bengaluru" for row in rows)

    async def test_a_note_without_a_source_is_refused(self, api, db, org):
        """Refused by the service with a sentence, and by `ck_edges_provenance` underneath it.
        The service refuses first so the caller is told which claim it was rather than handed an
        `IntegrityError`; the constraint is the last line of defence, not the first."""
        token = await _token(db, org, protocol.NOTES_SCOPE)
        response = _call(
            api,
            token,
            "propose_company_note",
            {"domain": "acme.example", "note": "They are doing well", "source_url": ""},
        )
        text = response.json()["result"]["content"][0]["text"]
        assert "URL" in text

    async def test_the_database_refuses_an_unsourced_edge_whatever_the_service_does(self, db, org):
        """Raw SQL, because testing it through the service would only prove the service is
        careful today -- and a note is the one path where the thing offering the claim is a
        model."""
        from sqlalchemy import text as sql

        from app.services.graph import store as graph

        # An entity to point at, or the INSERT ... SELECT would insert no rows and the test
        # would pass for the wrong reason -- the shape this project has caught six times.
        company = await graph.entity(
            db, OrgContext(org_id=org.id), kind="company", name="Acme", domain="acme.example"
        )
        await db.commit()

        with pytest.raises(IntegrityError):
            await db.execute(
                sql(
                    """
                    INSERT INTO app.edges (org_id, subject_id, predicate, object_text)
                    VALUES (:org, :subject, 'mentioned', 'no source for this')
                    """
                ),
                {"org": org.id, "subject": company.id},
            )
        await db.rollback()

    async def test_the_notes_scope_cannot_propose_a_draft(self, api, db, org):
        """The narrowest write in the catalogue stays the narrowest: a note is a line in the
        graph that nothing may cite, and a draft is an email a person will approve."""
        token = await _token(db, org, protocol.NOTES_SCOPE)
        assert "propose_draft" not in _listed(api, token)


class TestTheDatabaseIsTheAuthority:
    async def test_a_scope_this_deployment_has_never_heard_of_is_refused(self, db, org):
        """The constraint is generated from `API_TOKEN_SCOPES`, so `mcp:send` stays
        unexpressible even in `psql` -- which is what keeps the catalogue's story true
        independently of the code."""
        from sqlalchemy import text as sql

        with pytest.raises(IntegrityError):
            await db.execute(
                sql(
                    """
                    INSERT INTO app.api_tokens
                        (org_id, user_id, name, token_prefix, token_hash, scopes)
                    VALUES (:org, 'someone', 'bad', 'oa_x', 'notahash', ARRAY['mcp:send'])
                    """
                ),
                {"org": org.id},
            )
        await db.rollback()

    async def test_an_extension_token_is_still_refused_at_this_door(self, api, db, org):
        """ADR-039, unchanged by any of this: five scopes, and the extension's still opens
        exactly one door."""
        token = await _token(db, org, api_tokens.EXT_SUGGEST_SCOPE)
        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
        )
        assert response.status_code == 401

    async def test_the_minting_route_refuses_an_unknown_scope_before_the_insert(self, db, org):
        """Named before the constraint sees it, so the error says which scope rather than
        arriving as a violation nobody can read. The constraint is still the authority."""
        with pytest.raises(ValueError, match="mcp:send"):
            await api_tokens.create_token(
                db, org_id=org.id, user_id="someone", name="bad", scopes=["mcp:send"]
            )
        await db.rollback()


class TestTheTickBoxes:
    async def test_everything_is_off_unless_asked_for(self, api, db, org, owner):
        response = api.post("/api/v1/mcp-tokens", json={"name": "Plain"}, headers=owner)
        assert response.status_code == 201
        assert response.json()["scopes"] == [protocol.READ_SCOPE]

    async def test_each_box_adds_exactly_one_scope(self, api, db, org, owner):
        response = api.post(
            "/api/v1/mcp-tokens",
            json={
                "name": "Everything",
                "may_propose": True,
                "may_read_profile": True,
                "may_write_notes": True,
            },
            headers=owner,
        )
        assert set(response.json()["scopes"]) == set(MCP_SCOPES)

    async def test_the_scopes_are_recorded_in_the_audit_trail(self, api, db, org, owner):
        """A credential granted more than the default deserves a row saying who granted it."""
        from app.db.models import Event

        api.post(
            "/api/v1/mcp-tokens",
            json={"name": "Noted", "may_write_notes": True},
            headers=owner,
        )
        rows = (
            await db.scalars(
                select(Event)
                .where(Event.org_id == org.id, Event.type == "mcp.token_created")
                .order_by(Event.at.desc())
                .limit(1)
            )
        ).all()
        assert rows
        assert protocol.NOTES_SCOPE in rows[0].data["scopes"]

    async def test_an_extension_token_still_gets_only_its_own_scope(self, api, db, org, owner):
        response = api.post(
            "/api/v1/mcp-tokens",
            json={"name": "Ext", "for_extension": True, "may_propose": True},
            headers=owner,
        )
        assert response.json()["scopes"] == [api_tokens.EXT_SUGGEST_SCOPE]


class TestNothingHasGainedTheAbilityToSend:
    def test_no_tool_sends_or_approves_whatever_its_scope(self) -> None:
        for tool in CATALOGUE:
            assert tool.sends is False
            assert tool.approves is False

    def test_there_is_no_send_scope_to_grant(self) -> None:
        assert not any("send" in scope for scope in API_TOKEN_SCOPES)

    async def test_a_revoked_token_loses_every_scope_at_once(self, api, db, org):
        token = await _token(db, org, *MCP_SCOPES)
        row = await db.scalar(select(ApiToken).order_by(ApiToken.created_at.desc()).limit(1))
        assert row is not None
        row.revoked_at = datetime.now(UTC)
        await db.commit()

        response = api.post(
            MCP, json=_body("tools/list"), headers=_headers(token, method="tools/list")
        )
        assert response.status_code == 401


class TestItStillResolvesTheMembership:
    async def test_tenancy_holds_for_the_new_tools(self, api, db, org):
        """ADR-004: `org_id` from the membership, never from an argument -- and neither new
        read tool takes one."""
        from app.db.models import Org

        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        db.add(
            ProfileFact(
                org_id=other.id,
                fact_id="pp1",
                kind="skill",
                statement="Their private fact.",
                source="user",
                confirmed_at=datetime.now(UTC),
            )
        )
        await db.commit()

        token = await _token(db, org, protocol.PROFILE_SCOPE)
        assert "Their private fact" not in _call(api, token, "get_profile_facts").text

    def test_no_new_tool_takes_an_org_id(self) -> None:
        for tool in CATALOGUE:
            assert "org_id" not in (tool.input_schema.get("properties") or {})
