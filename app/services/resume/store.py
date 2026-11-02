"""Writing resume versions, their renders and the application tracker (F20, ADR-043).

**`create_version` is the only way a resume version exists**, and it validates twice: the
operations against the workspace's confirmed facts, and then the rendered text line by line
against the operations. The second is the one that matters when somebody later adds another
producer, because it checks the artefact rather than the plan -- and this project's dominant
defect is a correct producer and a correct consumer with nothing checking that they agreed.

The row is immutable by a database trigger, so there is no `update_version` here. A change makes
a new row, and an `applications` row keeps pointing at the one that was actually sent.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    APPLICATION_STATUSES,
    Application,
    JobPost,
    ProfileFact,
    ResumeRender,
    ResumeVersion,
)
from app.db.repo import OrgContext
from app.db.state import record_event
from app.services.resume import validate
from app.services.resume.ops import EditOp, as_json
from app.services.resume.validate import KnownFact, Report

#: A follow-up on a sent application, when the person does not say otherwise. Long enough that
#: a recruiter has had the week they usually take, short enough to still be the same posting.
FOLLOW_UP_DAYS = 7

APPLIED_EVENT = "application.applied"


class ResumeRefused(Exception):
    """The proposal asserts something the facts do not support.

    Carries every problem rather than the first, because a person fixing a resume wants the
    whole list in one pass -- and a model given feedback fixes what it is told about.
    """

    def __init__(self, report: Report) -> None:
        self.report = report
        super().__init__("; ".join(report.messages) or "the resume was refused")


async def known_facts(session: AsyncSession, ctx: OrgContext) -> dict[str, KnownFact]:
    """Every fact this workspace holds, confirmed or not.

    Proposals are included deliberately: the validator refuses them *by name*, and "that fact is
    still a proposal" is something a person can act on where "no such fact" sends them looking
    for a typo.
    """
    rows = await session.scalars(
        select(ProfileFact).where(ProfileFact.org_id == ctx.org_id).order_by(ProfileFact.created_at)
    )
    return {
        row.fact_id: KnownFact(
            fact_id=row.fact_id, statement=row.statement, confirmed=row.confirmed_at is not None
        )
        for row in rows
    }


@dataclass(frozen=True)
class NewVersion:
    version: ResumeVersion
    report: Report


async def create_version(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    ops: list[EditOp],
    label: str,
    header: str = "",
    job_post_id: uuid.UUID | None = None,
    created_by: str | None = None,
) -> NewVersion:
    """Validate, render, validate the rendering, and write one immutable row."""
    facts = await known_facts(session, ctx)
    report = validate.validate_ops(ops, facts)
    if not report.passed:
        raise ResumeRefused(report)
    if not report.cited:
        raise ResumeRefused(
            Report(problems=[validate.Problem(message="a resume needs at least one fact in it")])
        )

    if job_post_id is not None:
        posting = await session.scalar(
            select(JobPost).where(JobPost.id == job_post_id, JobPost.org_id == ctx.org_id)
        )
        if posting is None:
            raise ResumeRefused(
                Report(problems=[validate.Problem(message="that job posting does not exist")])
            )

    text = validate.render(ops, facts, header=header)
    # The artefact, not the plan. Run whatever produced `text`, so a future caller that renders
    # its own cannot get a sentence past this.
    rendered_report = validate.check_rendered(text, ops, facts, header=header)
    if not rendered_report.passed:
        raise ResumeRefused(rendered_report)

    row = ResumeVersion(
        org_id=ctx.org_id,
        job_post_id=job_post_id,
        label=" ".join((label or "Tailored resume").split())[:200],
        header=(header or "").strip(),
        edit_ops=as_json(ops),
        rendered_text=text,
        fact_ids=list(report.cited),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return NewVersion(version=row, report=report)


async def get_version(
    session: AsyncSession, ctx: OrgContext, *, version_id: uuid.UUID
) -> ResumeVersion | None:
    return await session.scalar(
        select(ResumeVersion).where(
            ResumeVersion.id == version_id, ResumeVersion.org_id == ctx.org_id
        )
    )


async def list_versions(
    session: AsyncSession, ctx: OrgContext, *, limit: int = 50
) -> list[ResumeVersion]:
    rows = await session.scalars(
        select(ResumeVersion)
        .where(ResumeVersion.org_id == ctx.org_id)
        .order_by(ResumeVersion.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def store_render(
    session: AsyncSession, ctx: OrgContext, *, version_id: uuid.UUID, content: bytes
) -> ResumeRender:
    """Replace the PDF for a version. The version itself is never touched."""
    row = await session.scalar(
        select(ResumeRender).where(
            ResumeRender.resume_version_id == version_id, ResumeRender.org_id == ctx.org_id
        )
    )
    digest = hashlib.sha256(content).hexdigest()
    if row is None:
        row = ResumeRender(
            org_id=ctx.org_id,
            resume_version_id=version_id,
            content=content,
            size_bytes=len(content),
            sha256=digest,
        )
        session.add(row)
    else:
        row.content = content
        row.size_bytes = len(content)
        row.sha256 = digest
        row.rendered_at = datetime.now(tz=UTC)
    await session.flush()
    return row


async def get_render(
    session: AsyncSession, ctx: OrgContext, *, version_id: uuid.UUID
) -> ResumeRender | None:
    return await session.scalar(
        select(ResumeRender).where(
            ResumeRender.resume_version_id == version_id, ResumeRender.org_id == ctx.org_id
        )
    )


# --------------------------------------------------------------------------- the tracker


class ApplicationError(Exception):
    """The tracker was asked for something it will not record, with a sentence saying why."""


async def upsert_application(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    job_post_id: uuid.UUID,
    resume_version_id: uuid.UUID | None = None,
    notes: str | None = None,
) -> Application:
    """Start tracking a posting, or point an existing row at a different resume version.

    Created `planned`: **a row appearing is not a claim that anybody applied.** Marking it
    applied is `set_status`, which only a person reaches.
    """
    posting = await session.scalar(
        select(JobPost).where(JobPost.id == job_post_id, JobPost.org_id == ctx.org_id)
    )
    if posting is None:
        raise ApplicationError("that job posting does not exist")
    if resume_version_id is not None:
        version = await get_version(session, ctx, version_id=resume_version_id)
        if version is None:
            raise ApplicationError("that resume version does not exist")

    row = await session.scalar(
        select(Application).where(
            Application.org_id == ctx.org_id, Application.job_post_id == job_post_id
        )
    )
    if row is None:
        row = Application(
            org_id=ctx.org_id,
            job_post_id=job_post_id,
            resume_version_id=resume_version_id,
            status="planned",
            notes=notes,
        )
        session.add(row)
    else:
        if resume_version_id is not None:
            row.resume_version_id = resume_version_id
        if notes is not None:
            row.notes = notes
    await session.flush()
    return row


async def set_status(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    application_id: uuid.UUID,
    status: str,
    follow_up_due: date | None = None,
    actor_id: str | None = None,
    now: datetime | None = None,
) -> Application | None:
    """Move an application. `applied` stamps the date, and nothing else sets that column.

    `applied_at` is set here and **never cleared**: going back to `planned` from `applied` is
    refused rather than quietly rewriting history, because the one thing a tracker must not do
    is forget that somebody sent a document to a company.
    """
    if status not in APPLICATION_STATUSES:
        raise ApplicationError(f"status must be one of {', '.join(APPLICATION_STATUSES)}")
    row = await session.scalar(
        select(Application).where(
            Application.id == application_id, Application.org_id == ctx.org_id
        )
    )
    if row is None:
        return None
    if status == "planned" and row.applied_at is not None:
        raise ApplicationError(
            "this application is already recorded as sent; it cannot go back to planned."
        )

    moment = now or datetime.now(tz=UTC)
    first_time = status == "applied" and row.applied_at is None
    if status in {"applied", "interviewing"} and row.applied_at is None:
        row.applied_at = moment
        if follow_up_due is None:
            follow_up_due = (moment + timedelta(days=FOLLOW_UP_DAYS)).date()
    row.status = status
    if follow_up_due is not None:
        row.follow_up_due = follow_up_due

    if first_time:
        # Who marked it, and when, in the append-only record. A tracker's whole value is that
        # its dates are the person's own; an entry nobody can attribute is one they will doubt.
        await record_event(
            session,
            org_id=ctx.org_id,
            type=APPLIED_EVENT,
            actor="user",
            actor_id=actor_id or ctx.actor_id,
            data={
                "application_id": str(row.id),
                "job_post_id": str(row.job_post_id),
                "resume_version_id": str(row.resume_version_id) if row.resume_version_id else None,
            },
        )
    await session.flush()
    return row


async def list_applications(
    session: AsyncSession, ctx: OrgContext, *, status: str | None = None
) -> list[Application]:
    statement = select(Application).where(Application.org_id == ctx.org_id)
    if status:
        statement = statement.where(Application.status == status)
    rows = await session.scalars(statement.order_by(Application.created_at.desc()))
    return list(rows)
