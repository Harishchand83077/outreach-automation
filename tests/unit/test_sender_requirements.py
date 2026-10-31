"""Checking a sending domain's SPF, DKIM, DMARC and one-click unsubscribe (F4, slice S11.5).

Gmail and Yahoo's bulk-sender rules are the reason this exists, and the repo has already been
caught out by the gap between *published* and *working*: mail went out as `From: rahul@arkvane.in`
while SMTP authenticated as an unrelated Gmail, so the SPF and DKIM records verified for that
domain did not apply to any of it, and the first reply this system ever received was in Spam.

So the checker is built around one honesty rule: **say what DNS can prove and no more.**

- One SPF record is checkable. Two is a permanent error, not a merge, and that is the single most
  common way a domain's mail breaks.
- DKIM is only *probeable*. DNS cannot enumerate selectors, so a miss means "not at the selectors
  we tried", never "no DKIM" — and a published key still does not prove anything is signing with
  it. Only a received message can (`GO_LIVE.md` §1).
- A failed lookup is `UNKNOWN`, never `FAIL`. A timeout is not evidence of absence, which is the
  same rule the importer's MX check already follows.
"""

from __future__ import annotations

from app.services.deliverability import sender_requirements as sr


class FakeResolver:
    """TXT answers by name, with an optional set of names that fail to resolve."""

    def __init__(self, answers: dict[str, list[str]], *, failing: set[str] | None = None) -> None:
        self.answers = answers
        self.failing = failing or set()
        self.asked: list[str] = []

    def txt(self, name: str) -> list[str] | None:
        self.asked.append(name)
        if name in self.failing:
            return None  # could not tell
        return self.answers.get(name, [])


GOOD = {
    "example.com": ["v=spf1 include:_spf.google.com ~all"],
    "google._domainkey.example.com": ["v=DKIM1; k=rsa; p=MIIBIjAN"],
    "_dmarc.example.com": ["v=DMARC1; p=none; rua=mailto:dmarc@example.com"],
}


def check(answers, *, failing=None, unsubscribe=True):
    return sr.check_domain(
        "example.com",
        resolver=FakeResolver(answers, failing=failing),
        unsubscribe_configured=unsubscribe,
    )


class TestSpf:
    def test_exactly_one_record_passes(self):
        assert check(GOOD).spf.verdict is sr.Verdict.PASS

    def test_two_records_fail_and_say_why(self):
        """**The most common way a domain's mail breaks.** Two SPF records is a permanent error.

        Receivers do not merge them; they give up. Worth its own message because the instinct
        when adding a sender is to add a second record rather than edit the first.
        """
        answers = GOOD | {
            "example.com": [
                "v=spf1 include:_spf.google.com ~all",
                "v=spf1 include:sendgrid.net ~all",
            ]
        }

        result = check(answers).spf

        assert result.verdict is sr.Verdict.FAIL
        assert "two" in result.detail.lower() or "2" in result.detail

    def test_no_record_fails(self):
        assert check(GOOD | {"example.com": []}).spf.verdict is sr.Verdict.FAIL

    def test_other_txt_records_are_not_counted_as_spf(self):
        """A domain's apex is full of verification tokens; only `v=spf1` is an SPF record."""
        answers = GOOD | {
            "example.com": [
                "google-site-verification=abc",
                "v=spf1 include:_spf.google.com ~all",
            ]
        }

        assert check(answers).spf.verdict is sr.Verdict.PASS

    def test_a_failed_lookup_is_unknown_not_a_failure(self):
        result = check(GOOD, failing={"example.com"}).spf

        assert result.verdict is sr.Verdict.UNKNOWN


class TestDkim:
    def test_a_key_at_a_known_selector_passes(self):
        assert check(GOOD).dkim.verdict is sr.Verdict.PASS

    def test_the_selector_that_answered_is_named(self):
        """Which selector matters when fixing it, and the operator does not know ours by heart."""
        assert "google" in check(GOOD).dkim.detail

    def test_a_miss_is_unknown_rather_than_a_failure(self):
        """**DNS cannot enumerate selectors.**

        A miss means "not at the selectors we tried", and reporting that as FAIL would tell an
        operator their DKIM is broken when it may be published under a selector we never asked
        for. Overstating a failure here costs trust in the whole report.
        """
        answers = {name: value for name, value in GOOD.items() if "_domainkey" not in name}

        result = check(answers).dkim

        assert result.verdict is sr.Verdict.UNKNOWN
        assert "selector" in result.detail.lower()

    def test_a_published_key_is_reported_as_published_and_defers_on_signing(self):
        """The distinction the project has already been burned by.

        A published record does not mean anything is signing with it -- only a received message
        shows that. So the detail says "published" and then explicitly sends the reader to a
        received message, rather than letting them infer that DKIM is working.

        The first version of this test asserted the word "signed" was absent, which failed
        against a sentence that uses it to *deny* signing. Asserting the absence of a word is a
        poor proxy for asserting a claim.
        """
        detail = check(GOOD).dkim.detail.lower()

        assert "published" in detail
        assert "received message" in detail


class TestDmarc:
    def test_a_record_passes_and_the_policy_is_reported(self):
        result = check(GOOD).dmarc

        assert result.verdict is sr.Verdict.PASS
        assert "p=none" in result.detail

    def test_no_record_fails(self):
        assert check(GOOD | {"_dmarc.example.com": []}).dmarc.verdict is sr.Verdict.FAIL

    def test_a_missing_rua_is_called_out(self):
        """Aggregate reports are the one deliverability signal that works at any volume.

        Postmaster Tools shows nothing below a few hundred a day (ADR-029); DMARC reports arrive
        regardless. A domain with no `rua` is flying blind and should be told so.
        """
        answers = GOOD | {"_dmarc.example.com": ["v=DMARC1; p=none"]}

        result = check(answers).dmarc

        assert result.verdict is sr.Verdict.WARN
        assert "rua" in result.detail.lower()


class TestOneClickUnsubscribe:
    def test_it_passes_when_configured(self):
        assert check(GOOD, unsubscribe=True).one_click.verdict is sr.Verdict.PASS

    def test_it_fails_when_not_configured(self):
        """`PUBLIC_API_URL` unset means no one-click header is sent at all (ADR-033)."""
        result = check(GOOD, unsubscribe=False).one_click

        assert result.verdict is sr.Verdict.FAIL
        assert "PUBLIC_API_URL" in result.detail


class TestTheOverallVerdict:
    def test_everything_right_is_ready(self):
        assert check(GOOD).ready is True

    def test_one_failure_is_not_ready(self):
        assert check(GOOD | {"example.com": []}).ready is False

    def test_a_warning_alone_is_still_ready(self):
        """A missing `rua` is worth fixing and does not stop mail being delivered."""
        answers = GOOD | {"_dmarc.example.com": ["v=DMARC1; p=none"]}

        assert check(answers).ready is True

    def test_unknown_is_not_ready(self):
        """Not ready is the honest answer to "we could not tell", for a go-live gate."""
        assert check(GOOD, failing={"example.com"}).ready is False

    def test_no_secret_or_address_appears_in_any_detail(self):
        """These strings reach a browser, a screenshot and a log.

        The DKIM public key is the one long opaque value here, and printing it would be noise
        at best; a `rua` address is somebody's mailbox.
        """
        report = check(GOOD)
        joined = " ".join(item.detail for item in report.items)

        assert "MIIBIjAN" not in joined
        assert "dmarc@example.com" not in joined


class TestAProviderOwnedDomain:
    """**The multi-user case, and the majority one.**

    Most people who sign up connect a Gmail address. `gmail.com` answers an SPF and a DMARC
    record but no selector we could guess, because Google signs with its own rotating ones. Run
    through the ordinary checklist, such a mailbox was reported "not ready" over a DKIM record
    the user cannot publish and does not need -- which is how a report stops being read.
    """

    def test_gmail_is_ready_rather_than_failing_on_a_dkim_record_nobody_can_publish(self):
        report = sr.check_domain(
            "gmail.com", resolver=FakeResolver({}), unsubscribe_configured=True
        )

        assert report.provider_owned is True
        assert report.ready is True

    def test_it_does_not_query_dns_at_all(self):
        """Nothing in DNS could change the answer, so asking is pure waste."""
        resolver = FakeResolver({})

        sr.check_domain("gmail.com", resolver=resolver, unsubscribe_configured=True)

        assert resolver.asked == []

    def test_it_reports_one_authentication_line_not_three_identical_ones(self):
        report = sr.check_domain(
            "outlook.com", resolver=FakeResolver({}), unsubscribe_configured=True
        )

        assert [check.name for check in report.items] == [
            "Authentication",
            "One-click unsubscribe",
        ]

    def test_it_says_the_two_things_that_are_actually_true(self):
        """Shared reputation, and no Postmaster verification. Both easy to assume away."""
        report = sr.check_domain(
            "gmail.com", resolver=FakeResolver({}), unsubscribe_configured=True
        )
        detail = report.spf.detail.lower()

        assert "share" in detail
        assert "postmaster" in detail

    def test_one_click_is_still_ours_to_get_right(self):
        """The header is ours, not the provider's, so it is still checked and can still fail."""
        report = sr.check_domain(
            "gmail.com", resolver=FakeResolver({}), unsubscribe_configured=False
        )

        assert report.one_click.verdict is sr.Verdict.FAIL
        assert report.ready is False

    def test_a_domain_the_sender_owns_still_gets_the_full_checklist(self):
        """The whole point is to tell the two cases apart, not to skip the checks."""
        report = check(GOOD)

        assert report.provider_owned is False
        assert [check_item.name for check_item in report.items] == [
            "SPF",
            "DKIM",
            "DMARC",
            "One-click unsubscribe",
        ]

    def test_case_and_a_trailing_dot_do_not_defeat_it(self):
        report = sr.check_domain(
            "GMail.com.", resolver=FakeResolver({}), unsubscribe_configured=True
        )

        assert report.provider_owned is True
