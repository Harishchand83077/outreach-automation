"""The three callers that acted on a job board as if it were a company (ADR-047).

Each one spent something real: a provider credit, a research fetch and a model call, or the
user's credibility with somebody they know. So each gets a test asserting it now refuses or
resolves, and the resolution is asserted to come from a fact rather than from a guess.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

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
from app.db.models import CompanyProfile, ContactCandidate, JobPost, Lead
from app.db.repo import OrgContext
from app.services.contacts import keys, lookup
from app.services.jobposts import company, intake
from app.services.jobposts.feeds import Posting

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
GREENHOUSE_URL = "https://job-boards.greenhouse.io/vercel/jobs/6231864004"


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": TEST_KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    _, public = keypair
    application = create_app(
        _settings(CORS_ALLOWED_ORIGINS="https://dash.example"),
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


async def _feed_posting(db, org) -> JobPost:
    """A posting exactly as the Greenhouse poll stores one."""
    await intake.store(
        db,
        OrgContext(org_id=org.id),
        [
            Posting(
                source="greenhouse",
                external_id="6231864004",
                title="Account Executive",
                apply_url=GREENHOUSE_URL,
            )
        ],
    )
    await db.commit()
    row = await db.scalar(select(JobPost).where(JobPost.org_id == org.id))
    assert row is not None
    return row


class TestWhatTheIntakeStores:
    async def test_a_feed_posting_records_the_slug_and_no_domain(self, db, org) -> None:
        """The host is the board's, so there is no company domain to record -- and the slug,
        which is the company's name on that board, is recorded instead."""
        row = await _feed_posting(db, org)

        assert row.company_domain is None
        assert row.board_slug == "vercel"

    async def test_a_pasted_posting_on_a_companys_own_site_keeps_its_domain(self, db, org) -> None:
        post, _ = await intake.store_pasted(
            db,
            OrgContext(org_id=org.id),
            url="https://acme.example/careers/backend",
            title="Backend Engineer",
        )
        await db.commit()

        assert post.company_domain == "acme.example"
        assert post.board_slug is None


class TestResolvingTheCompany:
    async def test_the_slug_resolves_through_the_shared_company_cache(self, db, org) -> None:
        """The one place a slug becomes a domain, and it is a fact some workspace established
        from a careers page rather than a guess. F19 records `ats_type` and `ats_slug` per
        domain, which is exactly this lookup in reverse."""
        db.add(
            CompanyProfile(
                domain="vercel.com", name="Vercel", ats_type="greenhouse", ats_slug="vercel"
            )
        )
        row = await _feed_posting(db, org)

        ref = await company.resolve(db, row)

        assert ref.domain == "vercel.com"
        assert ref.label == "Vercel"
        assert ref.via == "board slug"

    async def test_an_unknown_slug_resolves_to_nothing_rather_than_a_guess(self, db, org) -> None:
        """`vercel` is not `vercel.com` reliably enough to assume, and a guess here is a credit
        spent on the wrong company and an email about them."""
        row = await _feed_posting(db, org)

        ref = await company.resolve(db, row)

        assert ref.domain is None
        assert ref.known is False
        assert ref.label == "vercel", "still readable, but never passed off as a domain"
        assert ref.via == "unknown"

    async def test_a_posting_with_its_own_domain_needs_no_lookup(self, db, org) -> None:
        post, _ = await intake.store_pasted(
            db,
            OrgContext(org_id=org.id),
            url="https://acme.example/careers/backend",
            title="Backend Engineer",
        )
        await db.commit()

        ref = await company.resolve(db, post)

        assert ref.domain == "acme.example"
        assert ref.via == "posting"


class TestTheContactLookupNoLongerSearchesTheAtsVendor:
    """**The defect that spent money.** A lookup from a feed posting took the posting's domain,
    so asking who is hiring at a company billed one of the user's own provider credits to find
    people who work at Greenhouse."""

    async def _with_mock_key(self, db, org) -> None:
        await keys.store(
            db,
            OrgContext(org_id=org.id),
            provider="mock",
            secret="pretend",
            settings=_settings(),
        )
        await db.commit()

    async def test_a_lookup_on_a_feed_posting_is_refused_with_a_sentence(self, db, org) -> None:
        await self._with_mock_key(db, org)
        row = await _feed_posting(db, org)

        with pytest.raises(lookup.LookupRefused) as caught:
            await lookup.run(
                db,
                OrgContext(org_id=org.id),
                provider="mock",
                job_post_id=row.id,
                settings=_settings(),
            )

        message = str(caught.value)
        assert "job board" in message
        assert "company's domain" in message

    async def test_it_runs_once_the_slug_can_be_resolved(self, db, org) -> None:
        """Refusing is not the feature -- resolving is. With the shared cache knowing the slug,
        the same lookup proceeds against the company's own domain."""
        await self._with_mock_key(db, org)
        db.add(
            CompanyProfile(
                domain="vercel.com", name="Vercel", ats_type="greenhouse", ats_slug="vercel"
            )
        )
        row = await _feed_posting(db, org)

        result = await lookup.run(
            db,
            OrgContext(org_id=org.id),
            provider="mock",
            job_post_id=row.id,
            settings=_settings(),
        )

        assert result.domain == "vercel.com"

    async def test_a_board_domain_typed_by_hand_is_refused_too(self, db, org) -> None:
        """Reachable by pasting the posting's own link into the box. Refused rather than run,
        because the lookup would succeed and bill them for contacts at an ATS."""
        await self._with_mock_key(db, org)

        with pytest.raises(lookup.LookupRefused) as caught:
            await lookup.run(
                db,
                OrgContext(org_id=org.id),
                provider="mock",
                domain="job-boards.greenhouse.io",
                settings=_settings(),
            )

        assert "job board" in str(caught.value)


class TestNoLeadIsEverCreatedForAJobBoard:
    """**The worst of the three.** An imported contact was given the vendor as its
    `company_url`, which the research pipeline fetched, synthesised and cited -- and
    `cache_key()` reduces that host to `greenhouse.io`, so every feed-sourced lead in a
    workspace shared one `domain_research` row and the second was served the first's findings.
    ADR-011's defect through a different door."""

    async def test_importing_a_contact_recorded_against_a_board_is_refused(
        self, api, owner, db, org, campaign
    ) -> None:
        candidate = ContactCandidate(
            org_id=org.id,
            company_domain="job-boards.greenhouse.io",
            provider="mock",
            full_name="Ben Shah",
            title="Head of Talent",
            email="ben@example.com",
            email_norm="ben@example.com",
            email_status="verified",
            state="proposed",
        )
        db.add(candidate)
        await db.commit()

        response = api.post(
            f"/api/v1/contact-candidates/{candidate.id}/import",
            json={"campaign_id": str(campaign.id)},
            headers=owner,
        )

        assert response.status_code == 422, response.text
        assert "job board" in response.json()["detail"]

        leads = (await db.scalars(select(Lead).where(Lead.org_id == org.id))).all()
        assert leads == [], "and nothing was created on the way to refusing"

    async def test_a_real_company_still_imports(self, api, owner, db, org, campaign) -> None:
        """The guard must refuse a board and nothing else, or it has broken the feature it was
        added to protect."""
        candidate = ContactCandidate(
            org_id=org.id,
            company_domain="acme.example",
            provider="mock",
            full_name="Asha Rao",
            title="Founder",
            email="asha@example.com",
            email_norm="asha@example.com",
            email_status="verified",
            state="proposed",
        )
        db.add(candidate)
        await db.commit()

        response = api.post(
            f"/api/v1/contact-candidates/{candidate.id}/import",
            json={"campaign_id": str(campaign.id)},
            headers=owner,
        )

        assert response.status_code == 201, response.text
        lead = await db.scalar(select(Lead).where(Lead.org_id == org.id))
        assert lead is not None
        assert lead.company_url == "https://acme.example"
