"""Upload a connections export, find who is at a company (F10, ADR-042, slice S13.6).

The properties worth testing here are the ones a unit test cannot see: that a second export
updates rather than doubles the network, that one workspace's connections are invisible to
another, that the note cites only a **confirmed** profile fact, and that deleting really deletes.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import Connection, JobPost, Member, Org, ProfileFact

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
PREAMBLE = 'Notes:\n"When exporting your connection data..."\n\n'


def _export(*rows: str) -> bytes:
    return (PREAMBLE + "\n".join([HEADER, *rows]) + "\n").encode("utf-8")


def _upload(api, headers, data: bytes):
    return api.post(
        "/api/v1/connections/import",
        files={"file": ("Connections.csv", data, "text/csv")},
        headers=headers,
    )


ASHA = (
    "Asha,Rao,https://example.com/in/asha,asha@example.com,Acme Inc,Head of Engineering,21 Mar 2024"
)
VIKRAM = "Vikram,Shah,https://example.com/in/vikram,,Globex,Designer,2 Jan 2023"


class TestUploading:
    async def test_an_export_is_stored_and_counted(self, api, owner) -> None:
        response = _upload(api, owner, _export(ASHA, VIKRAM))

        assert response.status_code == 201, response.text
        assert response.json()["stored"] == 2
        assert api.get("/api/v1/connections", headers=owner).json()["connections"] == 2

    async def test_an_address_in_the_file_reaches_no_row(self, api, owner, db, org) -> None:
        """The guard that matters most, asserted against the database rather than the parser.

        A LinkedIn export carries `Email Address`; keeping it would turn a referral finder into
        somebody's whole address book, belonging to people who agreed to connect with *them*.
        """
        _upload(api, owner, _export(ASHA))

        rows = (await db.scalars(select(Connection).where(Connection.org_id == org.id))).all()

        assert len(rows) == 1
        assert "asha@example.com" not in str(
            {column.name: getattr(rows[0], column.name) for column in rows[0].__table__.columns}
        )

    async def test_a_second_export_updates_rather_than_doubling(self, api, owner) -> None:
        """A re-export is mostly the same people. Inserting would show each of them twice in a
        referral shortlist, which looks wrong rather than long."""
        _upload(api, owner, _export(ASHA, VIKRAM))

        again = _upload(
            api,
            owner,
            _export(
                "Asha,Rao,https://example.com/in/asha,,Acme Inc,VP Engineering,21 Mar 2024",
                VIKRAM,
            ),
        )

        assert again.json() == {
            **again.json(),
            "stored": 0,
            "updated": 2,
        }
        assert api.get("/api/v1/connections", headers=owner).json()["connections"] == 2

    async def test_a_newer_export_wins_on_a_changed_job(self, api, owner, db, org) -> None:
        """A note quoting a job somebody left two years ago is worse than one with no title."""
        _upload(api, owner, _export(ASHA))
        _upload(
            api,
            owner,
            _export("Asha,Rao,https://example.com/in/asha,,Acme Inc,VP Engineering,21 Mar 2024"),
        )

        row = await db.scalar(select(Connection).where(Connection.org_id == org.id))

        assert row is not None
        assert row.position == "VP Engineering"

    async def test_the_wrong_file_is_refused_with_a_sentence(self, api, owner) -> None:
        response = _upload(api, owner, b"name,amount\nInvoice,100\n")

        assert response.status_code == 422
        assert "Connections.csv" in response.json()["detail"]

    async def test_the_response_says_what_is_discarded(self, api, owner) -> None:
        body = _upload(api, owner, _export(ASHA)).json()

        assert "discarded" in body["note"]


class TestFinding:
    async def test_a_connection_at_the_company_is_proposed_with_a_reason(self, api, owner) -> None:
        _upload(api, owner, _export(ASHA, VIKRAM))

        body = api.get("/api/v1/referrals", params={"company": "Acme"}, headers=owner).json()

        assert [one["full_name"] for one in body["referrals"]] == ["Asha Rao"]
        assert body["referrals"][0]["why"] == "their company name matches the posting's exactly"
        # Shown for the user to open. Nothing fetches it.
        assert body["referrals"][0]["profile_url"] == "https://example.com/in/asha"

    async def test_the_company_suffix_does_not_decide_the_match(self, api, owner) -> None:
        """The export says what the connection typed; the posting says something else."""
        _upload(api, owner, _export(ASHA))

        for written in ("Acme", "Acme Inc", "ACME, Inc.", "Acme Pvt Ltd"):
            body = api.get("/api/v1/referrals", params={"company": written}, headers=owner).json()
            assert len(body["referrals"]) == 1, written

    async def test_the_note_names_the_person_and_their_own_job(self, api, owner) -> None:
        """Every value is from a row. Nothing here is generated, because this message goes to
        somebody who knows the user well enough to notice an invented line."""
        _upload(api, owner, _export(ASHA))

        note = api.get("/api/v1/referrals", params={"company": "Acme"}, headers=owner).json()[
            "referrals"
        ][0]["note"]

        assert note.startswith("Hi Asha,")
        assert "Head of Engineering" in note
        assert "Acme" in note

    async def test_the_note_cites_only_a_confirmed_fact(self, api, owner, db, org) -> None:
        """F6's rule, where it bites hardest.

        An unconfirmed fact is a claim the person has not agreed to make about themselves, and
        this one would be made to a friend who could ask about it.
        """
        _upload(api, owner, _export(ASHA))
        db.add(
            ProfileFact(
                org_id=org.id,
                fact_id="pp1",
                kind="skill",
                statement="PROPOSED AND NOT CONFIRMED",
                source="github",
            )
        )
        await db.commit()

        note = api.get("/api/v1/referrals", params={"company": "Acme"}, headers=owner).json()[
            "referrals"
        ][0]["note"]

        assert "PROPOSED AND NOT CONFIRMED" not in note

    async def test_a_posting_supplies_the_company_and_the_link(self, api, owner, db, org) -> None:
        _upload(api, owner, _export(ASHA))
        post = JobPost(
            org_id=org.id,
            source="pasted",
            external_id=uuid.uuid4().hex,
            company_domain="acme.example.com",
            title="Senior Engineer",
            apply_url="https://acme.example.com/jobs/1",
        )
        db.add(post)
        await db.commit()

        body = api.get(
            "/api/v1/referrals", params={"job_post_id": str(post.id)}, headers=owner
        ).json()

        assert len(body["referrals"]) == 1
        note = body["referrals"][0]["note"]
        assert "Senior Engineer" in note
        assert "https://acme.example.com/jobs/1" in note

    async def test_a_posting_from_another_workspace_resolves_to_nothing(self, api, owner) -> None:
        _upload(api, owner, _export(ASHA))

        body = api.get(
            "/api/v1/referrals", params={"job_post_id": str(uuid.uuid4())}, headers=owner
        ).json()

        assert body["referrals"] == []

    async def test_asking_for_nothing_is_refused(self, api, owner) -> None:
        response = api.get("/api/v1/referrals", headers=owner)

        assert response.status_code == 422

    async def test_nobody_at_the_company_is_an_empty_list_rather_than_an_error(
        self, api, owner
    ) -> None:
        _upload(api, owner, _export(ASHA))

        body = api.get("/api/v1/referrals", params={"company": "Initech"}, headers=owner).json()

        assert body["referrals"] == []


class TestIsolationAndDeletion:
    async def test_another_workspace_sees_none_of_it(self, api, owner, db, keypair) -> None:
        _upload(api, owner, _export(ASHA, VIKRAM))
        private, _ = keypair
        other = Org(name="Separate")
        db.add(other)
        await db.flush()
        subject = f"other-{uuid.uuid4()}"
        db.add(Member(org_id=other.id, user_id=subject, email="o@example.com", role="owner"))
        await db.commit()
        stranger = {"Authorization": f"Bearer {token_for(private, subject=subject)}"}

        assert api.get("/api/v1/connections", headers=stranger).json()["connections"] == 0
        assert (
            api.get("/api/v1/referrals", params={"company": "Acme"}, headers=stranger).json()[
                "referrals"
            ]
            == []
        )

    async def test_deleting_really_deletes(self, api, owner, db, org) -> None:
        """A real delete, not a flag.

        These are thousands of people who never agreed to be in our database -- they agreed to
        connect with the user. And unlike a suppression, forgetting one cannot cause anybody to
        be contacted, so there is nothing worth keeping a tombstone for.
        """
        _upload(api, owner, _export(ASHA, VIKRAM))

        response = api.delete("/api/v1/connections", headers=owner)

        assert response.status_code == 200
        assert response.json()["removed"] == 2
        rows = (await db.scalars(select(Connection).where(Connection.org_id == org.id))).all()
        assert rows == []

    async def test_a_member_cannot_delete_the_workspaces_connections(
        self, api, owner, member
    ) -> None:
        _upload(api, owner, _export(ASHA))

        assert api.delete("/api/v1/connections", headers=member).status_code == 403
