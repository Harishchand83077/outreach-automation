"""The public one-click unsubscribe endpoint (ADR-033, slice S11.4).

This is the only unauthenticated write in the system, so the tests are mostly about what it
refuses and what it does not do.

The one worth reading first is `test_a_get_does_not_unsubscribe_anybody`. Mail clients, security
scanners and link-preview crawlers fetch URLs they find in a message; if GET acted, a scanner
opening the mail would opt a recipient out of a campaign they had not read, and we would record
it as their choice. RFC 8058's one-click is a POST, and that split is a safety property rather
than pedantry.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.settings import config
from app.db.models import Lead, Suppression
from app.services import unsubscribe

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")


def token_for_lead(lead) -> str:
    return unsubscribe.sign(
        org_id=lead.org_id,
        lead_id=lead.id,
        secret=unsubscribe.signing_key(config.CREDENTIAL_KEY),
    )


async def suppressions(db, org) -> list[Suppression]:
    rows = await db.scalars(select(Suppression).where(Suppression.org_id == org.id))
    return list(rows)


class TestThePostActs:
    async def test_it_suppresses_the_address(self, api, db, org, lead_factory):
        lead = await lead_factory()

        response = api.post(f"/u/{token_for_lead(lead)}")

        assert response.status_code == 200
        assert [row.value for row in await suppressions(db, org)] == [lead.email_norm]

    async def test_it_is_idempotent(self, api, db, org, lead_factory):
        """A receiving server may POST more than once, and a person may press twice."""
        lead = await lead_factory()
        token = token_for_lead(lead)

        api.post(f"/u/{token}")
        api.post(f"/u/{token}")

        assert len(await suppressions(db, org)) == 1

    async def test_the_suppression_records_why(self, api, db, org, lead_factory):
        """Months later, "why is this address suppressed?" has to be answerable."""
        lead = await lead_factory()

        api.post(f"/u/{token_for_lead(lead)}")

        rows = await suppressions(db, org)
        assert rows and "one-click" in rows[0].reason


class TestTheGetDoesNot:
    async def test_a_get_does_not_unsubscribe_anybody(self, api, db, org, lead_factory):
        """**The test this endpoint exists to pass.**

        A scanner or link preview that fetched the URL would otherwise opt somebody out of a
        campaign they had not read, and the record would say they chose to.
        """
        lead = await lead_factory()

        response = api.get(f"/u/{token_for_lead(lead)}")

        assert response.status_code == 200
        assert await suppressions(db, org) == []

    async def test_it_offers_a_form_that_posts(self, api, db, org, lead_factory):
        lead = await lead_factory()

        body = api.get(f"/u/{token_for_lead(lead)}").text

        assert 'method="post"' in body.lower()

    async def test_an_invalid_token_gets_a_page_rather_than_a_crash(self, api):
        response = api.get("/u/not-a-real-token")

        assert response.status_code == 404
        assert "not valid" in response.text


class TestWhatItRefuses:
    async def test_a_forged_token_changes_nothing(self, api, db, org, lead_factory):
        """A token signed with another secret must not act, or anyone could unsubscribe anyone."""
        lead = await lead_factory()
        forged = unsubscribe.sign(org_id=lead.org_id, lead_id=lead.id, secret="not-our-secret")

        response = api.post(f"/u/{forged}")

        assert response.status_code == 200, "a public endpoint should not advertise what failed"
        assert await suppressions(db, org) == []

    async def test_an_unknown_token_answers_the_same_as_a_known_one(self, api, db, org):
        """Answering differently would make this an oracle for which tokens exist.

        From the recipient's side the outcome is identical either way: they will not be emailed.
        """
        stranger = unsubscribe.sign(
            org_id=uuid.uuid4(),
            lead_id=uuid.uuid4(),
            secret=unsubscribe.signing_key(config.CREDENTIAL_KEY),
        )

        response = api.post(f"/u/{stranger}")

        assert response.status_code == 200
        assert await suppressions(db, org) == []

    async def test_a_token_naming_a_lead_from_another_workspace_does_nothing(
        self, api, db, org, lead_factory
    ):
        """ADR-004 on an unauthenticated route: the signature binds the org to the lead.

        Re-signing a real lead id under a *different* org id is the one forgery the MAC cannot
        catch by itself -- the attacker holds no secret, so they cannot -- but the route checks
        the pairing before it writes.
        """
        lead = await lead_factory()
        mismatched = unsubscribe.sign(
            org_id=uuid.uuid4(),
            lead_id=lead.id,
            secret=unsubscribe.signing_key(config.CREDENTIAL_KEY),
        )

        response = api.post(f"/u/{mismatched}")

        assert response.status_code == 200
        assert await suppressions(db, org) == []

    async def test_an_erased_lead_is_handled_quietly(self, api, db, org, lead_factory):
        """Erasure leaves a hashed suppression behind, so the token outliving the lead is normal."""
        lead = await lead_factory()
        token = token_for_lead(lead)
        await db.delete(await db.get(Lead, lead.id))
        await db.commit()

        response = api.post(f"/u/{token}")

        assert response.status_code == 200


class TestTheRouteCannotSend:
    def test_it_imports_nothing_that_can_send(self):
        """The structural rule every new router gets, per the plan.

        An unauthenticated route that could reach the mail services would be the worst surface
        in the system, so it is asserted rather than assumed.
        """
        import ast
        from pathlib import Path

        from app.api.routers import unsubscribe as route

        source = Path(route.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any(name.startswith("app.services.mail") for name in imported)
        assert not any("smtp" in name.lower() for name in imported)
