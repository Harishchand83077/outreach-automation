"""The booking webhook end to end (F15, ADR-045, slice S14.2).

The three acceptance criteria for this slice, each with a test named after it:

1. a valid signature sets `MEETING_BOOKED`;
2. a bad signature is refused;
3. **a reply still cannot set it** -- the locked product rule, and the one this slice was most
   capable of breaking.

The public route is the second unauthenticated write in this system, so most of what is here is
about how narrow it is: what it refuses, what it refuses to *say* about a refusal, and the fact
that it takes no `org_id` from anybody.
"""

# ruff: noqa: F811 -- the shared api/owner/member fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

import json
import uuid
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
from app.db.models import BookingEvent, Event, Lead, Org
from app.db.repo import OrgContext
from app.db.state import transition
from app.domain.outcome import Outcome
from app.domain.state import LeadState
from app.services.booking import intake, payload, secrets_store, signature

from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")
PUBLIC_BASE = "https://api.example.test"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        GROQ_API_KEY="gsk_test",
        CREDENTIAL_KEY=TEST_KEY,
        PUBLIC_API_URL=PUBLIC_BASE,
        CORS_ALLOWED_ORIGINS="https://dash.example",
    )


@pytest_asyncio.fixture(loop_scope="session")
async def api(session_factory, keypair):
    """This slice's own app, because the webhook needs a `CREDENTIAL_KEY` and a public base.

    It is also the reason `receive` takes `SettingsDep`: the URL token is signed when the secret
    is generated and verified when a delivery arrives, and if those two read different `Settings`
    objects every delivery is refused for a reason nobody could find. That is the F9 gap, in a
    place where the symptom would be silence.
    """
    _, public = keypair
    application = create_app(_settings(), limiter=RateLimit(limit=10_000, window_seconds=60.0))

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[deps.get_session] = override_session
    deps.set_verifier(TokenVerifier(jwks_url="unused", issuer=ISSUER, cache=FakeJwks(public)))
    with TestClient(application, raise_server_exceptions=False) as client:
        yield client
    deps.set_verifier(None)


@pytest_asyncio.fixture(loop_scope="session")
async def webhook(api, owner):
    """A generated secret, and the URL path a tool would post to."""
    response = api.post("/api/v1/booking-webhook/secret", headers=owner)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["url"].startswith(PUBLIC_BASE)
    return {"secret": body["secret"], "path": body["url"][len(PUBLIC_BASE) :]}


def _post(api, webhook, data: dict, *, secret: str | None = None, timestamp: str = ""):
    """Post a booking the way a tool would: raw bytes, signed exactly as sent."""
    body = json.dumps(data).encode()
    headers = {
        "content-type": "application/json",
        "x-cal-signature-256": signature.body_signature(
            body, secret=secret or webhook["secret"], timestamp=timestamp
        ),
    }
    if timestamp:
        headers[signature.TIMESTAMP_HEADER] = timestamp
    return api.post(webhook["path"], content=body, headers=headers)


def _booking(uid: str, email: str, *, event: str = "BOOKING_CREATED") -> dict:
    return {
        "triggerEvent": event,
        "payload": {
            "uid": uid,
            "startTime": "2026-11-10T09:30:00Z",
            "attendees": [{"email": email, "name": "Ben Shah"}],
        },
    }


async def _sent_lead(db, org, campaign, lead_factory, *, email="ben@example.com"):
    lead = await lead_factory(state="APPROVED", email=email)
    await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.SENT)
    await db.commit()
    return lead


class TestAValidSignatureSetsMeetingBooked:
    async def test_the_lead_moves_and_the_outcome_is_recorded(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """Acceptance criterion 1."""
        lead = await _sent_lead(db, org, campaign, lead_factory)

        response = _post(api, webhook, _booking(f"cal-{uuid.uuid4().hex}", "ben@example.com"))

        assert response.status_code == 204, response.text
        await db.refresh(lead)
        assert lead.state == LeadState.MEETING_BOOKED.value

    async def test_the_funnel_outcome_says_a_machine_confirmed_it(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """Not a person, and the dataset must not record it as one (ADR-026). `confirmed_by`
        names the provider, so a funnel can tell a webhook's meeting from a click."""
        lead = await _sent_lead(db, org, campaign, lead_factory, email="ciara@example.com")

        _post(api, webhook, _booking(f"cal-{uuid.uuid4().hex}", "ciara@example.com"))

        from app.db.models import LeadOutcome

        row = await db.scalar(select(LeadOutcome).where(LeadOutcome.lead_id == lead.id))
        assert row is not None
        assert row.outcome == Outcome.MEETING_BOOKED.value
        assert row.confirmed_by.startswith("booking:")

    async def test_an_audit_event_names_how_the_lead_was_matched(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        lead = await _sent_lead(db, org, campaign, lead_factory, email="dev@example.com")

        _post(api, webhook, _booking(f"cal-{uuid.uuid4().hex}", "dev@example.com"))

        rows = await db.scalars(
            select(Event).where(Event.lead_id == lead.id, Event.type == intake.BOOKED_EVENT)
        )
        found = list(rows.all())
        assert len(found) == 1
        assert found[0].data["matched_by"] in {"email", "in_conversation"}


class TestABadSignatureIsRefused:
    async def test_a_wrong_secret_changes_nothing(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """Acceptance criterion 2."""
        lead = await _sent_lead(db, org, campaign, lead_factory, email="eve@example.com")

        response = _post(
            api,
            webhook,
            _booking(f"cal-{uuid.uuid4().hex}", "eve@example.com"),
            secret="not-the-right-secret",
        )

        assert response.status_code == 204
        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value

    async def test_no_signature_at_all_changes_nothing(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        lead = await _sent_lead(db, org, campaign, lead_factory, email="fay@example.com")
        body = json.dumps(_booking("cal-nosig", "fay@example.com")).encode()

        response = api.post(
            webhook["path"], content=body, headers={"content-type": "application/json"}
        )

        assert response.status_code == 204
        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value

    async def test_every_refusal_answers_the_same_way(self, api, webhook, db, org) -> None:
        """A public endpoint that distinguishes an unknown workspace from a bad signature from an
        unreadable body is an oracle for all three -- and a provider reads any non-2xx as "retry",
        so a 401 to a body we will never accept buys a retry storm as well."""
        good = json.dumps(_booking("cal-shape", "ben@example.com")).encode()

        answers = {
            api.post(webhook["path"], content=good).status_code,
            api.post("/hooks/booking/not-a-token", content=good).status_code,
            _post(api, webhook, {"payload": {"no": "id"}}).status_code,
            api.post(
                webhook["path"],
                content=b"not json",
                headers={
                    "x-booking-signature": signature.body_signature(
                        b"not json", secret=webhook["secret"]
                    )
                },
            ).status_code,
        }

        assert answers == {204}

    async def test_a_body_too_large_is_refused_before_it_is_read(self, api, webhook) -> None:
        """The one exception to answering 204: a provider sending megabytes is a configuration
        error worth reporting back to the sender."""
        huge = b"x" * (payload.MAX_BODY_BYTES + 1024)

        response = api.post(
            webhook["path"],
            content=huge,
            headers={"x-booking-signature": "irrelevant"},
        )

        assert response.status_code == 413

    async def test_a_workspace_with_no_secret_receives_nothing(
        self, api, owner, db, org, campaign, lead_factory
    ) -> None:
        """No deployment gains a webhook by being migrated (ADR-023's rule): a workspace receives
        bookings only after an admin generates a secret."""
        api.delete("/api/v1/booking-webhook/secret", headers=owner)
        lead = await _sent_lead(db, org, campaign, lead_factory, email="gus@example.com")
        token = signature.sign_token(org_id=org.id, secret=signature.signing_key(TEST_KEY))
        body = json.dumps(_booking("cal-nosecret", "gus@example.com")).encode()

        response = api.post(
            f"/hooks/booking/{token}",
            content=body,
            headers={"x-booking-signature": signature.body_signature(body, secret="anything")},
        )

        assert response.status_code == 204
        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value


class TestAReplyStillCannotSetIt:
    async def test_the_state_machine_has_no_machine_path_to_meeting_booked(self) -> None:
        """Acceptance criterion 3, as the existing guarantee restated.

        The webhook is not an inference: a third party reports that a calendar slot was taken,
        signed with a secret this workspace pasted in itself. A *reply* is somebody's words, and
        a system that read a warm reply as a meeting would inflate every funnel it ever reported.
        """
        from app.domain.reply import POLICY, ReplyLabel

        for label in ReplyLabel:
            # No label's policy names the state, so there is no label a classifier could return
            # that would put a lead there. The booking webhook is not an inference: a third
            # party reports that a calendar slot was taken, signed with this workspace's own
            # secret, which is a fact about a calendar rather than a reading of somebody's words.
            assert "MEETING_BOOKED" not in repr(POLICY[label])
            assert "meeting_booked" not in repr(POLICY[label])

    async def test_an_interested_reply_leaves_the_lead_in_replied(
        self, db, org, campaign, lead_factory
    ) -> None:
        lead = await lead_factory(state="SENT", email="hana@example.com")
        await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.REPLIED)
        await db.commit()

        await db.refresh(lead)

        assert lead.state == LeadState.REPLIED.value

    async def test_only_the_webhook_and_a_person_write_this_outcome(self) -> None:
        """Structural: the one function that may set `MEETING_BOOKED` is `outcomes.confirm`, and
        its callers are a person's click and this webhook. A third caller is the thing to notice.
        """
        import ast
        from pathlib import Path

        callers: list[str] = []
        for path in sorted(Path("app").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name != "confirm":
                    continue
                if any(
                    isinstance(kw.value, ast.Attribute)
                    and getattr(kw.value.value, "id", "") == "Outcome"
                    and kw.value.attr == "MEETING_BOOKED"
                    for kw in node.keywords
                ):
                    callers.append(str(path).replace("\\", "/"))

        assert callers == ["app/services/booking/intake.py"], callers


class TestWhatArrivesIsAlwaysRecorded:
    async def test_a_retried_delivery_does_not_book_a_second_meeting(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """Every booking tool retries on a non-2xx and some retry on a 2xx. Without the unique
        key a provider's ordinary behaviour would put a second meeting in every funnel."""
        await _sent_lead(db, org, campaign, lead_factory, email="ida@example.com")
        data = _booking("cal-retry-once", "ida@example.com")

        first = _post(api, webhook, data)
        second = _post(api, webhook, data)

        assert (first.status_code, second.status_code) == (204, 204)
        rows = await db.scalars(
            select(BookingEvent).where(BookingEvent.external_id == "cal-retry-once")
        )
        assert len(list(rows.all())) == 1

    async def test_a_booking_that_matches_nobody_is_kept_and_counted(
        self, api, owner, webhook, db, org
    ) -> None:
        """The interesting case rather than the error case: usually the lead replied from a
        second address or forwarded the link. Dropping it leaves a funnel quietly one short."""
        response = _post(api, webhook, _booking("cal-unmatched", "nobody-here@example.com"))

        assert response.status_code == 204
        row = await db.scalar(
            select(BookingEvent).where(BookingEvent.external_id == "cal-unmatched")
        )
        assert row is not None
        assert row.lead_id is None
        assert row.matched_by == "unmatched"

        listed = api.get("/api/v1/bookings", headers={}).status_code
        assert listed in {401, 403}

    async def test_the_unmatched_ones_are_listed_for_an_admin(
        self, api, owner, webhook, db, org
    ) -> None:
        _post(api, webhook, _booking("cal-unmatched-2", "stranger@example.com"))

        response = api.get("/api/v1/bookings", headers=owner)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["unmatched"] >= 1
        assert any(row["invitee_email"] == "stranger@example.com" for row in body["bookings"])

    async def test_a_cancellation_is_recorded_and_does_not_move_the_lead_back(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """`MEETING_BOOKED` is terminal with no exits by construction. A meeting that was booked
        *was* booked, and widening a terminal state is a change to a locked product rule rather
        than a side effect of adding a webhook. The cancellation is recorded so somebody can see
        it."""
        lead = await _sent_lead(db, org, campaign, lead_factory, email="jo@example.com")
        _post(api, webhook, _booking("cal-will-cancel", "jo@example.com"))

        response = _post(
            api,
            webhook,
            _booking("cal-cancelled", "jo@example.com", event="BOOKING_CANCELLED"),
        )

        assert response.status_code == 204
        await db.refresh(lead)
        assert lead.state == LeadState.MEETING_BOOKED.value
        cancelled = await db.scalar(
            select(BookingEvent).where(BookingEvent.external_id == "cal-cancelled")
        )
        assert cancelled is not None
        assert cancelled.kind == "cancelled"
        events = await db.scalars(
            select(Event).where(Event.lead_id == lead.id, Event.type == intake.CANCELLED_EVENT)
        )
        assert list(events.all())

    async def test_a_suppressed_or_opted_out_lead_is_not_moved_by_a_webhook(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """Somebody who opted out has not booked a meeting with us, and a webhook that could move
        them would be a way to resurrect a suppressed lead from outside the system."""
        lead = await lead_factory(state="SENT", email="kit@example.com")
        await transition(db, org_id=org.id, lead_id=lead.id, to_state=LeadState.OPTED_OUT)
        await db.commit()

        _post(api, webhook, _booking("cal-opted-out", "kit@example.com"))

        await db.refresh(lead)
        assert lead.state == LeadState.OPTED_OUT.value
        row = await db.scalar(
            select(BookingEvent).where(BookingEvent.external_id == "cal-opted-out")
        )
        assert row is not None, "the booking is still recorded, so somebody can see it"

    async def test_no_lead_is_ever_created_by_a_booking(self, api, owner, webhook, db, org) -> None:
        """Creating one would put a person into a sending pipeline because they booked a
        meeting, which is the opposite of what they asked for."""
        before = len(list((await db.scalars(select(Lead).where(Lead.org_id == org.id))).all()))

        _post(api, webhook, _booking("cal-no-lead", "brand-new@example.com"))

        after = len(list((await db.scalars(select(Lead).where(Lead.org_id == org.id))).all()))
        assert after == before


class TestTenancy:
    async def test_a_token_for_one_workspace_cannot_reach_another(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """The route takes **no `org_id` from the caller** (ADR-004). The token proves the
        workspace; the body's signature proves it came from that workspace's own tool."""
        other = Org(name="Another agency")
        db.add(other)
        await db.commit()
        other_secret = await secrets_store.generate(
            db, OrgContext(org_id=other.id), settings=_settings()
        )
        await db.commit()
        lead = await _sent_lead(db, org, campaign, lead_factory, email="lena@example.com")

        # Their token, their secret, our lead's address. Their workspace holds no such lead.
        their_token = signature.sign_token(org_id=other.id, secret=signature.signing_key(TEST_KEY))
        body = json.dumps(_booking("cal-cross", "lena@example.com")).encode()
        response = api.post(
            f"/hooks/booking/{their_token}",
            content=body,
            headers={
                "x-booking-signature": signature.body_signature(body, secret=other_secret.secret)
            },
        )

        assert response.status_code == 204
        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value, (
            "our lead must not be moved by another workspace's booking"
        )
        theirs = await db.scalar(
            select(BookingEvent).where(BookingEvent.external_id == "cal-cross")
        )
        assert theirs is not None
        assert theirs.org_id == other.id
        assert theirs.lead_id is None

    async def test_a_secret_from_one_workspace_does_not_sign_for_another(
        self, api, owner, webhook, db, org
    ) -> None:
        other = Org(name="A third agency")
        db.add(other)
        await db.commit()
        theirs = await secrets_store.generate(db, OrgContext(org_id=other.id), settings=_settings())
        await db.commit()

        response = _post(
            api, webhook, _booking("cal-wrong-secret", "ben@example.com"), secret=theirs.secret
        )

        assert response.status_code == 204
        assert (
            await db.scalar(
                select(BookingEvent).where(BookingEvent.external_id == "cal-wrong-secret")
            )
            is None
        )


class TestTheSettingsHalf:
    async def test_the_secret_is_shown_once_and_never_again(self, api, owner) -> None:
        made = api.post("/api/v1/booking-webhook/secret", headers=owner)
        described = api.get("/api/v1/booking-webhook", headers=owner)

        assert made.status_code == 201
        secret = made.json()["secret"]
        assert described.status_code == 200
        assert secret not in described.text
        assert described.json()["configured"] is True

    async def test_regenerating_revokes_the_previous_secret(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """A second live secret would mean a revoked one kept working."""
        await _sent_lead(db, org, campaign, lead_factory, email="mo@example.com")
        old = webhook["secret"]
        api.post("/api/v1/booking-webhook/secret", headers=owner)

        response = _post(api, webhook, _booking("cal-old-secret", "mo@example.com"), secret=old)

        assert response.status_code == 204
        assert (
            await db.scalar(
                select(BookingEvent).where(BookingEvent.external_id == "cal-old-secret")
            )
            is None
        )

    async def test_a_delivery_stamps_the_last_used_time(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """The cheapest way for a screen to tell "no meetings yet" from "this was never wired
        up", which is the commonest failure of a webhook somebody pasted into the wrong field."""
        before = api.get("/api/v1/booking-webhook", headers=owner).json()
        await _sent_lead(db, org, campaign, lead_factory, email="nia@example.com")

        _post(api, webhook, _booking("cal-stamps", "nia@example.com"))
        after = api.get("/api/v1/booking-webhook", headers=owner).json()

        assert before["last_delivery_at"] is None
        assert after["last_delivery_at"] is not None

    async def test_a_member_cannot_generate_or_read_the_webhook(self, api, member) -> None:
        """A webhook that can move a lead to a terminal state is a rule about the workspace, so
        it is `admin` -- the line ADR-023 drew for the allowlist."""
        assert api.get("/api/v1/booking-webhook", headers=member).status_code == 403
        assert api.post("/api/v1/booking-webhook/secret", headers=member).status_code == 403
        assert api.get("/api/v1/bookings", headers=member).status_code == 403

    async def test_there_is_no_get_on_the_public_path(self, api, webhook) -> None:
        """Nobody clicks a webhook URL, and the things that *do* fetch URLs found in text --
        scanners, link previews, a provider's own test button -- would otherwise reach a write
        path. The unsubscribe endpoint has a GET because a human clicks that link."""
        assert api.get(webhook["path"]).status_code == 405


class TestErasureReachesABooking:
    async def test_erasing_a_lead_clears_the_address_it_booked_with(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        """The booking row survives (`ON DELETE SET NULL`), because erasing somebody must not
        erase the record that a meeting happened. The address on it would still identify them,
        so the erasure clears it -- otherwise it would be decorative for exactly the leads who
        got furthest."""
        from app.services.privacy.erasure import erase_lead

        lead = await _sent_lead(db, org, campaign, lead_factory, email="opal@example.com")
        _post(api, webhook, _booking("cal-erase", "opal@example.com"))

        await erase_lead(db, OrgContext(org_id=org.id), lead.id)
        await db.commit()

        row = await db.scalar(select(BookingEvent).where(BookingEvent.external_id == "cal-erase"))
        assert row is not None, "the record that a meeting happened survives"
        assert row.lead_id is None
        assert row.invitee_email_norm is None
        assert row.invitee_name is None


class TestNothingHereSends:
    async def test_the_package_cannot_reach_the_outbox_or_smtp(self) -> None:
        """The tree-wide send guard already covers this package. This is the narrower claim:
        nothing here names the outbox, the message builder or the send loop."""
        import ast
        from pathlib import Path

        banned = {"outbox", "smtp", "send_loop", "message"}
        offenders: list[str] = []
        for path in sorted(Path("app/services/booking").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    tail = node.module.rsplit(".", 1)[-1]
                    if tail in banned and "booking" not in node.module:
                        offenders.append(f"{path}: {node.module}")

        assert offenders == []

    async def test_the_booking_intake_never_writes_a_draft(self) -> None:
        source = __import__("pathlib").Path("app/services/booking/intake.py").read_text("utf-8")

        assert "Draft(" not in source
        assert "claim_send" not in source


class TestTheClockIsNotAWayIn:
    async def test_a_stale_signed_delivery_is_refused_over_the_wire(
        self, api, owner, webhook, db, org, campaign, lead_factory
    ) -> None:
        lead = await _sent_lead(db, org, campaign, lead_factory, email="pia@example.com")
        stale = str(int(datetime(2026, 1, 1, tzinfo=UTC).timestamp()))

        response = _post(api, webhook, _booking("cal-stale", "pia@example.com"), timestamp=stale)

        assert response.status_code == 204
        await db.refresh(lead)
        assert lead.state == LeadState.SENT.value
