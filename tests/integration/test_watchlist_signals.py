"""The watchlist and its signals, against a real database (F11, ADR-049).

The two that matter most are the ones about what a signal is *not*: it never creates a lead, and
a re-poll never produces a second copy of the same thing.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import JobPost, Lead, SignalEvent, WatchedBoard, WatchlistEntry
from app.db.repo import OrgContext
from app.services.jobposts import intake as jobposts
from app.services.jobposts.feeds import Posting
from app.services.signals import intake, watchlist

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
LEVER_URL = "https://jobs.lever.co/acme/abc-123"


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


async def _watch(db, org, **kw):
    entry, _ = await watchlist.add(
        db,
        OrgContext(org_id=org.id),
        domain=kw.pop("domain", "acme.example"),
        ats_source=kw.pop("ats_source", "lever"),
        ats_slug=kw.pop("ats_slug", "acme"),
        **kw,
    )
    await db.commit()
    return entry


async def _poll(db, org, *, external_id: str = "abc-123", title: str = "Backend Engineer"):
    """What the daily poll does: store the postings, then produce signals from them."""
    ctx = OrgContext(org_id=org.id)
    result = await jobposts.store(
        db,
        ctx,
        [
            Posting(
                source="lever",
                external_id=external_id,
                title=title,
                apply_url=LEVER_URL,
            )
        ],
        now=NOW,
    )
    produced = await intake.from_postings(
        db, ctx, source="lever", slug="acme", postings=result.rows, now=NOW
    )
    await db.commit()
    return produced


class TestWatchingACompany:
    async def test_a_watch_puts_its_board_in_the_daily_poll(self, db, org) -> None:
        """A watch is a reason to read a board; it does not read one itself."""
        await _watch(db, org)

        board = await db.scalar(
            select(WatchedBoard).where(WatchedBoard.org_id == org.id, WatchedBoard.slug == "acme")
        )
        assert board is not None
        assert board.source == "lever"

    async def test_watching_a_board_somebody_already_follows_adds_no_second_row(
        self, db, org
    ) -> None:
        """`uq_watched_boards_org_source_slug` would refuse it, and an integrity error raised
        from adding a watch is a failure on a path nobody expects one from."""
        db.add(WatchedBoard(org_id=org.id, source="lever", slug="acme", label="for job hunting"))
        await db.commit()

        await _watch(db, org)

        boards = (
            await db.scalars(
                select(WatchedBoard).where(
                    WatchedBoard.org_id == org.id, WatchedBoard.slug == "acme"
                )
            )
        ).all()
        assert len(boards) == 1
        assert boards[0].label == "for job hunting", "the existing one is left alone"

    async def test_a_url_is_reduced_to_the_company_domain(self, db, org) -> None:
        """People paste a URL as often as a domain, and both name one company."""
        entry = await _watch(db, org, domain="https://www.acme.example/careers?x=1")

        assert entry.domain == "acme.example"

    async def test_watching_twice_updates_rather_than_failing(self, db, org) -> None:
        await _watch(db, org, label="Acme")
        entry, created = await watchlist.add(
            db,
            OrgContext(org_id=org.id),
            domain="acme.example",
            label="Acme Analytics",
            ats_source="lever",
            ats_slug="acme",
        )
        await db.commit()

        assert created is False
        assert entry.label == "Acme Analytics"
        rows = (
            await db.scalars(select(WatchlistEntry).where(WatchlistEntry.org_id == org.id))
        ).all()
        assert len(rows) == 1

    async def test_a_slug_without_a_board_is_refused(self, db, org) -> None:
        with pytest.raises(watchlist.WatchRefused):
            await watchlist.add(
                db, OrgContext(org_id=org.id), domain="acme.example", ats_slug="acme"
            )


class TestAPostingBecomesASignal:
    async def test_a_new_posting_at_a_watched_company_produces_one(self, db, org) -> None:
        await _watch(db, org)

        produced = await _poll(db, org)

        assert len(produced.created) == 1
        signal = produced.created[0]
        assert signal.kind == "hiring"
        assert signal.domain == "acme.example"
        assert signal.title == "Backend Engineer"
        assert signal.job_post_id is not None

    async def test_polling_again_produces_nothing_new(self, db, org) -> None:
        """**The test this table's unique key exists for.** The daily poll sees yesterday's
        postings again every single time it runs, so without it every morning would add a fresh
        copy of every open role and the list would be unusable inside a week."""
        await _watch(db, org)
        await _poll(db, org)

        again = await _poll(db, org)

        assert again.created == []
        assert again.already_known == 1
        rows = (await db.scalars(select(SignalEvent).where(SignalEvent.org_id == org.id))).all()
        assert len(rows) == 1

    async def test_a_board_nobody_watches_produces_nothing(self, db, org) -> None:
        """A board polled for job hunting and a company watched for outreach are independent
        uses of one fetch. Not an error."""
        produced = await _poll(db, org)

        assert produced.created == []
        assert (await db.scalar(select(SignalEvent).where(SignalEvent.org_id == org.id))) is None

    async def test_a_paused_watch_produces_nothing(self, db, org) -> None:
        entry = await _watch(db, org)
        await watchlist.set_paused(db, OrgContext(org_id=org.id), entry_id=entry.id, paused=True)
        await db.commit()

        produced = await _poll(db, org)

        assert produced.created == []

    async def test_a_watch_added_after_the_postings_still_gets_them(self, db, org) -> None:
        """The first poll after a watch is a poll of rows already stored, so a producer that
        only looked at newly-created postings would produce nothing until the company posted
        something new -- which for a quiet company is never."""
        await _poll(db, org)  # nobody watching yet; the posting is stored
        await _watch(db, org)

        produced = await _poll(db, org)

        assert len(produced.created) == 1

    async def test_another_workspace_sees_nothing(self, db, org, org_factory=None) -> None:
        """ADR-004 asserted rather than assumed: a watch is one workspace's interest."""
        from app.db.models import Org

        other = Org(name="Somebody Else")
        db.add(other)
        await db.commit()
        await _watch(db, org)
        await _poll(db, org)

        rows = (await db.scalars(select(SignalEvent).where(SignalEvent.org_id == other.id))).all()
        assert rows == []


class TestASignalNeverCreatesALead:
    """The rule of the slice."""

    async def test_a_signal_with_no_lead_creates_nobody(self, db, org) -> None:
        await _watch(db, org)

        produced = await _poll(db, org)

        assert produced.created[0].lead_id is None
        assert (await db.scalars(select(Lead).where(Lead.org_id == org.id))).all() == []

    async def test_a_signal_matches_a_lead_this_workspace_already_has(
        self, db, org, lead_factory
    ) -> None:
        lead = await lead_factory(
            state="SENT",
            email="asha@acme.example",
            company_name="Acme",
            company_url="https://acme.example",
            domain="acme.example",
        )
        await _watch(db, org)

        produced = await _poll(db, org)

        assert produced.created[0].lead_id == lead.id

    async def test_a_terminal_lead_is_not_matched(self, db, org, lead_factory) -> None:
        """A person who opted out is not somebody a new reason to write makes approachable
        again. Attaching a signal to them would put a suggestion on screen beside somebody we
        have been told to leave alone."""
        await lead_factory(
            state="OPTED_OUT",
            email="asha@acme.example",
            company_name="Acme",
            company_url="https://acme.example",
            domain="acme.example",
        )
        await _watch(db, org)

        produced = await _poll(db, org)

        assert produced.created[0].lead_id is None


class TestTheRoutes:
    async def test_a_member_cannot_watch_a_company(self, api, member) -> None:
        response = api.post("/api/v1/watchlist", json={"domain": "acme.example"}, headers=member)

        assert response.status_code == 403, response.text

    async def test_watching_without_a_board_says_nothing_will_arrive(self, api, owner) -> None:
        """Said rather than left to be discovered: a watch with no board produces nothing, and
        silence is indistinguishable from a quiet company."""
        response = api.post("/api/v1/watchlist", json={"domain": "acme.example"}, headers=owner)

        assert response.status_code == 201, response.text
        assert "No board" in response.json()["note"]

    async def test_nonsense_is_refused_with_a_sentence(self, api, owner) -> None:
        response = api.post("/api/v1/watchlist", json={"domain": "not a domain"}, headers=owner)

        assert response.status_code == 422, response.text

    async def test_forgetting_a_company_keeps_its_signals(self, api, owner, db, org) -> None:
        """Deleting the record that a company was hiring in March would quietly rewrite a
        funnel, so the signal survives with its own domain."""
        entry = await _watch(db, org)
        await _poll(db, org)

        response = api.delete(f"/api/v1/watchlist/{entry.id}", headers=owner)

        assert response.status_code == 200, response.text
        rows = (await db.scalars(select(SignalEvent).where(SignalEvent.org_id == org.id))).all()
        assert len(rows) == 1
        assert rows[0].domain == "acme.example"
        assert rows[0].watchlist_id is None

    async def test_a_signal_can_be_acted_on_or_dismissed_but_not_reset(
        self, api, owner, db, org
    ) -> None:
        await _watch(db, org)
        produced = await _poll(db, org)
        signal_id = produced.created[0].id

        acted = api.post(
            f"/api/v1/signals/{signal_id}/state", json={"state": "acted"}, headers=owner
        )
        reset = api.post(f"/api/v1/signals/{signal_id}/state", json={"state": "new"}, headers=owner)

        assert acted.status_code == 200, acted.text
        assert acted.json()["signal"]["state"] == "acted"
        assert reset.status_code == 422, "a list that can be reset is one nobody trusts to shrink"

    async def test_the_list_says_how_many_have_nobody_to_write_to(
        self, api, owner, db, org
    ) -> None:
        """The number worth opening the screen for: a company worth approaching and nobody to
        approach is exactly what the contact lookup is for."""
        await _watch(db, org)
        await _poll(db, org)

        body = api.get("/api/v1/signals", headers=owner).json()

        assert body["without_a_lead"] == 1
        assert body["signals"][0]["lead_name"] is None

    async def test_no_posting_text_or_person_is_returned(self, api, owner, db, org) -> None:
        """A signal is a title and a link. The posting's body belongs to the job-hunt side, and
        nothing about a person belongs on a company's signal at all."""
        await _watch(db, org)
        await _poll(db, org)

        body = api.get("/api/v1/signals", headers=owner).json()

        assert set(body["signals"][0]) == {
            "id",
            "domain",
            "kind",
            "source",
            "title",
            "url",
            "state",
            "lead_id",
            "lead_name",
            "job_post_id",
            "observed_at",
            "created_at",
        }


class TestThePostingsAreUntouched:
    async def test_the_poll_stores_postings_exactly_as_before(self, db, org) -> None:
        """A watch changes what the poll *produces*, never what it stores. The job-hunt side
        must behave identically whether or not anybody is watching for outreach."""
        await _poll(db, org)

        post = await db.scalar(select(JobPost).where(JobPost.org_id == org.id))
        assert post is not None
        assert post.title == "Backend Engineer"
        assert post.board_slug == "acme"
        assert post.company_domain is None, "a feed's apply URL is the board's (ADR-047)"
