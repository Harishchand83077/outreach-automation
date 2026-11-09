"""How each provider is connected, declared once (F9/F17, ADR-060). No I/O (§4).

**The honest version of "connect with one click".** Asked for directly: a button that redirects
to the provider, comes back, and saves the credential by itself. That is OAuth, and whether it
is available is the provider's decision rather than ours:

- **Google** already works that way (ADR-018), because Google offers it to anybody.
- **Apollo** offers an authorization-code flow, but **only to approved partners** -- the client
  id and secret come from their developer portal after they approve the integration. So the
  redirect button is one catalogue entry and one token exchange away, and the thing standing in
  the way is an application to Apollo, not code.
- **Hunter** has no OAuth at all. Its documentation says authentication is an API key added to
  every call. There is no redirect flow to build.
- **Snov** authenticates with a client id and secret exchanged for a token. That is OAuth's
  `client_credentials` grant, which has no redirect and no consent screen: the user still pastes
  two values, so it is a key by any other name and is declared as one here.

**So the catalogue declares the method rather than the screen assuming it**, which is the whole
point of this module. Adding Apollo's redirect later is a changed `method` and an adapter -- not
a second screen, not a parallel concept, and nothing to keep in step. Building a fake redirect
for a key-based provider would be theatre: it would look like a connection and still be a paste.

What *is* available today for a key provider is removing the part that actually wastes people's
time, which is not the pasting. It is not knowing which page of the provider's site creates the
key. So every key entry carries a deep link straight to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Method = Literal["oauth", "key"]


@dataclass(frozen=True)
class Connection:
    """How one provider is connected, and what a person needs to know to do it."""

    provider: str
    label: str
    #: What this provider is for, in one line, because a list of five names tells nobody which
    #: one to connect.
    purpose: str
    method: Method
    #: The page on the provider's own site that creates the credential. **The one thing that
    #: actually saves time**: the pasting is ten seconds and finding this page is ten minutes.
    #: Empty for an OAuth provider, which has no such page.
    key_page: str = ""
    #: Snov issues a client id *and* a secret. Declared rather than inferred from the provider
    #: name, so the form asks for what the provider actually wants.
    needs_client_id: bool = False
    #: What the free tier is, where there is one. Stated because somebody deciding which
    #: provider to connect is mostly deciding what it will cost them.
    free_tier: str = ""
    #: Why there is no redirect button, when there is not. **Written down rather than left as a
    #: silence**, because "why can I connect Gmail in one click but not this?" is a reasonable
    #: question with a real answer.
    oauth_note: str = ""

    @property
    def one_click(self) -> bool:
        return self.method == "oauth"


#: Every provider this deployment can connect, contact and search alike. One tuple, because the
#: screen, the route and the tests must not be able to disagree about what exists.
CONNECTIONS: tuple[Connection, ...] = (
    Connection(
        provider="apollo",
        label="Apollo",
        purpose="Find people at a company, with a verified work address where they have one.",
        method="key",
        key_page="https://app.apollo.io/#/settings/integrations/api",
        free_tier="A free plan with a monthly credit allowance.",
        oauth_note=(
            "Apollo does offer a one-click authorisation flow, but only to approved partners: "
            "the credentials for it come from their developer portal after they approve an "
            "integration. Apply for that and this becomes a button -- the code behind it is "
            "one catalogue entry and one token exchange."
        ),
    ),
    Connection(
        provider="hunter",
        label="Hunter",
        purpose="Find and verify work email addresses for a domain.",
        method="key",
        key_page="https://hunter.io/api-keys",
        free_tier="A free plan includes an API key with a monthly search allowance.",
        oauth_note=(
            "Hunter has no authorisation flow to offer. Its documentation describes a key added "
            "to every call and nothing else, so there is no button to build."
        ),
    ),
    Connection(
        provider="snov",
        label="Snov",
        purpose="Find people and verify addresses, as an alternative to the two above.",
        method="key",
        key_page="https://app.snov.io/api-setting",
        needs_client_id=True,
        free_tier="A free trial allowance, then paid.",
        oauth_note=(
            "Snov exchanges a client id and secret for a token. That is OAuth's machine-to-"
            "machine grant, which has no consent screen and no redirect -- you still paste two "
            "values, so it is a key here by any other name."
        ),
    ),
    Connection(
        provider="tavily",
        label="Tavily",
        purpose="Search the web for what a company has been doing lately.",
        method="key",
        key_page="https://app.tavily.com/home",
        free_tier="1,000 credits a month on the free plan.",
        oauth_note="Tavily issues API keys and offers no authorisation flow.",
    ),
    Connection(
        provider="brave",
        label="Brave Search",
        purpose="A second search provider, tried when the first is out of credit.",
        method="key",
        key_page="https://api-dashboard.search.brave.com/app/keys",
        free_tier="A free tier with a monthly query allowance.",
        oauth_note="Brave issues API keys and offers no authorisation flow.",
    ),
    Connection(
        provider="serper",
        label="Serper",
        purpose="A third search provider, for the same reason as the second.",
        method="key",
        key_page="https://serper.dev/api-key",
        free_tier="A free allowance of queries to start with.",
        oauth_note="Serper issues API keys and offers no authorisation flow.",
    ),
    Connection(
        provider="mock",
        label="Mock",
        purpose="A fake provider that answers without calling anybody. For trying the flow.",
        method="key",
        key_page="",
        free_tier="Free, because it is not real.",
        oauth_note="Nothing to authorise: it never leaves this machine.",
    ),
)

_BY_PROVIDER = {entry.provider: entry for entry in CONNECTIONS}


def for_provider(provider: str) -> Connection | None:
    return _BY_PROVIDER.get((provider or "").strip().lower())


def describe(provider: str) -> dict[str, object]:
    """What the screen needs to render a connect card. Empty dict for an unknown provider.

    Returned per provider rather than as one blob, so a provider added to the adapters without
    a catalogue entry shows as unconnectable rather than as a card with blank fields -- and
    `test_connection_catalogue.py` fails before anybody sees it.
    """
    entry = for_provider(provider)
    if entry is None:
        return {}
    return {
        "label": entry.label,
        "purpose": entry.purpose,
        "method": entry.method,
        "one_click": entry.one_click,
        "key_page": entry.key_page,
        "needs_client_id": entry.needs_client_id,
        "free_tier": entry.free_tier,
        "oauth_note": entry.oauth_note,
    }
