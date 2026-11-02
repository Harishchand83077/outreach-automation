"""Three contact providers, on the user's own key, called only when somebody presses a button.

**What this is not.** It is not a scraper and it is not a contact database. Hard rule 4 forbids
fetching LinkedIn, and the owner's instruction for this phase goes further: no scraping of a
data provider's own site either. Every call here is to a documented JSON API, authenticated with
a key the user pasted, and it runs on an explicit request. There is no scheduler pass and no job
kind that could call one -- `tests/unit/test_contacts_button_press_only.py` asserts that.

**Each provider's response shape is read in exactly one function**, and that is the design
decision rather than an accident of layout. The job-post feeds taught this expensively: an
unknown source whose payload happened to have the right shape returned *zero postings*, which
is indistinguishable from an empty board. So a response this code cannot read raises
`ProviderUnreadable` and says which provider and what was missing, rather than returning an
empty list that reads as "nobody works there".

**What is verified and what is assumed is recorded in `docs/CONTACT_PROVIDERS.md`**, in the same
spirit as `docs/MCP_PLAN.md` and `docs/ATS_FEEDS.md`. None of these three can be called without
a paid account, so unlike the ATS feeds they were **not** live-tested; the shapes come from each
provider's documentation. The fixtures in `tests/fixtures/contacts/` are therefore written from
documentation, not captured, and the tests say so.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20.0

#: Contacts one lookup will return. A provider page is 100; nobody reviews 100 strangers, and a
#: larger page costs more credits for results that go straight in the bin.
MAX_RESULTS = 10


class ProviderError(Exception):
    """A provider refused, was unreachable, or answered an error.

    The message is written to be shown to the user and **never quotes the key**.
    """


class ProviderUnreadable(ProviderError):
    """The provider answered 200 with something this code cannot read.

    Its own class because the operator action is different: a refusal means fix the key or the
    plan, and this means the provider changed its response and one function needs correcting. A
    shape change reported as "no contacts found" is a silent wrong answer.
    """


class ProviderUnauthorised(ProviderError):
    """The key is wrong, expired, or out of credit. Recorded as `invalid_reason`."""


@dataclass(frozen=True)
class FoundContact:
    """One proposed contact, in our own vocabulary rather than any provider's."""

    full_name: str | None
    title: str | None
    email: str | None
    #: verified | guess | unknown. See `CONTACT_EMAIL_STATUSES` -- a guess is a naming pattern,
    #: not a finding, and mailing one is how a bounce rate rises.
    email_status: str
    confidence: int | None
    profile_url: str | None


class ContactProvider(Protocol):
    """What a provider must do. One method, because one thing is being asked."""

    name: str

    async def find(
        self, *, domain: str, role_hint: str | None, client: httpx.AsyncClient
    ) -> list[FoundContact]: ...


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, number))


def _clip(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


async def _json(
    client: httpx.AsyncClient,
    provider: str,
    request: httpx.Request,
) -> dict[str, Any]:
    """Send one request and return its JSON object, or raise something actionable.

    Every refusal is turned into a sentence naming the provider and what to do. A bare
    `httpx.HTTPStatusError` reaching a route would escape as a 500, which FastAPI handles
    *outside* the CORS middleware -- the trap that once made a duplicate key look like an
    unreachable backend.
    """
    try:
        response = await client.send(request)
    except httpx.HTTPError as exc:
        raise ProviderError(f"could not reach {provider}: {type(exc).__name__}") from exc

    if response.status_code in (401, 403):
        raise ProviderUnauthorised(
            f"{provider} refused the key for this workspace. Check it in {provider}'s own "
            "dashboard, and that the plan includes API access."
        )
    if response.status_code == 429:
        raise ProviderError(
            f"{provider} is rate limiting this key. Wait and press the button again."
        )
    if response.status_code >= 400:
        raise ProviderError(f"{provider} answered HTTP {response.status_code}.")

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderUnreadable(f"{provider} did not answer with JSON.") from exc
    if not isinstance(payload, dict):
        raise ProviderUnreadable(f"{provider} answered a {type(payload).__name__}, not an object.")
    return payload


class HunterProvider:
    """Hunter's Domain Search.

    `GET https://api.hunter.io/v2/domain-search`, contacts under `data.emails[]`, each with
    `value`, `confidence`, `first_name`, `last_name` and `position`.

    **The key goes in a header, not the query string.** Hunter's documentation shows `api_key` as
    a query parameter, and a credential in a URL is a credential in an access log, a proxy log
    and a browser history. `X-API-KEY` is accepted, and if a deployment ever finds it is not, the
    fix belongs here rather than in a decision to put the key in the URL.
    """

    name = "hunter"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def find(
        self, *, domain: str, role_hint: str | None, client: httpx.AsyncClient
    ) -> list[FoundContact]:
        params: dict[str, str] = {"domain": domain, "limit": str(MAX_RESULTS)}
        if role_hint:
            params["department"] = role_hint
        request = client.build_request(
            "GET",
            "https://api.hunter.io/v2/domain-search",
            params=params,
            headers={"X-API-KEY": self._key, "Accept": "application/json"},
        )
        payload = await _json(client, self.name, request)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderUnreadable("hunter answered without a `data` object.")
        rows = data.get("emails")
        if not isinstance(rows, list):
            raise ProviderUnreadable("hunter answered without `data.emails`.")

        found: list[FoundContact] = []
        for row in rows[:MAX_RESULTS]:
            if not isinstance(row, dict):
                continue
            names = [row.get("first_name"), row.get("last_name")]
            full = " ".join(str(part) for part in names if part) or None
            # Hunter reports `verification.status`; absent it, the address is a pattern it
            # inferred from the domain's naming convention, which is a guess and is labelled one.
            verification = row.get("verification")
            status = (
                str((verification or {}).get("status", "")).lower()
                if isinstance(verification, dict)
                else ""
            )
            found.append(
                FoundContact(
                    full_name=_clip(full, 300),
                    title=_clip(row.get("position"), 300),
                    email=_clip(row.get("value"), 320),
                    email_status="verified" if status == "valid" else "guess",
                    confidence=_as_int(row.get("confidence")),
                    profile_url=_clip(row.get("linkedin"), 2000),
                )
            )
        return found


class ApolloProvider:
    """Apollo's people search.

    `POST https://api.apollo.io/api/v1/mixed_people/search` with the key in `X-Api-Key`.

    **Two things about this one are recorded rather than smoothed over.** The path is reported
    inconsistently by the community -- `/mixed_people/search` and `/mixed_people/api_search` both
    appear, and Apollo's own reference was not reached -- so the path is a module constant and
    `docs/CONTACT_PROVIDERS.md` says it is unverified. And Apollo's search deliberately returns
    **no email addresses** on most plans: revealing one is a separate enrichment call that spends
    a credit. So this returns people with `email=None` and `email_status="unknown"`, which the
    dashboard shows as a name and a title to decide about. A provider that returns no address is
    still useful; one that *pretends* to is how a guess gets mailed.
    """

    name = "apollo"
    ENDPOINT = "https://api.apollo.io/api/v1/mixed_people/search"

    def __init__(self, api_key: str) -> None:
        self._key = api_key

    async def find(
        self, *, domain: str, role_hint: str | None, client: httpx.AsyncClient
    ) -> list[FoundContact]:
        body: dict[str, Any] = {
            "q_organization_domains": domain,
            "page": 1,
            "per_page": MAX_RESULTS,
        }
        if role_hint:
            body["person_titles"] = [role_hint]
        request = client.build_request(
            "POST",
            self.ENDPOINT,
            json=body,
            headers={
                "X-Api-Key": self._key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        payload = await _json(client, self.name, request)

        rows = payload.get("people")
        if not isinstance(rows, list):
            raise ProviderUnreadable("apollo answered without a `people` list.")

        found: list[FoundContact] = []
        for row in rows[:MAX_RESULTS]:
            if not isinstance(row, dict):
                continue
            email = _clip(row.get("email"), 320)
            # Apollo returns the literal string "email_not_unlocked@domain.com" for a contact
            # whose address needs an enrichment credit. Stored as no address at all: a
            # placeholder in an email column is an address something will eventually try to mail.
            if email and "email_not_unlocked" in email:
                email = None
            found.append(
                FoundContact(
                    full_name=_clip(row.get("name"), 300),
                    title=_clip(row.get("title"), 300),
                    email=email,
                    email_status="verified" if email else "unknown",
                    confidence=None,
                    profile_url=_clip(row.get("linkedin_url"), 2000),
                )
            )
        return found


class SnovProvider:
    """Snov's domain search, behind a client-credentials token exchange.

    `POST https://api.snov.io/v1/oauth/access_token` for a one-hour Bearer token, then
    `GET https://api.snov.io/v2/domain-emails-with-info`.

    **Two calls per lookup, and no token is cached.** Caching would mean a second secret with a
    lifetime, in a table, that has to be invalidated when the credential is revoked -- for a
    button somebody presses a handful of times a day. The extra round trip is cheaper than the
    thing that goes wrong when a cached token outlives the key it came from.

    Snov also documents an asynchronous start-and-poll flow for domain search, and the sources
    disagree about which is current. The synchronous endpoint is used here and
    `docs/CONTACT_PROVIDERS.md` records the disagreement; a start-and-poll variant would be a
    second method on this class, not a change anywhere else.
    """

    name = "snov"
    # S105 below fires on the words "access_token" in the path. It is a public
    # URL, and the thing worth checking is that no *key* is in a URL anywhere here --
    # `test_the_key_travels_in_a_header_and_not_in_the_url` is that check.
    TOKEN_URL = "https://api.snov.io/v1/oauth/access_token"  # noqa: S105
    SEARCH_URL = "https://api.snov.io/v2/domain-emails-with-info"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self._id = client_id
        self._secret = client_secret

    async def _token(self, client: httpx.AsyncClient) -> str:
        request = client.build_request(
            "POST",
            self.TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": self._id,
                "client_secret": self._secret,
            },
            headers={"Accept": "application/json"},
        )
        payload = await _json(client, self.name, request)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            # Snov answers 200 with an error body for bad credentials, so this *is* the
            # unauthorised case rather than an unreadable one.
            raise ProviderUnauthorised(
                "snov did not return an access token for this client id and secret."
            )
        return token

    async def find(
        self, *, domain: str, role_hint: str | None, client: httpx.AsyncClient
    ) -> list[FoundContact]:
        token = await self._token(client)
        request = client.build_request(
            "GET",
            self.SEARCH_URL,
            params={"domain": domain, "type": "all", "limit": str(MAX_RESULTS)},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        payload = await _json(client, self.name, request)

        rows = payload.get("emails")
        if not isinstance(rows, list):
            raise ProviderUnreadable("snov answered without an `emails` list.")

        found: list[FoundContact] = []
        for row in rows[:MAX_RESULTS]:
            if not isinstance(row, dict):
                continue
            names = [row.get("firstName"), row.get("lastName")]
            full = " ".join(str(part) for part in names if part) or None
            position = row.get("position")
            if position is None:
                jobs = row.get("jobs")
                if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict):
                    position = jobs[0].get("position")
            status = str(row.get("status") or row.get("emailStatus") or "").lower()
            found.append(
                FoundContact(
                    full_name=_clip(full, 300),
                    title=_clip(position, 300),
                    email=_clip(row.get("email"), 320),
                    email_status="verified" if status == "valid" else "guess",
                    confidence=None,
                    profile_url=_clip(row.get("sourcePage"), 2000),
                )
            )
        if role_hint:
            # Snov has no department filter on this endpoint, so the hint is applied here rather
            # than silently ignored -- a filter the caller asked for and did not get is worse
            # than one that was never offered.
            needle = role_hint.lower()
            narrowed = [one for one in found if one.title and needle in one.title.lower()]
            if narrowed:
                found = narrowed
        return found


class MockProvider:
    """The provider the tests and a key-less deployment use.

    It exists in `CONTACT_PROVIDERS` and in the check constraint on purpose: the owner holds no
    provider key, so without this the whole feature would be unreachable and untestable, and the
    first time it ran would be on somebody's paid account. It returns one verified address and
    one guess, which is exactly the pair the review screen has to tell apart.
    """

    name = "mock"

    def __init__(self, api_key: str = "mock") -> None:
        self._key = api_key

    async def find(
        self, *, domain: str, role_hint: str | None, client: httpx.AsyncClient
    ) -> list[FoundContact]:
        del client  # No network, which is the point.
        title = role_hint or "Head of Engineering"
        return [
            FoundContact(
                full_name="Asha Rao",
                title=title,
                email=f"asha@{domain}",
                email_status="verified",
                confidence=95,
                profile_url=None,
            ),
            FoundContact(
                full_name="Vikram Shah",
                title="Engineering Manager",
                email=f"vikram.shah@{domain}",
                email_status="guess",
                confidence=52,
                profile_url=None,
            ),
        ]


def build(provider: str, secret: str) -> ContactProvider:
    """The adapter for a stored credential.

    `secret` is the plaintext just unsealed, so this function is as close to the network as the
    key ever gets. For `snov` it is a JSON object holding the client id and secret, because one
    credential with a lifetime is one thing to rotate rather than two.
    """
    if provider == "hunter":
        return HunterProvider(secret)
    if provider == "apollo":
        return ApolloProvider(secret)
    if provider == "mock":
        return MockProvider(secret)
    if provider == "snov":
        try:
            parsed = json.loads(secret)
            return SnovProvider(str(parsed["client_id"]), str(parsed["client_secret"]))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError(
                "the stored snov credential is not a client id and secret. Save it again."
            ) from exc
    raise ProviderError(f"{provider} is not a provider this system can call.")
