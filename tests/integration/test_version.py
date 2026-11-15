"""`GET /version` says which build is running, without a token and without a secret (S17.3).

Four times in this project a merged pull request was read as a shipped one. Cloudflare rebuilds
the dashboard on every merge; the API is deployed by hand. The symptom is a screen calling a
route the running API does not serve, which arrives as a bare 404 -- and **a 404 cannot say
"this API is older than this screen"**, so the investigation started at DNS, CORS and
connectivity each time.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.app import create_app
from app.core.settings import Settings


def _app(**overrides: object):
    fields: dict[str, object] = {
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": "unit-test-key",
    }
    fields.update(overrides)
    return create_app(Settings(_env_file=None, **fields))  # type: ignore[call-arg]


async def _get(application, path: str):
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_it_answers_without_a_token() -> None:
    """The dashboard has to be able to ask *before* it can sign in.

    An API too old to serve a route the screen calls is the case this exists to explain, and in
    that state the dashboard may be failing against it already -- so requiring a session would
    make the diagnosis available exactly when it is not needed.
    """
    response = await _get(
        _app(BUILD_COMMIT="abc1234", BUILD_TIME="2026-11-15T10:00:00Z"), "/version"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["commit"] == "abc1234"
    assert body["built_at"] == "2026-11-15T10:00:00Z"


@pytest.mark.asyncio
async def test_an_unknown_build_says_unknown() -> None:
    """Never a guess.

    A development run bakes no build arguments, and the dashboard's comparison is only sound
    when both sides know their own build -- so `unknown` has to be reported as itself rather
    than as an empty string that reads like a value.
    """
    body = (await _get(_app(), "/version")).json()
    assert body["commit"] == "unknown"
    assert body["built_at"] == "unknown"


@pytest.mark.asyncio
async def test_it_says_when_this_process_started() -> None:
    """Which answers a question the commit cannot: did the deploy restart anything?

    A rollback that silently left the old container running reports an uptime in days beside a
    commit that looks current.
    """
    body = (await _get(_app(), "/version")).json()
    assert body["started_at"].startswith("20")


@pytest.mark.asyncio
async def test_it_reveals_nothing_else() -> None:
    """An inventory of what is installed is a list of which advisories to try.

    So this is three keys and no fourth: no dependency versions, no paths, no settings, and
    nothing read from the environment but the two values the image was built with.
    """
    response = await _get(
        _app(
            BUILD_COMMIT="abc1234",
            GROQ_API_KEY="gsk_a_real_looking_secret",
            CREDENTIAL_KEY="a-real-looking-key",
        ),
        "/version",
    )
    assert set(response.json()) == {"commit", "built_at", "started_at"}
    text = response.text
    for secret in ("gsk_a_real_looking_secret", "a-real-looking-key"):
        assert secret not in text
