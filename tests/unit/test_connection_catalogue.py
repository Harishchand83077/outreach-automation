"""The catalogue and the providers cannot disagree about what exists (ADR-060).

Two lists that must match is this project's signature defect waiting to happen, and it has been
caught here nine times. `ALL_PROVIDERS` is what the database's check constraint is built from;
`CONNECTIONS` is what the screen renders. A provider in one and not the other is either a card
nobody can fill in or a key nobody can be told where to get.
"""

from __future__ import annotations

from app.db.models import ALL_PROVIDERS, CONTACT_PROVIDERS
from app.domain import connections


def test_every_provider_has_a_connection_entry() -> None:
    """Otherwise the screen shows a provider with no label, no purpose and no key page."""
    missing = sorted(set(ALL_PROVIDERS) - {entry.provider for entry in connections.CONNECTIONS})
    assert missing == [], f"no catalogue entry for: {missing}"


def test_every_connection_entry_is_a_real_provider() -> None:
    """And the other direction: a card for something the database would refuse to store is a
    connect button that fails on save."""
    extra = sorted({entry.provider for entry in connections.CONNECTIONS} - set(ALL_PROVIDERS))
    assert extra == [], f"catalogue names a provider the database does not allow: {extra}"


def test_every_key_provider_says_where_to_get_the_key() -> None:
    """**The one field that actually saves time.** Pasting a key is ten seconds; finding the
    page that creates it is ten minutes, and that is the whole friction this slice removes.

    `mock` is exempt because it has no site: it answers without calling anybody.
    """
    for entry in connections.CONNECTIONS:
        if entry.method != "key" or entry.provider == "mock":
            continue
        assert entry.key_page.startswith("https://"), entry.provider


def test_every_key_provider_explains_why_there_is_no_button() -> None:
    """Written down rather than left as a silence. "Why can I connect Gmail in one click but
    not this?" is a reasonable question with a real answer, and an unanswered one reads as the
    feature being unfinished."""
    for entry in connections.CONNECTIONS:
        if entry.method == "key":
            assert entry.oauth_note.strip(), entry.provider


def test_nothing_claims_one_click_that_does_not_have_it() -> None:
    """**The claim this module exists to keep honest.** A card saying "connect in one click"
    over a paste field is the same defect as a draft asserting what no page supports, pointed
    at a different reader.

    Every provider here is key-based today. Apollo becomes `oauth` when their partner
    credentials exist, and this test then holds it to having no `key_page` instead.
    """
    for entry in connections.CONNECTIONS:
        if entry.one_click:
            assert entry.method == "oauth"
            assert not entry.key_page, f"{entry.provider} claims one-click and wants a key page"
        else:
            assert entry.method == "key"


def test_only_snov_asks_for_a_client_id() -> None:
    """Declared per provider rather than inferred from the name, so the form asks for what the
    provider actually wants. If another provider adds a client id, this test is where it is
    noticed."""
    wants = {entry.provider for entry in connections.CONNECTIONS if entry.needs_client_id}
    assert wants == {"snov"}


def test_describe_is_empty_for_an_unknown_provider() -> None:
    """An unknown provider renders nothing rather than a card with blank fields."""
    assert connections.describe("not-a-provider") == {}
    assert connections.describe("") == {}


def test_a_contact_provider_describes_what_it_finds() -> None:
    """A list of five names tells nobody which one to connect."""
    for provider in CONTACT_PROVIDERS:
        entry = connections.for_provider(provider)
        assert entry is not None
        assert entry.purpose.strip()
