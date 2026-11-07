"""The extension gateway, a third door (F8, ADR-039, slices S13.1-S13.3).

The first class is the point of the whole design. There are now three credentials and three
doors, and each key opens exactly one: a Supabase JWT for the dashboard, an `oa_` token with an
`mcp:` scope for the connector, and an `oa_` token with `ext:suggest` for the extension. A
credential that worked everywhere would be one whose theft cost everything — and this one lives
in a browser extension on somebody's laptop.

The second point is that an unmatched form label comes back as `needs_user` rather than a guess.
That is the difference from tools that invent screening answers, and it is why no model is
reachable from this path at all.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import Capture, Member, Org, Suggestion
from app.db.repo import OrgContext
from app.services import api_tokens
from app.services.candidate import profile
from app.services.mcp import protocol

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXT = "/ext/v1"


async def mint(db, org, *, scopes: list[str]) -> str:
    subject = f"ext-user-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin"))
    minted = await api_tokens.create_token(
        db, org_id=org.id, user_id=subject, name="Extension", scopes=scopes
    )
    await db.commit()
    return minted.secret


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestThreeDoorsThreeKeys:
    async def test_an_extension_token_opens_the_gateway(self, api, db, org):
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        response = api.get(f"{EXT}/whoami", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["connected"] is True

    async def test_an_extension_token_cannot_call_the_dashboard_api(self, api, db, org):
        """It is not a Supabase JWT, so `/api/v1` refuses it."""
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        assert api.get("/api/v1/profile/facts", headers=auth(token)).status_code == 401

    async def test_an_extension_token_cannot_call_the_mcp_endpoint(self, api, db, org):
        """**The one a reader should check.**

        The extension's credential sits in a browser on a laptop. If it also opened `/mcp` it
        would read every campaign, lead and reply in the workspace.
        """
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        response = api.post("/mcp", json={}, headers=auth(token))

        assert response.status_code == 401

    async def test_an_mcp_token_cannot_call_the_gateway(self, api, db, org):
        token = await mint(db, org, scopes=[api_tokens.READ_SCOPE])

        assert api.get(f"{EXT}/whoami", headers=auth(token)).status_code == 401

    async def test_a_dashboard_jwt_cannot_call_the_gateway(self, api, owner):
        """A perfectly valid credential for every `/api/v1` route, refused here."""
        assert api.get(f"{EXT}/whoami", headers=owner).status_code == 401

    async def test_no_credential_is_refused_with_a_way_forward(self, api):
        response = api.get(f"{EXT}/whoami")

        assert response.status_code == 401
        assert "WWW-Authenticate" in response.headers

    async def test_the_two_scopes_are_never_minted_together(self, api, owner):
        """An extension token is only ever `ext:suggest`.

        A token that could do both would be a browser extension that also reads the whole
        workspace over MCP, which is the opposite of why the doors are separate.
        """
        made = api.post(
            "/api/v1/mcp-tokens",
            headers=owner,
            json={"name": "My extension", "for_extension": True, "may_propose": True},
        )

        assert made.status_code == 201
        assert made.json()["scopes"] == [api_tokens.EXT_SUGGEST_SCOPE]

    async def test_a_revoked_membership_closes_the_door(self, api, db, org):
        """Authorisation re-resolves the member row on every call, as `/mcp` does."""
        subject = f"ext-gone-{uuid.uuid4()}"
        row = Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin")
        db.add(row)
        minted = await api_tokens.create_token(
            db,
            org_id=org.id,
            user_id=subject,
            name="Extension",
            scopes=[api_tokens.EXT_SUGGEST_SCOPE],
        )
        await db.commit()
        assert api.get(f"{EXT}/whoami", headers=auth(minted.secret)).status_code == 200

        await db.delete(row)
        await db.commit()

        assert api.get(f"{EXT}/whoami", headers=auth(minted.secret)).status_code == 401


class TestSuggesting:
    async def test_an_approved_answer_is_proposed(self, api, db, org):
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(
            db, ctx, question_pattern="notice period", answer="30 days", approve=True
        )
        await db.commit()
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        body = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={
                "page_url": "https://boards.greenhouse.io/x/jobs/1",
                "labels": ["What is your notice period?"],
            },
        ).json()

        assert body["suggestions"][0]["answer"] == "30 days"
        assert body["suggestions"][0]["state"] == "proposed"

    async def test_an_unknown_label_is_flagged_rather_than_guessed(self, api, db, org):
        """**The difference from tools that invent screening answers.**"""
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        body = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Why do you want this job?"]},
        ).json()

        assert body["suggestions"][0]["state"] == "needs_user"
        assert body["suggestions"][0]["answer"] is None

    async def test_an_unapproved_answer_is_not_offered(self, api, db, org):
        ctx = OrgContext(org_id=org.id)
        await profile.add_answer(db, ctx, question_pattern="notice period", answer="30 days")
        await db.commit()
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        body = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice period"]},
        ).json()

        assert body["suggestions"][0]["state"] == "needs_user"

    async def test_a_form_capture_stores_no_page_text(self, api, db, org):
        """Only labels are needed to match an answer.

        The rest of an application page is the user's own data and somebody else's copy, so it
        is not sent and not stored.
        """
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])
        api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice period"]},
        )

        capture = await db.scalar(select(Capture).where(Capture.org_id == org.id))

        assert capture is not None
        assert capture.page_text is None

    async def test_the_capture_records_which_token_sent_it(self, api, db, org):
        """So a device can be revoked and what it sent identified."""
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])
        api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice"]},
        )

        capture = await db.scalar(select(Capture).where(Capture.org_id == org.id))

        assert capture is not None
        assert capture.token_id is not None

    async def test_every_response_says_it_only_suggests(self, api, db, org):
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        body = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice"]},
        ).json()

        assert "submit the page yourself" in body["note"]

    async def test_a_non_http_page_url_is_refused(self, api, db, org):
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        response = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "file:///etc/passwd", "labels": ["x"]},
        )

        assert response.status_code == 422

    async def test_another_workspaces_answers_are_not_offered(self, api, db, org):
        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        await profile.add_answer(
            db,
            OrgContext(org_id=other.id),
            question_pattern="notice period",
            answer="Theirs",
            approve=True,
        )
        await db.commit()
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        body = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice period"]},
        ).json()

        assert body["suggestions"][0]["state"] == "needs_user"


class TestRecordingWhatHappened:
    async def test_an_outcome_is_recorded(self, api, db, org):
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])
        made = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice"]},
        ).json()["suggestions"][0]

        response = api.post(
            f"{EXT}/suggestions/{made['id']}/outcome",
            headers=auth(token),
            json={"state": "edited"},
        )

        assert response.status_code == 200
        row = await db.get(Suggestion, uuid.UUID(made["id"]))
        await db.refresh(row)
        assert row.state == "edited"

    async def test_an_invented_state_is_refused(self, api, db, org):
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])
        made = api.post(
            f"{EXT}/suggest",
            headers=auth(token),
            json={"page_url": "https://x.test/apply", "labels": ["Notice"]},
        ).json()["suggestions"][0]

        response = api.post(
            f"{EXT}/suggestions/{made['id']}/outcome",
            headers=auth(token),
            json={"state": "submitted"},
        )

        assert response.status_code == 422

    async def test_another_workspaces_suggestion_cannot_be_touched(self, api, db, org):
        other = Org(name="Theirs again")
        db.add(other)
        await db.commit()
        capture = Capture(org_id=other.id, page_url="https://theirs.test/apply", kind="form")
        db.add(capture)
        await db.commit()
        row = Suggestion(
            org_id=other.id, capture_id=capture.id, field_key="Notice", state="proposed"
        )
        db.add(row)
        await db.commit()
        token = await mint(db, org, scopes=[api_tokens.EXT_SUGGEST_SCOPE])

        response = api.post(
            f"{EXT}/suggestions/{row.id}/outcome",
            headers=auth(token),
            json={"state": "accepted"},
        )

        assert response.status_code == 404


class TestTheGatewayCannotAct:
    def test_it_imports_nothing_that_can_send_or_submit(self):
        """The structural rule every new router gets.

        An unauthenticated-adjacent surface that could reach the mail services or an HTTP client
        would be the worst thing in the system: a token on a laptop that can send mail.
        """
        import ast
        from pathlib import Path

        tree = ast.parse(Path("app/api/routers/extension.py").read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any(name.startswith("app.services.mail") for name in imported)
        assert not any("llm" in name for name in imported)
        for name in ("httpx", "requests", "aiohttp", "smtplib", "aiosmtplib"):
            assert name not in imported, f"the extension gateway imports {name}"

    def test_the_extension_has_exactly_one_scope_and_it_is_not_an_mcp_one(self):
        """The database's check constraint is built from this tuple.

        **Restated when F24 split the MCP scopes (ADR-056).** This asserted the exact tuple of
        three, which is a proxy: it fails whenever a scope is added for a good reason, and says
        nothing about the property that matters -- that the extension's door is one scope, that
        it is not an `mcp:` one, and that no scope exists which could open both. That is the
        ninth test in this project found to encode something other than the rule it was named
        for, and the second this week.
        """
        extension_scopes = [scope for scope in api_tokens.ALL_SCOPES if scope.startswith("ext:")]
        assert extension_scopes == [api_tokens.EXT_SUGGEST_SCOPE]
        assert not api_tokens.EXT_SUGGEST_SCOPE.startswith("mcp:")
        # No scope is in both families, which is what makes "each key opens one door" a fact
        # about the vocabulary rather than about each gate remembering.
        assert not set(extension_scopes) & set(protocol.SCOPES)

    def test_an_unknown_scope_cannot_be_minted(self):
        assert "ext:submit" not in api_tokens.ALL_SCOPES
        assert "ext:fill" not in api_tokens.ALL_SCOPES
