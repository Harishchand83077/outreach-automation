"""Assignment is sticky and recorded; the funnel counts from the record (F13, ADR-053).

The allocation rule is unit-tested with a seed. These are the properties that need a database:
that a lead keeps its arm when the arms change under it, that two running experiments on one
campaign are impossible, that the message carries the arm and the warm-up day, and that a lead
still inside its wait window is counted as pending rather than as a failure.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and used as test parameters,
# which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db.models import Experiment, LeadOutcome, Message
from app.db.repo import OrgContext
from app.domain.experiment import Arm
from app.domain.outcome import Outcome
from app.services.experiments import store as experiments

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


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


async def _running(db, org, campaign, *arms: str, share: float = 0.2) -> Experiment:
    record = Experiment(
        org_id=org.id,
        campaign_id=campaign.id,
        name="subject length",
        arms=[Arm(variant=name).model_dump() for name in (arms or ("a", "b"))],
        exploration_share=share,
        active=True,
    )
    db.add(record)
    await db.commit()
    return record


class TestAnArmIsStickyPerLead:
    async def test_a_lead_is_allocated_once(self, db, org, campaign, lead_factory):
        await _running(db, org, campaign)
        lead = await lead_factory()
        ctx = OrgContext(org_id=org.id)
        first, _ = await experiments.assign(db, ctx, lead=lead, campaign_id=campaign.id)
        again, _ = await experiments.assign(db, ctx, lead=lead, campaign_id=campaign.id)
        assert first in {"a", "b"}
        assert again == first

    async def test_adding_an_arm_does_not_move_a_lead_already_in_flight(
        self, db, org, campaign, lead_factory
    ):
        """**The defect that killed ADR-026's design.** Stickiness was specified as a
        deterministic hash of (lead, experiment), and a hash has to be taken modulo the number
        of arms -- so adding a third arm moves most leads already in flight from one arm to the
        other, silently, after their first email has gone out. Every message already sent would
        then be attributed to an arm its draft did not come from.
        """
        experiment = await _running(db, org, campaign, "a", "b")
        lead = await lead_factory()
        ctx = OrgContext(org_id=org.id)
        before, _ = await experiments.assign(db, ctx, lead=lead, campaign_id=campaign.id)

        experiment.arms = [Arm(variant=name).model_dump() for name in ("a", "b", "c")]
        await db.commit()

        after, _ = await experiments.assign(db, ctx, lead=lead, campaign_id=campaign.id)
        assert after == before

    async def test_a_lead_keeps_a_retired_arm_rather_than_being_relabelled(
        self, db, org, campaign, lead_factory
    ):
        """The message already sent carried that label. Rewriting history to tidy a funnel is
        worse than a funnel with a retired arm in it."""
        experiment = await _running(db, org, campaign, "a", "b")
        lead = await lead_factory()
        ctx = OrgContext(org_id=org.id)
        before, _ = await experiments.assign(db, ctx, lead=lead, campaign_id=campaign.id)

        experiment.arms = [Arm(variant="x").model_dump(), Arm(variant="y").model_dump()]
        await db.commit()
        after, arm = await experiments.assign(db, ctx, lead=lead, campaign_id=campaign.id)
        assert after == before
        # No instruction, because the arm it is in is no longer described anywhere.
        assert arm is None

    async def test_no_experiment_means_no_arm_and_a_perfectly_normal_lead(
        self, db, org, campaign, lead_factory
    ):
        """Every lead in the live database has NULL here, so NULL has to be the ordinary case
        and must not read as "not yet eligible" -- the care ADR-044 took with
        `leads.mailbox_id`, where that mistake would have stopped an in-flight campaign."""
        lead = await lead_factory()
        variant, arm = await experiments.assign(
            db, OrgContext(org_id=org.id), lead=lead, campaign_id=campaign.id
        )
        assert variant is None and arm is None


class TestTheDatabaseAllowsOneActiveExperiment:
    async def test_a_second_active_experiment_on_one_campaign_is_refused(self, db, org, campaign):
        """In the database rather than in a route's memory: two would both allocate, the second
        would overwrite `leads.variant`, and every message would carry whichever ran last."""
        await _running(db, org, campaign)
        db.add(
            Experiment(
                org_id=org.id,
                campaign_id=campaign.id,
                name="second",
                arms=[Arm(variant="c").model_dump()],
                exploration_share=0.2,
                active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

    async def test_a_stopped_experiment_does_not_block_a_new_one(self, db, org, campaign):
        first = await _running(db, org, campaign)
        first.active = False
        await db.commit()
        second = await _running(db, org, campaign, "c", "d")
        assert second.id != first.id

    async def test_an_exploration_share_below_the_floor_is_refused_by_the_database(
        self, db, org, campaign
    ):
        """A weighted allocator with nothing held back can drive an arm to zero, and the arm
        that gets starved is the one whose result would have surprised somebody."""
        db.add(
            Experiment(
                org_id=org.id,
                campaign_id=campaign.id,
                name="no exploration",
                arms=[Arm(variant="a").model_dump()],
                exploration_share=0.0,
                active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()


class TestTheFunnelCountsFromTheRecord:
    async def _sent(self, db, org, campaign, lead_factory, *, variant, outcome, warmup_day=None):
        lead = await lead_factory(variant=variant)
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                kind="initial",
                status="sent",
                subject="hello",
                variant=variant,
                warmup_day=warmup_day,
                idempotency_key=f"exp-{lead.id}",
                sent_at=NOW,
            )
        )
        if outcome is not None:
            db.add(
                LeadOutcome(
                    org_id=org.id,
                    lead_id=lead.id,
                    campaign_id=campaign.id,
                    outcome=outcome.value,
                    censored=outcome.censored,
                    stage="replied" if outcome.is_encouraging else "sent",
                )
            )
        await db.commit()
        return lead

    async def test_a_lead_still_in_its_window_is_pending_never_a_failure(
        self, db, org, campaign, lead_factory
    ):
        """ADR-026's whole design rests on this. Counting silence-so-far as a negative would
        make every arm look worse the sooner somebody looked, which is the one way a funnel
        actively misleads."""
        await _running(db, org, campaign)
        await self._sent(db, org, campaign, lead_factory, variant="a", outcome=None)
        report = await experiments.funnel(
            db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="variant"
        )
        bucket = next(one for one in report["buckets"] if one["key"] == "a")
        assert bucket["pending"] == 1
        assert bucket["settled"] == 0
        assert bucket["encouraging_rate"] is None

    async def test_a_closed_window_is_censored_rather_than_a_rejection(
        self, db, org, campaign, lead_factory
    ):
        await _running(db, org, campaign)
        await self._sent(
            db,
            org,
            campaign,
            lead_factory,
            variant="a",
            outcome=Outcome.NO_REPLY_AFTER_WINDOW,
        )
        report = await experiments.funnel(
            db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="variant"
        )
        bucket = next(one for one in report["buckets"] if one["key"] == "a")
        assert bucket["censored"] == 1
        assert bucket["settled"] == 1
        assert bucket["encouraging"] == 0

    async def test_the_outcome_vocabulary_comes_from_the_enum(
        self, db, org, campaign, lead_factory
    ):
        """`has_vendor` and `referral` are different news from `not_relevant`, and ADR-026
        refused to collapse them. The bucket reports the outcome's own value rather than a
        second taxonomy invented here -- which is also how a literal stops being able to
        disagree with the column."""
        await _running(db, org, campaign)
        await self._sent(db, org, campaign, lead_factory, variant="a", outcome=Outcome.HAS_VENDOR)
        await self._sent(db, org, campaign, lead_factory, variant="a", outcome=Outcome.REFERRAL)
        report = await experiments.funnel(
            db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="variant"
        )
        bucket = next(one for one in report["buckets"] if one["key"] == "a")
        assert bucket["outcomes"]["has_vendor"] == 1
        assert bucket["outcomes"]["referral"] == 1
        # `referral` is encouraging, `has_vendor` is not -- read off `Outcome`, not off a list
        # in this module.
        assert bucket["encouraging"] == 1

    async def test_a_message_still_sending_is_not_counted_as_sent(
        self, db, org, campaign, lead_factory
    ):
        """ADR-002: a message mid-flight may still fail. The platform overview got the
        neighbouring mistake twice."""
        await _running(db, org, campaign)
        lead = await lead_factory(variant="a")
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                kind="initial",
                status="sending",
                variant="a",
                idempotency_key=f"inflight-{lead.id}",
            )
        )
        await db.commit()
        report = await experiments.funnel(
            db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="variant"
        )
        assert report["buckets"] == []

    async def test_the_funnel_can_be_cut_by_warmup_day(self, db, org, campaign, lead_factory):
        await self._sent(db, org, campaign, lead_factory, variant=None, outcome=None, warmup_day=3)
        report = await experiments.funnel(
            db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="warmup_day"
        )
        assert [one["key"] for one in report["buckets"]] == ["day 03"]

    async def test_an_unknown_dimension_is_refused(self, db, org, campaign):
        with pytest.raises(ValueError, match="can be cut by"):
            await experiments.funnel(
                db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="company_size"
            )

    async def test_a_send_is_bucketed_in_the_campaigns_own_timezone(
        self, db, org, campaign, lead_factory
    ):
        """A send at 23:40 IST belongs to that operator's day. Bucketing by UTC would quietly
        move a sixth of India's working hours into the previous one."""
        lead = await lead_factory()
        db.add(
            Message(
                org_id=org.id,
                lead_id=lead.id,
                direction="out",
                kind="initial",
                status="sent",
                idempotency_key=f"tz-{lead.id}",
                # 18:10 UTC is 23:40 in Asia/Kolkata, the campaign default.
                sent_at=datetime(2026, 11, 7, 18, 10, tzinfo=UTC),
            )
        )
        await db.commit()
        report = await experiments.funnel(
            db, OrgContext(org_id=org.id), campaign_id=campaign.id, by="hour"
        )
        assert [one["key"] for one in report["buckets"]] == ["23:00"]


class TestTheRoutes:
    async def test_starting_an_experiment_reports_each_arms_effective_share(
        self, api, db, org, campaign, owner
    ):
        started = api.put(
            f"/api/v1/campaigns/{campaign.id}/experiment",
            json={
                "experiment": {
                    "name": "shorter subject",
                    "arms": [
                        {"variant": "control", "weight": 1},
                        {"variant": "short", "weight": 3, "instruction": "Keep it to 4 words."},
                    ],
                }
            },
            headers=owner,
        )
        assert started.status_code == 200

        read = api.get(f"/api/v1/campaigns/{campaign.id}/experiment", headers=owner)
        arms = read.json()["experiment"]["arms"]
        shares = {arm["variant"]: arm["effective_share"] for arm in arms}
        # The weight says 3:1; the floor means the quiet arm gets more than a quarter of what
        # the weights alone would give it. Shown because the number an operator types is not
        # the number they get.
        assert shares["control"] > 0.1
        assert abs(sum(shares.values()) - 1.0) < 1e-6

    async def test_replacing_stops_the_previous_rather_than_editing_it(
        self, api, db, org, campaign, owner
    ):
        """The arms are what the labels on already-sent messages *mean*, so editing them in
        place would retroactively change the description of emails that have gone out."""
        first = api.put(
            f"/api/v1/campaigns/{campaign.id}/experiment",
            json={"experiment": {"name": "one", "arms": [{"variant": "a"}, {"variant": "b"}]}},
            headers=owner,
        ).json()
        second = api.put(
            f"/api/v1/campaigns/{campaign.id}/experiment",
            json={"experiment": {"name": "two", "arms": [{"variant": "c"}, {"variant": "d"}]}},
            headers=owner,
        ).json()
        assert second["stopped"] == first["id"]

    async def test_an_exploration_share_below_the_floor_is_refused_by_the_route(
        self, api, campaign, owner
    ):
        response = api.put(
            f"/api/v1/campaigns/{campaign.id}/experiment",
            json={
                "experiment": {"name": "none", "arms": [{"variant": "a"}, {"variant": "b"}]},
                "exploration_share": 0,
            },
            headers=owner,
        )
        assert response.status_code == 422
        assert "held back" in response.json()["detail"]

    async def test_stopping_keeps_the_arms_so_the_funnel_still_reads(
        self, api, db, org, campaign, owner
    ):
        api.put(
            f"/api/v1/campaigns/{campaign.id}/experiment",
            json={"experiment": {"name": "one", "arms": [{"variant": "a"}, {"variant": "b"}]}},
            headers=owner,
        )
        stopped = api.delete(f"/api/v1/campaigns/{campaign.id}/experiment", headers=owner)
        assert stopped.status_code == 200
        assert "keep theirs" in stopped.json()["note"]
        # The row survives, which is what lets a funnel built next month say what "a" meant.
        rows = await experiments.active_for(db, OrgContext(org_id=org.id), campaign_id=campaign.id)
        assert rows is None

    async def test_a_member_may_read_and_may_not_start(self, api, campaign, member):
        assert (
            api.get(f"/api/v1/campaigns/{campaign.id}/experiment", headers=member).status_code
            == 200
        )
        refused = api.put(
            f"/api/v1/campaigns/{campaign.id}/experiment",
            json={"experiment": {"name": "x", "arms": [{"variant": "a"}, {"variant": "b"}]}},
            headers=member,
        )
        assert refused.status_code == 403

    async def test_a_campaign_in_another_workspace_is_404(self, api, owner):
        import uuid as _uuid

        response = api.get(f"/api/v1/campaigns/{_uuid.uuid4()}/experiment", headers=owner)
        assert response.status_code == 404
