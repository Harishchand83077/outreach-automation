"""Heartbeats, alerts, and `/ready` (§18; issues C6, E7).

The behaviour under test is the one that was missing entirely: **the api can tell whether the
worker is alive.** Until Phase 9 it could not — the worker's health lived in its own memory, so
a dead worker left a perfectly healthy-looking api that quietly researched, drafted and sent
nothing. These tests pin the signal that fixes that, which is the *age* of a row rather than its
presence.

The alert tests are mostly about the cool-down. An alert that repeats once a minute for a day is
not an alert; it is a reason to turn alerts off.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api import deps
from app.api.app import create_app
from app.api.ratelimit import RateLimit
from app.api.security import TokenVerifier
from app.core.settings import Settings
from app.db import ops
from app.db.models import Alert, Member, Org
from app.services.alerts import WebhookNotifier, raise_alert

pytestmark = pytest.mark.asyncio(loop_scope="session")

ISSUER = "https://proj.supabase.co/auth/v1"


@pytest.fixture(scope="session")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


class FakeJwks:
    def __init__(self, public_pem: str) -> None:
        self.public_pem = public_pem

    def signing_key(self, token: str):
        return serialization.load_pem_public_key(self.public_pem.encode())


def token_for(private_pem: str, *, subject: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": subject, "email": f"{subject}@example.com", "iss": ISSUER, "exp": now + 3600},
        private_pem,
        algorithm="RS256",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    _, public = keypair
    application = create_app(
        Settings(_env_file=None, CORS_ALLOWED_ORIGINS="https://dash.example"),
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


@pytest_asyncio.fixture(loop_scope="session")
async def owner(db, org, keypair):
    private, _ = keypair
    subject = f"owner-{uuid.uuid4()}"
    db.add(Member(org_id=org.id, user_id=subject, email="owner@example.com", role="owner"))
    await db.commit()
    return {"Authorization": f"Bearer {token_for(private, subject=subject)}"}


def beat(**kw):
    """Heartbeat arguments with sensible defaults, so a test states only what it cares about."""
    now = datetime.now(tz=UTC)
    return {
        "worker_id": "host:1234",
        "started_at": now - timedelta(minutes=10),
        "info": {"health": {"healthy": True}},
        **kw,
    }


class TestHeartbeat:
    async def test_a_fresh_beat_is_not_stale(self, db):
        await ops.record_heartbeat(db, **beat())
        await db.commit()

        status = await ops.latest_heartbeat(db)
        assert status is not None
        assert status.stale is False
        assert status.worker_id == "host:1234"

    async def test_an_old_beat_is_stale(self, db):
        old = datetime.now(tz=UTC) - ops.HEARTBEAT_STALE_AFTER - timedelta(minutes=1)
        await ops.record_heartbeat(db, **beat(now=old))
        await db.commit()

        status = await ops.latest_heartbeat(db)
        assert status is not None
        assert status.stale is True

    async def test_the_same_worker_updates_its_row_rather_than_adding_one(self, db):
        await ops.record_heartbeat(db, **beat())
        await ops.record_heartbeat(db, **beat(info={"health": {"healthy": False}}))
        await db.commit()

        rows = await ops.prune_heartbeats(db, older_than=timedelta(seconds=-1))
        assert rows == 1  # one row, not two

    async def test_a_restart_does_not_keep_claiming_the_old_uptime(self, db):
        """The pid can be reused, and an uptime of three days for a one-minute-old process is
        a lie that would otherwise survive every restart."""
        first = datetime.now(tz=UTC) - timedelta(days=3)
        await ops.record_heartbeat(db, **beat(started_at=first))
        restarted = datetime.now(tz=UTC) - timedelta(seconds=30)
        await ops.record_heartbeat(db, **beat(started_at=restarted))
        await db.commit()

        status = await ops.latest_heartbeat(db)
        assert status is not None
        assert status.started_at > first

    async def test_the_freshest_of_several_workers_wins(self, db):
        """Readiness asks "is at least one alive", so a removed container's stale row must not
        fail readiness forever."""
        old = datetime.now(tz=UTC) - timedelta(hours=2)
        await ops.record_heartbeat(db, **beat(worker_id="gone:1", now=old))
        await ops.record_heartbeat(db, **beat(worker_id="live:2"))
        await db.commit()

        status = await ops.latest_heartbeat(db)
        assert status is not None
        assert status.worker_id == "live:2"
        assert status.stale is False

    async def test_rows_from_long_dead_processes_are_pruned(self, db):
        old = datetime.now(tz=UTC) - timedelta(days=5)
        await ops.record_heartbeat(db, **beat(worker_id="ancient:1", now=old))
        await ops.record_heartbeat(db, **beat(worker_id="live:2"))
        await db.commit()

        removed = await ops.prune_heartbeats(db, older_than=timedelta(days=2))
        await db.commit()
        assert removed == 1
        status = await ops.latest_heartbeat(db)
        assert status is not None
        assert status.worker_id == "live:2"


class TestReady:
    async def test_not_ready_when_the_worker_has_never_beaten(self, api):
        """A worker that has never run means nothing is researched, drafted or sent."""
        response = api.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        assert body["checks"]["worker"] == "no heartbeat"

    async def test_ready_when_the_database_and_the_worker_both_answer(self, api, db):
        await ops.record_heartbeat(db, **beat())
        await db.commit()

        response = api.get("/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {
            "database": "ok",
            "worker": "ok",
            "worker_seen_seconds_ago": response.json()["checks"]["worker_seen_seconds_ago"],
        }

    async def test_not_ready_when_the_worker_heartbeat_is_stale(self, api, db):
        """The whole point: an api that serves perfectly while the worker is dead is not ready,
        and an uptime monitor pinging this endpoint is how that gets noticed (§18)."""
        old = datetime.now(tz=UTC) - ops.HEARTBEAT_STALE_AFTER - timedelta(minutes=1)
        await ops.record_heartbeat(db, **beat(now=old))
        await db.commit()

        response = api.get("/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["worker"] == "stale"

    async def test_health_does_not_care_about_the_worker(self, api):
        """`/health` must not fail for somebody else's reason, or the api is restarted when the
        worker is the thing that is ill."""
        assert api.get("/health").status_code == 200

    async def test_ready_does_not_need_a_token(self, api):
        """A free uptime monitor cannot hold one."""
        assert api.get("/ready").status_code in (200, 503)

    async def test_ready_reveals_no_detail_about_the_tasks(self, api, db):
        """Unauthenticated, so it answers with booleans — never the per-task error text, which
        says how the inside of the system is put together."""
        await ops.record_heartbeat(db, **beat(info={"health": {"tasks": {"send": "secrets"}}}))
        await db.commit()
        assert "secrets" not in api.get("/ready").text


class TestAlerts:
    async def test_an_alert_is_recorded(self, db, org):
        alert = await ops.raise_alert(
            db, org_id=org.id, kind="jobs_dead", message="2 jobs are dead", severity="warning"
        )
        assert alert is not None
        assert alert.severity == "warning"

    async def test_the_same_condition_is_not_recorded_twice_inside_the_cooldown(self, db, org):
        first = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="2 dead")
        second = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="3 dead")
        assert first is not None
        assert second is None

    async def test_a_different_kind_is_not_suppressed(self, db, org):
        await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="2 dead")
        other = await ops.raise_alert(db, org_id=org.id, kind="smtp_auth_failed", message="401")
        assert other is not None

    async def test_an_acknowledged_alert_does_not_suppress_the_next_one(self, db, org):
        """A human saying "seen" means they want to be told if it happens again."""
        first = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="2 dead")
        assert first is not None
        await ops.ack_alert(db, org_id=org.id, alert_id=first.id, user_id="someone")

        again = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="3 dead")
        assert again is not None

    async def test_the_cooldown_expires(self, db, org):
        long_ago = datetime.now(tz=UTC) - timedelta(hours=3)
        await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="old", now=long_ago)
        fresh = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="new")
        assert fresh is not None

    async def test_a_system_alert_belongs_to_no_org_and_is_listed_for_everyone(self, db, org):
        """A stalled worker stops every org's work, so attributing it to one would hide it from
        the others (ADR-009)."""
        alert = await ops.raise_alert(
            db, org_id=None, kind="worker_task_stalled", message="send_loop", severity="critical"
        )
        assert alert is not None
        assert alert.org_id is None

        listed = await ops.list_alerts(db, org_id=org.id)
        assert alert.id in [row.id for row in listed]

    async def test_another_orgs_alert_is_not_listed(self, db, org):
        other = Org(name="Rival Ltd")
        db.add(other)
        await db.flush()
        theirs = await ops.raise_alert(db, org_id=other.id, kind="jobs_dead", message="theirs")
        assert theirs is not None

        listed = await ops.list_alerts(db, org_id=org.id)
        assert theirs.id not in [row.id for row in listed]

    async def test_acking_another_orgs_alert_is_a_lookup_error(self, db, org):
        other = Org(name="Rival Ltd")
        db.add(other)
        await db.flush()
        theirs = await ops.raise_alert(db, org_id=other.id, kind="jobs_dead", message="theirs")
        assert theirs is not None

        with pytest.raises(LookupError):
            await ops.ack_alert(db, org_id=org.id, alert_id=theirs.id, user_id="someone")

    async def test_an_unknown_severity_is_refused(self, db, org):
        with pytest.raises(ValueError):
            await ops.raise_alert(db, org_id=org.id, kind="x", message="y", severity="apocalyptic")

    async def test_unacked_only_filters(self, db, org):
        first = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="a")
        second = await ops.raise_alert(db, org_id=org.id, kind="smtp_auth_failed", message="b")
        assert first is not None and second is not None
        await ops.ack_alert(db, org_id=org.id, alert_id=first.id, user_id="someone")

        open_alerts = await ops.list_alerts(db, org_id=org.id, unacked_only=True)
        assert [row.id for row in open_alerts] == [second.id]


class Recorder:
    """A notifier that records instead of making a request. No test touches the network."""

    def __init__(self) -> None:
        self.pushed: list[Alert] = []

    async def __call__(self, alert: Alert) -> None:
        self.pushed.append(alert)


class TestAlertService:
    async def test_the_row_is_written_before_the_webhook_is_pushed(self, db, org):
        """The table is the record and the webhook is the nudge: an alert that exists only in a
        failed HTTP request is an alert nobody will ever see."""
        recorder = Recorder()
        alert = await raise_alert(
            db, org_id=org.id, kind="jobs_dead", message="2 dead", notifier=recorder
        )
        assert alert is not None
        assert [pushed.id for pushed in recorder.pushed] == [alert.id]

    async def test_a_suppressed_repeat_is_not_pushed(self, db, org):
        recorder = Recorder()
        await raise_alert(db, org_id=org.id, kind="jobs_dead", message="a", notifier=recorder)
        await raise_alert(db, org_id=org.id, kind="jobs_dead", message="b", notifier=recorder)
        assert len(recorder.pushed) == 1

    async def test_a_webhook_failure_does_not_fail_the_caller(self, db, org):
        """Otherwise the system stops working *because* it tried to say it was in trouble."""

        async def explode(alert: Alert) -> None:
            raise RuntimeError("webhook down")

        alert = await raise_alert(db, org_id=org.id, kind="x", message="y", notifier=explode)
        assert alert is not None  # recorded despite the push failing

    async def test_an_unconfigured_webhook_is_a_no_op(self, org):
        notifier = WebhookNotifier(Settings(_env_file=None, ALERT_WEBHOOK_URL=""))
        assert notifier.configured is False
        # Nothing is attempted, so nothing can fail and no connection is opened.
        await notifier(Alert(org_id=org.id, severity="info", kind="x", message="y"))


class TestAdminViews:
    async def test_alerts_are_listed_for_the_owner(self, api, owner, db, org):
        await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="2 jobs are dead")
        await db.commit()

        body = api.get("/api/v1/admin/alerts", headers=owner).json()
        assert [row["kind"] for row in body["alerts"]] == ["jobs_dead"]
        assert body["alerts"][0]["scope"] == "org"

    async def test_a_system_alert_is_labelled_as_such(self, api, owner, db):
        await ops.raise_alert(db, org_id=None, kind="worker_task_stalled", message="send_loop")
        await db.commit()

        body = api.get("/api/v1/admin/alerts", headers=owner).json()
        assert body["alerts"][0]["scope"] == "system"

    async def test_an_alert_can_be_acknowledged(self, api, owner, db, org):
        alert = await ops.raise_alert(db, org_id=org.id, kind="jobs_dead", message="2 dead")
        assert alert is not None
        await db.commit()

        response = api.post(f"/api/v1/admin/alerts/{alert.id}/ack", headers=owner)
        assert response.status_code == 200
        assert response.json()["acked_at"]

    async def test_acking_an_unknown_alert_is_a_404(self, api, owner):
        response = api.post(f"/api/v1/admin/alerts/{uuid.uuid4()}/ack", headers=owner)
        assert response.status_code == 404

    async def test_the_worker_view_says_so_when_nothing_has_ever_run(self, api, owner):
        body = api.get("/api/v1/admin/worker", headers=owner).json()
        assert body["running"] is False

    async def test_the_worker_view_carries_the_task_detail(self, api, owner, db):
        await ops.record_heartbeat(db, **beat(info={"health": {"tasks": {"send_loop": {}}}}))
        await db.commit()

        body = api.get("/api/v1/admin/worker", headers=owner).json()
        assert body["running"] is True
        assert "send_loop" in body["info"]["health"]["tasks"]

    async def test_the_worker_view_needs_a_token(self, api):
        """Unlike `/ready`: the per-task detail says how the system is put together."""
        assert api.get("/api/v1/admin/worker").status_code == 401
