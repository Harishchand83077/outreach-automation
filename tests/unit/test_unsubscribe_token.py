"""The one-click unsubscribe token (ADR-033 part 2, slice S11.4).

The token is the whole security surface of an **unauthenticated, public** endpoint that writes a
suppression. It has to carry who is unsubscribing, be unguessable, and be impossible to alter
into somebody else's identity.

What it deliberately is not: a secret. Anyone holding the email holds the token, which is the
point — a recipient must be able to unsubscribe without signing in. So the property that matters
is **integrity, not confidentiality**: a token cannot be edited to point at a different lead.
"""

from __future__ import annotations

import uuid

import pytest

from app.services import unsubscribe


def test_a_token_round_trips():
    org, lead = uuid.uuid4(), uuid.uuid4()

    token = unsubscribe.sign(org_id=org, lead_id=lead, secret="a-test-secret")

    assert unsubscribe.verify(token, secret="a-test-secret") == (org, lead)


def test_the_token_is_url_safe():
    """It goes in a header and in a URL a mail client will rewrite if it can."""
    token = unsubscribe.sign(org_id=uuid.uuid4(), lead_id=uuid.uuid4(), secret="s")

    assert token
    assert all(ch.isalnum() or ch in "-_." for ch in token), token


def test_two_leads_get_different_tokens():
    org = uuid.uuid4()
    first = unsubscribe.sign(org_id=org, lead_id=uuid.uuid4(), secret="s")
    second = unsubscribe.sign(org_id=org, lead_id=uuid.uuid4(), secret="s")

    assert first != second


@pytest.mark.parametrize(
    "mangle",
    [
        lambda t: t[:-1],
        lambda t: t + "x",
        lambda t: t.replace(t[0], "z", 1),
        lambda t: t.split(".")[0],
        lambda t: "." + t,
        lambda t: "",
        lambda t: "not-a-token",
    ],
)
def test_a_mangled_token_is_refused(mangle):
    """Every rejection returns None rather than raising.

    The caller is a public route, and a route that can be made to raise by sending it rubbish
    is a route somebody will make raise -- which in this codebase means a 500 outside the CORS
    middleware, the trap that once made a duplicate key look like an unreachable backend.
    """
    token = unsubscribe.sign(org_id=uuid.uuid4(), lead_id=uuid.uuid4(), secret="s")

    assert unsubscribe.verify(mangle(token), secret="s") is None


def test_a_token_signed_with_another_secret_is_refused():
    """**The assertion the whole scheme rests on.**

    Without this, the ids in the token are just data a sender can edit: anybody could
    unsubscribe anybody by changing the lead id and re-encoding.
    """
    token = unsubscribe.sign(org_id=uuid.uuid4(), lead_id=uuid.uuid4(), secret="theirs")

    assert unsubscribe.verify(token, secret="ours") is None


def test_the_payload_cannot_be_edited_into_another_lead():
    """The same property stated as an attack, because that is how it will be attempted."""
    org, lead = uuid.uuid4(), uuid.uuid4()
    token = unsubscribe.sign(org_id=org, lead_id=lead, secret="s")
    payload, _, mac = token.partition(".")
    forged = unsubscribe.sign(org_id=org, lead_id=uuid.uuid4(), secret="s").partition(".")[0]

    assert unsubscribe.verify(f"{forged}.{mac}", secret="s") is None
    assert payload != forged


def test_the_signing_key_is_derived_rather_than_the_credential_key_itself():
    """`CREDENTIAL_KEY` encrypts mailbox credentials. This signs public URLs.

    Using one secret for both would mean a flaw in either reached the other, so the signing key
    is derived from it under a fixed label: no new secret for the owner to manage, and the two
    purposes stay separate.
    """
    derived = unsubscribe.signing_key("a-credential-key")

    assert derived != "a-credential-key"
    assert derived == unsubscribe.signing_key("a-credential-key")
    assert derived != unsubscribe.signing_key("another-credential-key")
