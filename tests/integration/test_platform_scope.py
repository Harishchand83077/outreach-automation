"""The scope above orgs, and the line it must never cross.

`SIGNUP_MODE=open` means every signup owns its own workspace (ADR-017), which is the whole
product -- so "the owner can see everything" cannot mean one shared org. It means a **third
scope**: somebody who can see *that* the workspaces exist and whether they are healthy, and who
can never read what is inside them.

**The test that matters most in this file is `TestTheBoundaryHolds`.** Everything else here is
counting. That class asserts the thing a platform view is always one careless `select` away from
breaking: a lead's address, a company's name and a draft's text must not appear in the response,
whoever is asking. A screen that answers "is this workspace healthy?" by shipping its contents is
not an aggregate view, it is a tenancy failure with a nicer heading.
"""

# Importing a fixture is how pytest shares one between modules, and taking it as a parameter
# then "redefines" the imported name -- which is exactly what is supposed to happen.
# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest

from app.db.models import (
    Campaign,
    Draft,
    LlmUsage,
    Mailbox,
    Member,
    Message,
    Org,
    PlatformOperator,
)

from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

OVERVIEW = "/api/v1/platform/overview"


async def _operator(db, keypair, *, email: str = "sanket@example.com", member_of=None):
    """Somebody with the platform flag. Granted by an INSERT, which is the only way there is."""
    private, _ = keypair
    subject = f"operator-{uuid.uuid4()}"
    db.add(PlatformOperator(user_id=subject, email=email, note="the owner of the deployment"))
    if member_of is not None:
        db.add(Member(org_id=member_of.id, user_id=subject, email=email, role="owner"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


async def _workspace(db, name: str):
    org = Org(name=name)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


class TestOnlyAnOperatorGetsIn:
    async def test_an_org_owner_is_refused(self, api, owner):
        """The strongest role *inside* a workspace is not this.

        An owner may turn on live sending for their own org. That is not evidence they may read
        the list of everybody else's.
        """
        assert api.get(OVERVIEW, headers=owner).status_code == 403

    async def test_a_member_is_refused(self, api, member):
        assert api.get(OVERVIEW, headers=member).status_code == 403

    async def test_an_unauthenticated_caller_is_refused(self, api):
        assert api.get(OVERVIEW).status_code == 401

    async def test_an_operator_needs_no_membership_anywhere(self, api, db, keypair):
        """The point of the separate dependency, stated as a test.

        The operator identity is resolved from its own table and never passes through
        `current_user`, so it cannot be provisioned a workspace on the way in and cannot be
        refused for not having one. If this ever starts 403ing, something has been rewired to
        resolve the operator through `members` -- which would make the flag an org role again.
        """
        headers = await _operator(db, keypair)

        assert api.get(OVERVIEW, headers=headers).status_code == 200

    async def test_the_flag_does_not_make_them_an_admin_of_any_org(self, api, db, keypair):
        """Read-only *above* orgs, not a key *into* one.

        A superuser that could also use the ordinary admin routes would be a second way to reach
        a workspace's own settings -- including, eventually, its send mode.
        """
        headers = await _operator(db, keypair)

        assert api.get("/api/v1/admin/overview", headers=headers).status_code == 403
        assert api.get("/api/v1/team", headers=headers).status_code == 403
        assert api.get("/api/v1/campaigns", headers=headers).status_code == 403


class TestWhatItShows:
    async def test_every_workspace_is_listed(self, api, db, keypair, org):
        other = await _workspace(db, f"Second {uuid.uuid4().hex[:6]}")
        headers = await _operator(db, keypair)

        body = api.get(OVERVIEW, headers=headers).json()

        ids = {row["id"] for row in body["workspaces"]}
        assert str(org.id) in ids
        assert str(other.id) in ids
        assert body["totals"]["workspaces"] >= 2

    async def test_a_workspace_carries_its_own_counts(
        self, api, db, keypair, owner, campaign, lead_factory
    ):
        await lead_factory()
        headers = await _operator(db, keypair)

        body = api.get(OVERVIEW, headers=headers).json()
        row = next(r for r in body["workspaces"] if r["id"] == str(campaign.org_id))

        assert row["campaigns"] >= 1
        assert row["leads"] >= 1
        assert row["members"] >= 1

    async def test_a_sent_message_is_counted(self, api, db, keypair, lead_factory):
        """**This test exists because the first version of the query was wrong.**

        It filtered `direction == "outbound"`, and the column holds `'out'` --
        `ck_messages_direction` is the authority. So `emails_sent` was zero for every workspace,
        for ever, which is exactly what a deployment that has sent nothing looks like. Nothing
        else in this file would have noticed: every other assertion here is `>= 1` on a count
        that was right.

        The same shape as most of this project's defects -- a correct table and a correct caller
        with nothing checking that they agreed -- so the check is now written down.
        """
        lead = await lead_factory()
        db.add(
            Message(
                org_id=lead.org_id,
                lead_id=lead.id,
                direction="out",
                status="sent",
                idempotency_key=f"platform-test-{uuid.uuid4()}",
            )
        )
        await db.commit()
        headers = await _operator(db, keypair)

        body = api.get(OVERVIEW, headers=headers).json()
        row = next(r for r in body["workspaces"] if r["id"] == str(lead.org_id))

        assert row["emails_sent"] >= 1
        assert body["totals"]["emails_sent"] >= 1

    async def test_a_message_still_sending_is_not_counted_as_sent(
        self, api, db, keypair, lead_factory
    ):
        """ADR-002's rule, kept here too: `sending` is not `sent` and must not be reported as it.

        A message mid-flight may still fail, and a platform screen that counted it would report
        volume this deployment has not actually put on anybody's reputation.
        """
        lead = await lead_factory()
        before = api.get(OVERVIEW, headers=await _operator(db, keypair)).json()
        start = next(r for r in before["workspaces"] if r["id"] == str(lead.org_id))["emails_sent"]

        db.add(
            Message(
                org_id=lead.org_id,
                lead_id=lead.id,
                direction="out",
                status="sending",
                idempotency_key=f"platform-inflight-{uuid.uuid4()}",
            )
        )
        await db.commit()

        after = api.get(OVERVIEW, headers=await _operator(db, keypair)).json()
        row = next(r for r in after["workspaces"] if r["id"] == str(lead.org_id))

        assert row["emails_sent"] == start

    async def test_mailbox_health_is_counted_not_named(self, api, db, keypair, org):
        """How many connections work, never which addresses they are.

        Connection health is the one operational question that genuinely needs the platform view
        -- a workspace whose mailbox lapsed has silently stopped sending. The count answers it;
        the address would be somebody else's user's identity.
        """
        address = f"private-{uuid.uuid4().hex[:8]}@example.com"
        db.add(Mailbox(org_id=org.id, from_name="Asha", from_email=address, reply_to=address))
        await db.commit()
        headers = await _operator(db, keypair)

        response = api.get(OVERVIEW, headers=headers)
        row = next(r for r in response.json()["workspaces"] if r["id"] == str(org.id))

        assert row["mailboxes"] >= 1
        assert address not in response.text

    async def test_only_failed_llm_calls_count_as_failures(self, api, db, keypair, org):
        """**Also written because the first version of the query was wrong.**

        It compared the outcome against `"ok"`, which is not a value `Outcome` has -- the
        success value is `"success"` -- so every successful call was counted as a failure and
        the screen would have reported a model chain in permanent trouble while it worked
        perfectly. Second guessed literal in the same function, which is why the query now reads
        the enum instead of a copy of it.
        """
        headers = await _operator(db, keypair)
        before = api.get(OVERVIEW, headers=headers).json()["usage"]

        db.add(LlmUsage(org_id=org.id, entry_name="groq-test", task="draft", outcome="success"))
        db.add(LlmUsage(org_id=org.id, entry_name="groq-test", task="draft", outcome="timeout"))
        await db.commit()

        after = api.get(OVERVIEW, headers=headers).json()["usage"]

        assert after["llm_calls_24h"] == before["llm_calls_24h"] + 2
        assert after["llm_failures_24h"] == before["llm_failures_24h"] + 1

    async def test_the_deployment_limits_are_reported(self, api, db, keypair):
        headers = await _operator(db, keypair)

        body = api.get(OVERVIEW, headers=headers).json()

        assert "signup_mode" in body["deployment"]
        assert "max_orgs" in body["deployment"]

    async def test_operational_health_is_reported(self, api, db, keypair):
        """Dead jobs and unacknowledged alerts: the two numbers that mean somebody must look."""
        headers = await _operator(db, keypair)

        body = api.get(OVERVIEW, headers=headers).json()

        assert "dead_jobs" in body["health"]
        assert "alerts_open" in body["health"]


class TestTheBoundaryHolds:
    """**The important class.** Aggregates only, asserted against the real strings.

    Checked against `response.text` rather than by walking the shape, because the failure this
    guards against is a field somebody adds later in a place nobody thought to look. A test that
    inspects only the keys it knows about cannot see a new one.
    """

    async def test_no_lead_address_company_or_draft_text_is_returned(
        self, api, db, keypair, campaign, lead_factory
    ):
        secret_company = f"Acme-{uuid.uuid4().hex[:8]}"
        secret_body = f"body-{uuid.uuid4().hex[:8]}"
        secret_subject = f"subject-{uuid.uuid4().hex[:8]}"
        lead = await lead_factory(
            email=f"founder-{uuid.uuid4().hex[:8]}@example.com", company_name=secret_company
        )
        db.add(
            Draft(
                org_id=lead.org_id,
                lead_id=lead.id,
                kind="initial",
                version=1,
                subject=secret_subject,
                body=secret_body,
                status="pending_review",
            )
        )
        await db.commit()
        headers = await _operator(db, keypair)

        text = api.get(OVERVIEW, headers=headers).text

        assert lead.email_norm not in text
        assert secret_company not in text
        assert secret_subject not in text
        assert secret_body not in text

    async def test_no_campaign_name_or_persona_is_returned(self, api, db, keypair, org):
        """A campaign name is the operator's own commercial information.

        "Q4 push at the three accounts we lost last quarter" is a sentence somebody wrote for
        their own eyes. The platform view needs the *number* of campaigns, never their names.
        """
        secret_name = f"Project-{uuid.uuid4().hex[:8]}"
        db.add(
            Campaign(
                org_id=org.id,
                name=secret_name,
                persona={"sender_name": "Asha", "proof_points": []},
            )
        )
        await db.commit()
        headers = await _operator(db, keypair)

        assert secret_name not in api.get(OVERVIEW, headers=headers).text

    async def test_no_credential_or_provider_message_is_returned(self, api, db, keypair, org):
        """`invalid_reason` can quote a provider's reply, here as everywhere."""
        headers = await _operator(db, keypair)

        text = api.get(OVERVIEW, headers=headers).text

        assert "invalid_reason" not in text
        assert "secret" not in text


class TestMeReportsTheFlag:
    async def test_an_ordinary_owner_is_not_an_operator(self, api, owner):
        assert api.get("/api/v1/me", headers=owner).json()["platform_operator"] is False

    async def test_an_operator_who_is_also_an_owner_is_told_so(self, api, db, keypair, org):
        """The dashboard needs this to decide whether to draw the nav item at all.

        It is reported on `/me` rather than discovered by calling the platform route and seeing
        whether it 403s, because a UI that learns what it may do from failed requests logs an
        error on every load for every ordinary user.
        """
        headers = await _operator(db, keypair, member_of=org)

        assert api.get("/api/v1/me", headers=headers).json()["platform_operator"] is True
