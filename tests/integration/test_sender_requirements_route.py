"""`GET /admin/sender-requirements` (F4, slice S11.5).

The checker itself is covered by `tests/unit/test_sender_requirements.py` against a fake
resolver. What is left for this file is the route's own behaviour: who may read it, that it
reports one entry per *domain* rather than per mailbox, and that nothing here reaches the real
network — hard rule: never the network in a test.
"""

# ruff: noqa: F811

from __future__ import annotations

import pytest

from app.db.models import Mailbox
from app.services.deliverability import sender_requirements as sr

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

ROUTE = "/api/v1/admin/sender-requirements"

ANSWERS = {
    "example.com": ["v=spf1 include:_spf.google.com ~all"],
    "google._domainkey.example.com": ["v=DKIM1; k=rsa; p=key"],
    "_dmarc.example.com": ["v=DMARC1; p=none; rua=mailto:d@example.com"],
}


class FakeResolver:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def txt(self, name: str) -> list[str] | None:
        self.asked.append(name)
        return ANSWERS.get(name, [])


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """**Never the network in a test.**

    Patched at the route's import site rather than globally, so a future caller that builds its
    own resolver is not silently covered by this and left untested.
    """
    from app.api.routers import operations

    monkeypatch.setattr(operations, "DnsTxtResolver", FakeResolver)


async def mailbox(db, org, address: str) -> Mailbox:
    row = Mailbox(org_id=org.id, from_name="Asha", from_email=address)
    db.add(row)
    await db.commit()
    return row


class TestWhoMayRead:
    async def test_an_admin_may(self, api, owner, db, org):
        await mailbox(db, org, "asha@example.com")

        assert api.get(ROUTE, headers=owner).status_code == 200

    async def test_a_member_may_not(self, api, member):
        """Deliverability is a rule about the workspace, and the gate goes on the rules."""
        assert api.get(ROUTE, headers=member).status_code == 403

    async def test_an_anonymous_caller_may_not(self, api):
        assert api.get(ROUTE).status_code == 401


class TestWhatItReports:
    async def test_one_entry_per_domain_not_per_mailbox(self, api, owner, db, org):
        """Two mailboxes on one domain are one set of DNS records, so one finding.

        Listing them twice would show one problem as two and invite somebody to 'fix' it twice.
        """
        await mailbox(db, org, "asha@example.com")
        await mailbox(db, org, "dev@example.com")

        body = api.get(ROUTE, headers=owner).json()

        assert [entry["domain"] for entry in body["domains"]] == ["example.com"]

    async def test_a_healthy_domain_is_ready(self, api, owner, db, org):
        await mailbox(db, org, "asha@example.com")

        entry = api.get(ROUTE, headers=owner).json()["domains"][0]

        verdicts = {check["name"]: check["verdict"] for check in entry["checks"]}
        assert verdicts["SPF"] == "pass"
        assert verdicts["DKIM"] == "pass"
        assert verdicts["DMARC"] == "pass"

    async def test_a_domain_with_no_records_is_not_ready(self, api, owner, db, org):
        await mailbox(db, org, "asha@nothing.test")

        entry = api.get(ROUTE, headers=owner).json()["domains"][0]

        assert entry["ready"] is False

    async def test_it_says_what_dns_cannot_prove(self, api, owner, db, org):
        """The honest limit of the whole screen, stated on the response rather than implied."""
        await mailbox(db, org, "asha@example.com")

        body = api.get(ROUTE, headers=owner).json()

        assert "not what is happening" in body["note"]

    async def test_a_workspace_with_no_mailbox_gets_an_empty_list(self, api, owner):
        body = api.get(ROUTE, headers=owner).json()

        assert body["domains"] == []

    async def test_another_workspace_s_domains_are_not_reported(self, api, owner, db, org):
        """ADR-004 on a read that looks global but is not."""
        from app.db.models import Org

        other = Org(name="Theirs")
        db.add(other)
        await db.commit()
        db.add(Mailbox(org_id=other.id, from_name="Them", from_email="them@theirs.test"))
        await db.commit()
        await mailbox(db, org, "asha@example.com")

        body = api.get(ROUTE, headers=owner).json()

        assert [entry["domain"] for entry in body["domains"]] == ["example.com"]


class TestTheCheckerAndTheRouteAgree:
    def test_the_route_uses_the_shared_checker(self):
        """The script in S10.1 will read the same function.

        A second implementation for the go-live script would be this project's signature defect:
        a screen and a script that disagree about whether a domain is ready.
        """
        import inspect

        from app.api.routers import operations

        source = inspect.getsource(operations.sender_requirements)
        assert "check_domain" in source
        assert sr.check_domain.__module__ == "app.services.deliverability.sender_requirements"
