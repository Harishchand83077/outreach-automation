"""Resume versions, their PDFs and the application tracker, end to end (F20, ADR-043).

The three acceptance criteria for this slice, each with a test named after it:

1. a tailored resume with a number or skill not in confirmed facts is **rejected**;
2. an application points at an **immutable** version;
3. the extension **cannot** set `applied`.

The immutability one is the test that could not be written anywhere else: the guarantee is a
database trigger, so only a real `UPDATE` against real Postgres can show it holding.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from app.db.models import Application, Job, JobPost, Member, Org, ProfileFact, ResumeVersion
from app.db.queue import JobKind, enqueue
from app.db.repo import OrgContext
from app.services.candidate import profile
from app.services.resume import store
from app.services.resume.ops import parse_ops
from app.worker import handlers

from ..unit.test_resume_tailor import FakeRouter
from .test_api_settings import api, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def facts(db, org):
    """Two confirmed facts and one proposal, which is the shape every rule here turns on."""
    ctx = OrgContext(org_id=org.id)
    await profile.add_fact(
        db,
        ctx,
        kind="role",
        statement="Built the payments API at Acme in Python, handling 2000 requests a minute.",
    )
    await profile.add_fact(db, ctx, kind="skill", statement="Comfortable with Postgres and Docker.")
    await profile.add_fact(db, ctx, kind="role", statement="Led a team of four.", source="resume")
    await db.commit()
    rows = await profile.list_facts(db, ctx)
    return {row.statement: row for row in rows}


@pytest_asyncio.fixture(loop_scope="session")
async def posting(db, org):
    row = JobPost(
        org_id=org.id,
        source="pasted",
        external_id=f"ext-{uuid.uuid4()}",
        title="Backend engineer",
        apply_url="https://example.com/apply",
        body_text="We want Python and Postgres.",
    )
    db.add(row)
    await db.commit()
    return row


def _include(fact_id: str, text_value: str | None = None, section: str = "Experience") -> dict:
    row: dict = {"kind": "include", "fact_id": fact_id, "section": section}
    if text_value is not None:
        row["text"] = text_value
    return row


class TestCreatingAVersion:
    async def test_a_version_is_assembled_from_confirmed_facts(self, api, owner, facts) -> None:
        response = api.post(
            "/api/v1/resume-versions",
            json={
                "label": "Backend engineer at Acme",
                "header": "Asha Rao\nasha@example.com",
                "edit_ops": [_include("pp1"), _include("pp2", section="Skills")],
            },
            headers=owner,
        )

        assert response.status_code == 201, response.text
        version = response.json()["version"]
        assert version["fact_ids"] == ["pp1", "pp2"]
        assert "Asha Rao" in version["rendered_text"]
        assert "Experience" in version["rendered_text"]
        assert version["created_by"].startswith("user:")

    async def test_a_resume_with_an_invented_number_is_rejected(self, api, owner, facts) -> None:
        """Acceptance criterion 1. The refusal names the number, so it is one edit to fix."""
        response = api.post(
            "/api/v1/resume-versions",
            json={
                "label": "Backend engineer",
                "edit_ops": [_include("pp1", "Built the payments API, cutting latency by 42%.")],
            },
            headers=owner,
        )

        assert response.status_code == 422, response.text
        refused = response.json()["detail"]["refused"]
        assert any("42" in message for message in refused)

    async def test_a_resume_with_an_invented_skill_is_rejected(self, api, owner, facts) -> None:
        response = api.post(
            "/api/v1/resume-versions",
            json={
                "label": "Backend engineer",
                "edit_ops": [_include("pp2", "Postgres, Docker and Kubernetes.")],
            },
            headers=owner,
        )

        assert response.status_code == 422
        assert any(
            "kubernetes" in message.lower() for message in response.json()["detail"]["refused"]
        )

    async def test_an_unconfirmed_fact_is_rejected_by_name(self, api, owner, facts) -> None:
        response = api.post(
            "/api/v1/resume-versions",
            json={"label": "Lead", "edit_ops": [_include("pp3")]},
            headers=owner,
        )

        assert response.status_code == 422
        assert any("proposal" in message for message in response.json()["detail"]["refused"])

    async def test_a_resume_with_no_facts_in_it_is_rejected(self, api, owner, facts) -> None:
        """An empty document is not a resume, and storing one would put a version in the list
        that cannot be sent anywhere."""
        response = api.post(
            "/api/v1/resume-versions",
            json={
                "label": "Empty",
                "edit_ops": [{"kind": "drop", "fact_id": "pp1", "reason": "not relevant"}],
            },
            headers=owner,
        )

        assert response.status_code == 422

    async def test_another_workspaces_posting_is_not_found(self, api, owner, db, facts) -> None:
        other = Org(name="Another candidate")
        db.add(other)
        await db.commit()
        theirs = JobPost(
            org_id=other.id,
            source="pasted",
            external_id=f"ext-{uuid.uuid4()}",
            title="Theirs",
            apply_url="https://example.com/theirs",
        )
        db.add(theirs)
        await db.commit()

        response = api.post(
            "/api/v1/resume-versions",
            json={
                "label": "Backend engineer",
                "edit_ops": [_include("pp1")],
                "job_post_id": str(theirs.id),
            },
            headers=owner,
        )

        assert response.status_code == 422


class TestAVersionIsImmutable:
    async def test_the_database_refuses_an_update(self, db, org, facts) -> None:
        """Acceptance criterion 2, in its load-bearing half. The guarantee is a trigger, so a
        test that only checked there is no update route would prove nothing about the thing
        somebody will eventually reach for: a hand-written UPDATE against production.
        """
        created = await store.create_version(
            db,
            OrgContext(org_id=org.id),
            ops=parse_ops([_include("pp1")]),
            label="Immutable",
        )
        await db.commit()

        with pytest.raises(Exception, match="immutable"):
            await db.execute(
                update(ResumeVersion)
                .where(ResumeVersion.id == created.version.id)
                .values(rendered_text="- Five years of Kubernetes.")
            )
        await db.rollback()

    async def test_there_is_no_route_that_edits_or_deletes_one(self, api, owner, db, org, facts):
        created = await store.create_version(
            db,
            OrgContext(org_id=org.id),
            ops=parse_ops([_include("pp1")]),
            label="No edit route",
        )
        await db.commit()

        assert (
            api.patch(
                f"/api/v1/resume-versions/{created.version.id}", json={"label": "x"}, headers=owner
            ).status_code
            == 405
        )
        assert (
            api.delete(f"/api/v1/resume-versions/{created.version.id}", headers=owner).status_code
            == 405
        )

    async def test_a_lawful_erasure_is_still_possible(self, db, org, facts) -> None:
        """The escape hatch, so an accident is loud and a data-subject request is possible --
        exactly the bargain `app.events` already makes."""
        created = await store.create_version(
            db,
            OrgContext(org_id=org.id),
            ops=parse_ops([_include("pp1")]),
            label="Erasable",
        )
        await db.commit()

        await db.execute(text("SET LOCAL app.allow_resume_version_delete = 'on'"))
        await db.delete(created.version)
        await db.commit()

        assert await db.get(ResumeVersion, created.version.id) is None


class TestTheTracker:
    async def test_an_application_points_at_a_version(self, api, owner, posting, facts) -> None:
        version = api.post(
            "/api/v1/resume-versions",
            json={"label": "For Acme", "edit_ops": [_include("pp1")]},
            headers=owner,
        ).json()["version"]

        response = api.post(
            "/api/v1/applications",
            json={"job_post_id": str(posting.id), "resume_version_id": version["id"]},
            headers=owner,
        )

        assert response.status_code == 201, response.text
        row = response.json()["application"]
        assert row["resume_version_id"] == version["id"]
        # Created `planned`: a row appearing is not a claim that anybody applied.
        assert row["status"] == "planned"
        assert row["applied_at"] is None

    async def test_marking_it_applied_stamps_the_date_and_a_follow_up(
        self, api, owner, posting, facts
    ) -> None:
        created = api.post(
            "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner
        ).json()["application"]

        response = api.patch(
            f"/api/v1/applications/{created['id']}", json={"new_status": "applied"}, headers=owner
        )

        assert response.status_code == 200, response.text
        row = response.json()["application"]
        assert row["applied_at"] is not None
        assert row["follow_up_due"] is not None

    async def test_an_applied_row_cannot_go_back_to_planned(
        self, api, owner, posting, facts
    ) -> None:
        """The one thing a tracker must not do is forget that somebody sent a document to a
        company."""
        created = api.post(
            "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner
        ).json()["application"]
        api.patch(
            f"/api/v1/applications/{created['id']}", json={"new_status": "applied"}, headers=owner
        )

        response = api.patch(
            f"/api/v1/applications/{created['id']}", json={"new_status": "planned"}, headers=owner
        )

        assert response.status_code == 422
        assert "already recorded as sent" in response.text

    async def test_an_unknown_status_is_refused(self, api, owner, posting, facts) -> None:
        created = api.post(
            "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner
        ).json()["application"]

        response = api.patch(
            f"/api/v1/applications/{created['id']}", json={"new_status": "hired"}, headers=owner
        )

        assert response.status_code == 422

    async def test_marking_it_applied_writes_an_audit_event(
        self, api, owner, db, org, posting, facts
    ) -> None:
        created = api.post(
            "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner
        ).json()["application"]
        api.patch(
            f"/api/v1/applications/{created['id']}", json={"new_status": "applied"}, headers=owner
        )

        rows = await db.execute(
            text(
                "SELECT count(*) FROM app.events "
                "WHERE org_id = :org AND type = :type AND data->>'application_id' = :id"
            ),
            {"org": org.id, "type": store.APPLIED_EVENT, "id": created["id"]},
        )

        assert rows.scalar_one() == 1

    async def test_two_applications_to_one_posting_are_one_row(
        self, api, owner, posting, facts
    ) -> None:
        first = api.post(
            "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner
        ).json()["application"]
        second = api.post(
            "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner
        ).json()["application"]

        assert first["id"] == second["id"]


class TestTheExtensionCannotSetApplied:
    async def test_an_extension_token_is_refused_at_this_door(
        self, api, owner, db, org, posting
    ) -> None:
        """Acceptance criterion 3. ADR-039's three doors, asserted on the routes that matter:
        a credential sitting in a browser on a laptop cannot claim the person applied."""
        from app.services import api_tokens

        subject = f"ext-user-{uuid.uuid4()}"
        db.add(Member(org_id=org.id, user_id=subject, email=f"{subject}@example.com", role="admin"))
        minted = await api_tokens.create_token(
            db,
            org_id=org.id,
            user_id=subject,
            name="Extension",
            scopes=[api_tokens.EXT_SUGGEST_SCOPE],
        )
        await db.commit()
        headers = {"Authorization": f"Bearer {minted.secret}"}

        assert api.get("/api/v1/applications", headers=headers).status_code == 401
        assert (
            api.post(
                "/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=headers
            ).status_code
            == 401
        )
        assert api.get("/api/v1/resume-versions", headers=headers).status_code == 401


class TestQueuedWork:
    async def test_tailoring_is_queued_rather_than_done_in_the_request(
        self, api, owner, db, org, posting, facts
    ) -> None:
        response = api.post(f"/api/v1/job-posts/{posting.id}/tailor", headers=owner)

        assert response.status_code == 202, response.text
        assert response.json()["queued"] is True
        rows = list(
            (
                await db.scalars(
                    select(Job).where(Job.org_id == org.id, Job.kind == JobKind.TAILOR_RESUME)
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].payload["job_post_id"] == str(posting.id)

    async def test_pressing_the_button_twice_queues_one_job(
        self, api, owner, db, org, posting, facts
    ) -> None:
        api.post(f"/api/v1/job-posts/{posting.id}/tailor", headers=owner)
        second = api.post(f"/api/v1/job-posts/{posting.id}/tailor", headers=owner)

        assert second.status_code == 202
        assert second.json()["queued"] is False

    async def test_a_pdf_that_has_not_been_rendered_is_a_sentence_not_an_empty_file(
        self, api, owner, db, org, facts
    ) -> None:
        created = await store.create_version(
            db,
            OrgContext(org_id=org.id),
            ops=parse_ops([_include("pp1")]),
            label="Unrendered",
        )
        await db.commit()

        response = api.get(f"/api/v1/resume-versions/{created.version.id}/pdf", headers=owner)

        assert response.status_code == 404
        assert "has not been rendered" in response.text

    async def test_a_rendered_pdf_comes_back_as_a_pdf(self, api, owner, db, org, facts) -> None:
        ctx = OrgContext(org_id=org.id)
        created = await store.create_version(
            db, ctx, ops=parse_ops([_include("pp1")]), label="Rendered"
        )
        from app.services.resume.pdf import render_pdf

        await store.store_render(
            db,
            ctx,
            version_id=created.version.id,
            content=render_pdf(created.version.rendered_text),
        )
        await db.commit()

        response = api.get(f"/api/v1/resume-versions/{created.version.id}/pdf", headers=owner)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")

    async def test_another_workspaces_version_is_not_found(
        self, api, owner, db, org, facts
    ) -> None:
        other = Org(name="Another candidate again")
        db.add(other)
        await db.commit()
        other_ctx = OrgContext(org_id=other.id)
        db.add(
            ProfileFact(
                org_id=other.id,
                fact_id="pp1",
                kind="skill",
                statement="Their own fact.",
                source="user",
                confirmed_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()
        theirs = await store.create_version(
            db, other_ctx, ops=parse_ops([_include("pp1")]), label="Theirs"
        )
        await db.commit()

        assert (
            api.get(f"/api/v1/resume-versions/{theirs.version.id}", headers=owner).status_code
            == 404
        )
        assert (
            api.post(
                f"/api/v1/resume-versions/{theirs.version.id}/render", headers=owner
            ).status_code
            == 404
        )


class TestWhoMayDoWhat:
    async def test_a_member_may_keep_their_own_tracker(self, api, member, posting, facts) -> None:
        """Not admin-gated. A workspace is one candidate's job hunt; a resume and an application
        are their own work, not settings that govern somebody else's."""
        response = api.get("/api/v1/applications", headers=member)

        assert response.status_code == 200
        assert "statuses" in response.json()

    async def test_the_tracker_lists_only_this_workspaces_rows(
        self, api, owner, db, org, posting, facts
    ) -> None:
        api.post("/api/v1/applications", json={"job_post_id": str(posting.id)}, headers=owner)
        other = Org(name="Yet another candidate")
        db.add(other)
        await db.commit()
        theirs_post = JobPost(
            org_id=other.id,
            source="pasted",
            external_id=f"ext-{uuid.uuid4()}",
            title="Theirs",
            apply_url="https://example.com/theirs",
        )
        db.add(theirs_post)
        await db.commit()
        db.add(
            Application(
                org_id=other.id,
                job_post_id=theirs_post.id,
                status="applied",
                applied_at=datetime.now(tz=UTC),
            )
        )
        await db.commit()

        response = api.get("/api/v1/applications", headers=owner)

        assert response.status_code == 200
        ours = {row["job_post_id"] for row in response.json()["applications"]}
        assert str(theirs_post.id) not in ours


class _StubContext:
    """Just `router_for`, which is all either handler touches.

    A real `WorkerContext` would build a real `LLMRouter` from the configured chain, which in a
    test session means the module-level settings that read `.env` at import time -- the gap the
    F9 slice found the hard way. A stub keeps this file's model calls fake and its prompts
    inspectable.
    """

    def __init__(self, *answers: list[dict]) -> None:
        self.router = FakeRouter(*answers)

    def router_for(self, session, org_id):
        return self.router


class TestTheWorkerHandlers:
    async def test_a_tailoring_job_writes_a_version_and_an_event(
        self, db, org, posting, facts
    ) -> None:
        job = await enqueue(
            db,
            org_id=org.id,
            kind=JobKind.TAILOR_RESUME,
            payload={"job_post_id": str(posting.id)},
            dedupe_key=f"tailor-handler:{uuid.uuid4()}",
        )
        await db.commit()
        assert job is not None

        ctx = _StubContext([{"kind": "include", "fact_id": "pp1", "section": "Experience"}])
        await handlers.handle_tailor_resume(ctx, db, job)  # type: ignore[arg-type]
        await db.commit()

        version = await db.scalar(
            select(ResumeVersion).where(
                ResumeVersion.org_id == org.id, ResumeVersion.job_post_id == posting.id
            )
        )
        assert version is not None
        assert version.created_by == "model"
        assert version.fact_ids == ["pp1"]
        kinds = await db.execute(
            text("SELECT type FROM app.events WHERE org_id = :org AND type LIKE 'resume.%'"),
            {"org": org.id},
        )
        assert "resume.tailored" in {row[0] for row in kinds}

    async def test_a_refused_proposal_records_the_reasons_and_does_not_fail_the_job(
        self, db, org, posting, facts
    ) -> None:
        """Nothing went wrong with the work: the model proposed something the facts do not
        support and the validator did its job. A dead job is where somebody looks for a fault."""
        job = await enqueue(
            db,
            org_id=org.id,
            kind=JobKind.TAILOR_RESUME,
            payload={"job_post_id": str(posting.id)},
            dedupe_key=f"tailor-refused:{uuid.uuid4()}",
        )
        await db.commit()
        assert job is not None

        ctx = _StubContext(
            [{"fact_id": "pp1", "text": "Cut latency by 42% on Kubernetes."}],
            [{"fact_id": "pp1", "text": "Cut latency by 42% on Kubernetes."}],
        )
        await handlers.handle_tailor_resume(ctx, db, job)  # type: ignore[arg-type]
        await db.commit()

        rows = await db.execute(
            text(
                "SELECT data FROM app.events WHERE org_id = :org AND type = 'resume.tailor_refused'"
            ),
            {"org": org.id},
        )
        data = rows.scalars().all()
        assert data
        assert any("42" in " ".join(one["problems"]) for one in data)

    async def test_rendering_stores_a_pdf_against_the_version(self, db, org, facts) -> None:
        ctx_org = OrgContext(org_id=org.id)
        created = await store.create_version(
            db, ctx_org, ops=parse_ops([_include("pp1")]), label="To render"
        )
        job = await enqueue(
            db,
            org_id=org.id,
            kind=JobKind.RENDER_RESUME_PDF,
            payload={"resume_version_id": str(created.version.id)},
            dedupe_key=f"render-handler:{uuid.uuid4()}",
        )
        await db.commit()
        assert job is not None

        await handlers.handle_render_resume_pdf(_StubContext(), db, job)  # type: ignore[arg-type]
        await db.commit()

        render = await store.get_render(db, ctx_org, version_id=created.version.id)
        assert render is not None
        assert render.content.startswith(b"%PDF")
        assert render.size_bytes == len(render.content)

    async def test_a_job_naming_a_version_in_another_org_is_a_permanent_failure(
        self, db, org, facts
    ) -> None:
        """Not a retry. A version in another workspace will not appear on the fifth attempt."""
        job = await enqueue(
            db,
            org_id=org.id,
            kind=JobKind.RENDER_RESUME_PDF,
            payload={"resume_version_id": str(uuid.uuid4())},
            dedupe_key=f"render-missing:{uuid.uuid4()}",
        )
        await db.commit()
        assert job is not None

        with pytest.raises(handlers.PermanentFailure):
            await handlers.handle_render_resume_pdf(_StubContext(), db, job)  # type: ignore[arg-type]
