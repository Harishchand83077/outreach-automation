"""Each workspace's rehearsal list is its own.

`SEND_ALLOWLIST` was one environment variable for the whole deployment. That was right while
there was one user and wrong the moment there were ten: a new workspace could select `allowlist`
mode and find no way to put its own address on the list, while reading the operator's addresses
out of its own settings screen. ADR-023 made it `app.send_allowlist`, per org.

**The decision did not move.** `allowlist_permits()` in `smtp.py` is still the only function that
compares a recipient against anything, and `tests/unit/test_send_guard.py` asserts that over the
whole tree. These tests are about the *values*: who may read them, who may change them, and that
one org's list can never widen another's.
"""

# Importing a fixture is how pytest shares one between modules, and taking it as a parameter
# then "redefines" the imported name -- which is exactly what is supposed to happen. Stated once
# here rather than on each parameter it would otherwise decorate, as in `test_attachments.py`.
# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import Member, Org, SendAllowlistEntry

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _address(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


class TestReadingAndWriting:
    async def test_an_added_address_comes_back(self, api, owner):
        address = _address("ben")

        created = api.post("/api/v1/admin/send-allowlist", headers=owner, json={"email": address})
        assert created.status_code == 201, created.text
        assert created.json()["added"] is True

        listed = api.get("/api/v1/admin/send-allowlist", headers=owner).json()
        assert address in [row["email"] for row in listed["allowlist"]]

    async def test_the_address_is_lower_cased(self, api, owner):
        """`allowlist_permits` compares lower-cased.

        A list whose entries do not match the comparison is a list that silently permits nobody,
        and the symptom is a rehearsal that sends nothing with no reason on screen.
        """
        address = _address("Mixed").upper()

        api.post("/api/v1/admin/send-allowlist", headers=owner, json={"email": address})

        listed = api.get("/api/v1/admin/send-allowlist", headers=owner).json()
        assert address.lower() in [row["email"] for row in listed["allowlist"]]

    async def test_adding_twice_is_idempotent(self, api, owner):
        address = _address("twice")

        first = api.post("/api/v1/admin/send-allowlist", headers=owner, json={"email": address})
        second = api.post("/api/v1/admin/send-allowlist", headers=owner, json={"email": address})

        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["added"] is False

    async def test_removing_takes_it_off(self, api, owner):
        address = _address("gone")
        created = api.post(
            "/api/v1/admin/send-allowlist", headers=owner, json={"email": address}
        ).json()

        removed = api.delete(f"/api/v1/admin/send-allowlist/{created['id']}", headers=owner)

        assert removed.status_code == 200
        listed = api.get("/api/v1/admin/send-allowlist", headers=owner).json()
        assert address not in [row["email"] for row in listed["allowlist"]]

    async def test_a_malformed_address_is_refused(self, api, owner):
        response = api.post(
            "/api/v1/admin/send-allowlist", headers=owner, json={"email": "not-an-address"}
        )
        assert response.status_code == 422


class TestItIsNotSharedBetweenWorkspaces:
    """The property that replaces "report a count instead of the list" (ADR-004)."""

    async def test_one_org_never_sees_anothers(self, api, owner, db, keypair):
        mine = _address("mine")
        api.post("/api/v1/admin/send-allowlist", headers=owner, json={"email": mine})

        stranger = Org(name="Somebody Else")
        db.add(stranger)
        await db.commit()
        sub = f"sub-{uuid.uuid4()}"
        db.add(Member(org_id=stranger.id, user_id=sub, email=_address("them"), role="owner"))
        db.add(
            SendAllowlistEntry(org_id=stranger.id, email_norm=_address("theirs"), added_by="them")
        )
        await db.commit()

        private, _ = keypair
        theirs = api.get(
            "/api/v1/admin/send-allowlist",
            headers={"Authorization": f"Bearer {token_for(private, subject=sub)}"},
        ).json()

        assert mine not in [row["email"] for row in theirs["allowlist"]]

    async def test_another_orgs_entry_cannot_be_deleted(self, api, owner, db):
        stranger = Org(name="Not Yours")
        db.add(stranger)
        await db.commit()
        entry = SendAllowlistEntry(
            org_id=stranger.id, email_norm=_address("theirs"), added_by="them"
        )
        db.add(entry)
        await db.commit()

        response = api.delete(f"/api/v1/admin/send-allowlist/{entry.id}", headers=owner)

        # 404 rather than 403: another org's rows do not exist as far as this caller is
        # concerned, and a 403 would confirm that the id is real.
        assert response.status_code == 404
        still_there = await db.scalar(
            select(SendAllowlistEntry).where(SendAllowlistEntry.id == entry.id)
        )
        assert still_there is not None


class TestWhoMayChangeIt:
    async def test_a_member_may_not_read_it(self, api, member):
        """It is a sending rule, and the gate in this API is on the rules, not on the work."""
        assert api.get("/api/v1/admin/send-allowlist", headers=member).status_code == 403

    async def test_a_member_may_not_add_to_it(self, api, member):
        """Adding widens who may be emailed. A reviewer reviews."""
        response = api.post(
            "/api/v1/admin/send-allowlist", headers=member, json={"email": _address("nope")}
        )
        assert response.status_code == 403
