"""A lapsed Gmail connection must not read as connected (ADR-018, ADR-050).

Reported by the owner: the alerts said the connection had expired, and the campaign screen and
the sending-account panel both still said **connected**.

They were both right about what they were computing. `invalid_reason` is set only once a
provider has *refused* a credential, so a grant that lapsed on Friday and has not been used
since carries no reason at all -- and every reader took that to mean the mailbox worked, while
`grant_status` separately knew it had nothing left. Two correct functions answering adjacent
questions, with the screens asking the wrong one.

The same shape as the pool blocker that reported nothing for a *paused* mailbox, and as the
platform router counting `direction == "outbound"` against a column holding `'out'`.
"""

# ruff: noqa: F811 -- the shared api/owner fixtures are imported and then used as test
# parameters, which is how every integration file in this directory does it.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
from app.db.models import Mailbox, MailCredential
from app.services.mail.connection import connection_status, unavailable_reason

from .conftest import connect_mailbox
from .test_api_settings import ISSUER, FakeJwks, keypair, member, owner, token_for  # noqa: F401

pytestmark = pytest.mark.asyncio(loop_scope="session")

TEST_KEY = Fernet.generate_key().decode("ascii")


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": TEST_KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


@pytest_asyncio.fixture(loop_scope="session")
async def mailbox(db, org):
    row = Mailbox(
        org_id=org.id,
        from_name="Asha Rao",
        from_email="asha@outreach.example",
        reply_to="asha@outreach.example",
        daily_cap=100,
    )
    db.add(row)
    await db.commit()
    return row


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


async def _google_credential(db, mailbox, *, granted_days_ago: float):
    """A Gmail connection consented to that long ago, and refused by nobody since."""
    credential = await db.scalar(
        select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
    )
    if credential is None:
        # One credential per mailbox, so connecting twice is a unique violation rather than a
        # second row -- the schema being right, and this helper having to know it.
        await connect_mailbox(db, mailbox)
        credential = await db.scalar(
            select(MailCredential).where(MailCredential.mailbox_id == mailbox.id)
        )
    assert credential is not None
    credential.kind = "google_oauth"
    credential.granted_at = datetime.now(tz=UTC) - timedelta(days=granted_days_ago)
    # The whole point: nothing has refused it, because nothing has tried since it lapsed.
    credential.invalid_reason = None
    await db.commit()
    return credential


class TestTheOneFunctionEveryReaderAsks:
    async def test_a_lapsed_grant_is_a_reason_to_be_unavailable(self, db, mailbox) -> None:
        await _google_credential(db, mailbox, granted_days_ago=9)

        reason = await unavailable_reason(db, mailbox=mailbox)

        assert reason is not None
        assert "expired" in reason.lower()

    async def test_a_fresh_grant_is_not(self, db, mailbox) -> None:
        """The guard must refuse a lapsed grant and nothing else, or it has broken sending for
        every workspace that reconnected this morning."""
        await _google_credential(db, mailbox, granted_days_ago=1)

        assert await unavailable_reason(db, mailbox=mailbox) is None

    async def test_a_grant_with_hours_left_still_works(self, db, mailbox) -> None:
        """`days_left` rounds up, so a connection with eleven hours left reports 1 and is not
        expired. A countdown that reaches zero while the thing still works is one nobody
        believes the next time it says zero."""
        await _google_credential(db, mailbox, granted_days_ago=6.5)

        assert await unavailable_reason(db, mailbox=mailbox) is None

    async def test_an_app_password_never_expires(self, db, mailbox) -> None:
        """Only an unpublished OAuth client's refresh token lapses. An app password that stopped
        working for this reason would be a refusal we invented."""
        credential = await _google_credential(db, mailbox, granted_days_ago=400)
        credential.kind = "app_password"
        await db.commit()

        assert await unavailable_reason(db, mailbox=mailbox) is None

    async def test_publishing_the_client_stops_it(self, db, mailbox) -> None:
        """`GOOGLE_GRANT_DAYS = 0` means a published client, where nothing expires. A
        configuration change, not a code change (ADR-018)."""
        await _google_credential(db, mailbox, granted_days_ago=400)

        reason = await unavailable_reason(
            db, mailbox=mailbox, settings=_settings(GOOGLE_GRANT_DAYS=0)
        )

        assert reason is None


class TestTheRowCannotContradictItself:
    async def test_mailboxes_reports_a_lapsed_grant_as_needing_a_reconnect(
        self, api, owner, db, mailbox
    ) -> None:
        """**The defect as reported.** `connected: true` beside `days_left: 0` and an
        `expiry_notice` saying it had expired."""
        await _google_credential(db, mailbox, granted_days_ago=9)

        row = next(
            item
            for item in api.get("/api/v1/mailboxes", headers=owner).json()["mailboxes"]
            if item["id"] == str(mailbox.id)
        )

        assert row["connected"] is False
        assert row["needs_reconnect"] is True
        assert row["usable"] is False
        assert row["days_left"] == 0
        assert "expired" in (row["expiry_notice"] or "").lower()

    async def test_connected_and_needs_reconnect_are_never_both_true(
        self, api, owner, db, mailbox
    ) -> None:
        """The property, rather than the instance: whatever the state, the row agrees with
        itself. They are two views of one answer now, so this cannot be arranged to fail."""
        for days in (1, 6.5, 9, 400):
            await _google_credential(db, mailbox, granted_days_ago=days)

            row = next(
                item
                for item in api.get("/api/v1/mailboxes", headers=owner).json()["mailboxes"]
                if item["id"] == str(mailbox.id)
            )

            assert row["connected"] != row["needs_reconnect"], days
            assert row["usable"] == row["connected"], days

    async def test_the_campaign_panel_agrees(self, api, owner, db, mailbox) -> None:
        """`/connect/google/status` is what the campaign screen's sending-account panel reads,
        and it was computing `connected` from `invalid_reason` too."""
        await _google_credential(db, mailbox, granted_days_ago=9)

        body = api.get(
            f"/api/v1/connect/google/status?mailbox_id={mailbox.id}", headers=owner
        ).json()

        assert body["connected"] is False
        assert body["needs_reconnect"] is True
        assert body["days_left"] == 0


class TestAProvidersWordsAreNeverEchoed:
    async def test_the_status_route_does_not_return_invalid_reason(
        self, api, owner, db, mailbox
    ) -> None:
        """`invalid_reason` can quote a provider's own reply, and this response reaches a
        browser, a screenshot and anything that logs a page. Every other route carries a boolean
        instead; this one was still echoing the text."""
        credential = await _google_credential(db, mailbox, granted_days_ago=1)
        credential.invalid_reason = "Google said: token has been expunged, trace id 42"
        await db.commit()

        response = api.get(f"/api/v1/connect/google/status?mailbox_id={mailbox.id}", headers=owner)

        assert response.status_code == 200, response.text
        assert "expunged" not in response.text
        assert "trace id 42" not in response.text
        assert response.json()["needs_reconnect"] is True

    async def test_connection_status_carries_no_reason_field_at_all(self, db, mailbox) -> None:
        credential = await _google_credential(db, mailbox, granted_days_ago=1)
        credential.invalid_reason = "a provider's own sentence"
        await db.commit()

        status = await connection_status(db, mailbox_id=mailbox.id)

        assert "reason" not in status
        assert status["connected"] is False
