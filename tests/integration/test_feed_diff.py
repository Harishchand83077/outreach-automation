"""A complete poll closes what it did not see, and a failed one closes nothing (F22, ADR-054).

The second half is the one that matters. `close_missing` archives every open posting of a board
that the current poll did not return, so an accidentally empty set would archive a company's
entire board on one bad afternoon and report it as every role being filled. The job-post intake
has already paid for the neighbouring version of that mistake: an unknown source with a
plausible payload returned zero postings, which read as an empty board.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters,
# which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import JobPost
from app.db.repo import OrgContext
from app.services.jobposts import intake
from app.services.jobposts.feeds import Posting

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    _, public = keypair
    application = create_app(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            GROQ_API_KEY="gsk_test",
            CREDENTIAL_KEY=TEST_KEY,
            CORS_ALLOWED_ORIGINS="https://dash.example",
        ),
        limiter=RateLimit(limit=10_000, window_seconds=60.0),
    )

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[deps.get_session] = override_session
    deps.set_verifier(TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=FakeJwks(public)))
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client
    deps.set_verifier(None)


def _posting(external_id: str, title: str) -> Posting:
    return Posting(
        source="lever",
        external_id=external_id,
        title=title,
        location="Bengaluru",
        apply_url=f"https://jobs.lever.co/acme/{external_id}",
        posted_at=NOW,
        body_text="We are hiring.",
    )


class TestTheDiff:
    async def test_a_posting_that_left_the_feed_is_archived(self, db, org):
        ctx = OrgContext(org_id=org.id)
        await intake.store(db, ctx, [_posting("a1", "Backend Engineer"), _posting("b2", "SRE")])
        await db.commit()

        result = await intake.close_missing(db, ctx, source="lever", slug="acme", seen={"a1"})
        await db.commit()

        assert result.closed == 1
        assert result.closed_titles == ["SRE"]
        rows = {
            row.external_id: row.status
            for row in (await db.scalars(intake.select(JobPost))).all()
            if row.org_id == org.id
        }
        assert rows == {"a1": "open", "b2": "archived"}

    async def test_an_archived_posting_is_never_deleted(self, db, org):
        """`applications` may point at a posting the candidate applied to, and the record of
        what they applied to has to survive the role being filled."""
        ctx = OrgContext(org_id=org.id)
        await intake.store(db, ctx, [_posting("a1", "Backend Engineer")])
        await db.commit()
        await intake.close_missing(db, ctx, source="lever", slug="acme", seen=set())
        await db.commit()
        row = await db.scalar(
            intake.select(JobPost).where(JobPost.org_id == org.id, JobPost.external_id == "a1")
        )
        assert row is not None and row.status == "archived"

    async def test_a_relisted_role_reopens_rather_than_becoming_a_second_posting(self, db, org):
        """The id is the board's own, so this is the same role rather than a new one. Treating
        it as new would double-count the company's hiring in any funnel built on these rows."""
        ctx = OrgContext(org_id=org.id)
        await intake.store(db, ctx, [_posting("a1", "Backend Engineer")])
        await intake.close_missing(db, ctx, source="lever", slug="acme", seen=set())
        await db.commit()

        await intake.store(db, ctx, [_posting("a1", "Backend Engineer")])
        result = await intake.close_missing(db, ctx, source="lever", slug="acme", seen={"a1"})
        await db.commit()

        assert result.reopened == 1
        rows = (
            await db.scalars(
                intake.select(JobPost).where(JobPost.org_id == org.id, JobPost.external_id == "a1")
            )
        ).all()
        assert len(rows) == 1 and rows[0].status == "open"

    async def test_an_empty_board_really_is_an_empty_board(self, db, org):
        """A company that closed its last role is a real and interesting event. Refusing to
        believe it would be second-guessing the only evidence we have -- and the protection
        against an *outage* looking like this is that `fetch_board` raises long before here."""
        ctx = OrgContext(org_id=org.id)
        await intake.store(db, ctx, [_posting("a1", "Backend Engineer")])
        await db.commit()
        result = await intake.close_missing(db, ctx, source="lever", slug="acme", seen=set())
        assert result.closed == 1

    async def test_another_boards_postings_are_untouched(self, db, org):
        """Scoped by `(source, board_slug)`: polling Acme must not archive Beta's roles."""
        ctx = OrgContext(org_id=org.id)
        await intake.store(db, ctx, [_posting("a1", "Backend Engineer")])
        other = Posting(
            source="lever",
            external_id="z9",
            title="Designer",
            location=None,
            apply_url="https://jobs.lever.co/beta/z9",
            posted_at=NOW,
            body_text=None,
        )
        await intake.store(db, ctx, [other])
        await db.commit()

        await intake.close_missing(db, ctx, source="lever", slug="acme", seen=set())
        await db.commit()

        beta = await db.scalar(
            intake.select(JobPost).where(JobPost.org_id == org.id, JobPost.external_id == "z9")
        )
        assert beta is not None and beta.status == "open"

    async def test_another_workspaces_postings_are_untouched(self, db, org):
        """ADR-004, asserted rather than assumed: two workspaces watching one company is the
        ordinary case in this product, and one's poll must not archive the other's rows."""
        from app.db.models import Org

        other_org = Org(name="Somebody Else")
        db.add(other_org)
        await db.commit()
        other_ctx = OrgContext(org_id=other_org.id)
        await intake.store(db, other_ctx, [_posting("a1", "Backend Engineer")])
        await db.commit()

        await intake.close_missing(
            db, OrgContext(org_id=org.id), source="lever", slug="acme", seen=set()
        )
        await db.commit()

        theirs = await db.scalar(
            intake.select(JobPost).where(
                JobPost.org_id == other_org.id, JobPost.external_id == "a1"
            )
        )
        assert theirs is not None and theirs.status == "open"
