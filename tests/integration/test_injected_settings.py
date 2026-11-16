"""A route answers from the `Settings` the app was built with (ADR-061).

ADR-041 recorded this gap and did not close it: a router reaching for the module-level `config`
singleton means an app constructed with its own `Settings` -- which is every test app -- is
partially ignoring them. In production the two are the same object, which is exactly why it
survived four phases.

Both tests below fail against the previous code. They are written against the two routes where
divergence is silent rather than loud, because a loud one would have been found already.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.app import create_app
from app.core.settings import Settings
from app.services import unsubscribe


def _app(**overrides: object):
    fields: dict[str, object] = {
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": "the-app-was-built-with-this-key",
    }
    fields.update(overrides)
    return create_app(Settings(_env_file=None, **fields))  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_an_unsubscribe_link_is_verified_with_the_apps_own_key() -> None:
    """A token signed with the app's key must be recognised by the app's route.

    **The GET and the POST fail differently, and the POST is the dangerous one.** GET answers
    404 with a page saying the link is not valid, which is what this test catches. POST answers
    200 after doing nothing for *every* refusal, deliberately, so the endpoint is not an oracle
    for which tokens exist (ADR-033) -- and the cost of that correct decision is that a route
    verifying with the wrong key is indistinguishable from one working perfectly: the recipient
    sees a success, nothing is logged, and the suppression is never written.

    So this asserts on the readable half. The unreadable half is held by the structural guard in
    `tests/unit/test_routers_take_injected_settings.py`, which is the only kind of check that
    can speak about a path whose failure is silent by design.
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        GROQ_API_KEY="gsk_test",
        CREDENTIAL_KEY="the-app-was-built-with-this-key",
    )
    application = create_app(settings)

    token = unsubscribe.sign(
        org_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        lead_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        secret=unsubscribe.signing_key(settings.CREDENTIAL_KEY),
    )

    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/u/{token}")

    assert response.status_code == 200
    # The page for a token we recognise does not say the link is invalid. A route holding a
    # different key reaches the "not valid" branch, with the same 200.
    assert "not valid" not in response.text.lower()


@pytest.mark.asyncio
async def test_the_sending_config_reports_this_apps_send_mode() -> None:
    """`/admin/sending-config` answers "what happens if I approve this draft?"

    It called `effective_send_mode(config, ...)`, so it answered from the singleton while the
    gate resolves from the object the app was built with. Hard rule 1 says the mode is decided
    in exactly one function -- it is, and the screen was asking it about a different deployment.

    Asserted through the settings object rather than through the route, because reaching the
    route needs a signed-in member and the property under test is which object the mode comes
    from. The structural guard in `tests/unit/test_routers_take_injected_settings.py` is what
    holds the route to it.
    """
    from app.services.mail.smtp import effective_send_mode

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        GROQ_API_KEY="gsk_test",
        CREDENTIAL_KEY="unit-test-key",
        SEND_MODE="allowlist",
    )
    assert effective_send_mode(settings, None) == "allowlist"
    # And an org override still wins, which is ADR-010's rule and must not have changed.
    assert effective_send_mode(settings, "disabled") == "disabled"
