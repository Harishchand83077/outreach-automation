"""A non-reply is not a "no", and a reply is not automatically good or bad.

These are the two rules the whole dataset rests on, so they are unit tests over pure functions
rather than something only observable after a database round trip.

**Why censoring matters enough to test.** If silence were recorded as rejection, any future model
trained on this would learn that every email without an instant answer was a bad email -- when
the commonest reason for silence is that somebody was busy on Tuesday. The bug would be invisible:
the dataset would look full and well-populated, and be systematically wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.outcome import (
    FUNNEL_STAGES,
    Outcome,
    Stage,
    outcome_for_label,
    resolve_silence,
    stages_for_kind,
)
from app.domain.reply import POLICY, ReplyLabel

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


class TestSilenceIsCensoredNotNegative:
    def test_a_lead_still_inside_the_window_is_unresolved(self):
        """Not "no_reply" yet -- we are still watching."""
        resolved = resolve_silence(first_sent_at=NOW - timedelta(days=3), wait_days=14, now=NOW)
        assert resolved is None

    def test_a_lead_past_the_window_resolves_to_no_reply(self):
        resolved = resolve_silence(first_sent_at=NOW - timedelta(days=20), wait_days=14, now=NOW)
        assert resolved is Outcome.NO_REPLY_AFTER_WINDOW

    def test_no_reply_after_window_is_censored(self):
        """**The load-bearing assertion of this entire design.**

        It is a real, recorded result -- and it is *not* evidence that the email was bad. Anything
        that reads this dataset has to be able to tell the two apart.
        """
        assert Outcome.NO_REPLY_AFTER_WINDOW.censored is True

    def test_every_other_outcome_is_observed(self):
        for outcome in Outcome:
            if outcome is not Outcome.NO_REPLY_AFTER_WINDOW:
                assert outcome.censored is False, outcome

    def test_a_send_that_never_happened_has_no_clock(self):
        assert resolve_silence(first_sent_at=None, wait_days=14, now=NOW) is None


class TestAReplyIsNotOneAxis:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            (ReplyLabel.INTERESTED, Outcome.POSITIVE_INTEREST),
            (ReplyLabel.WRONG_PERSON_REFERRAL, Outcome.REFERRAL),
            (ReplyLabel.NOT_NOW, Outcome.NOT_NOW),
            (ReplyLabel.HAS_VENDOR, Outcome.HAS_VENDOR),
            (ReplyLabel.NOT_INTERESTED, Outcome.NOT_RELEVANT),
            (ReplyLabel.OBJECTION, Outcome.OBJECTION),
            (ReplyLabel.OPT_OUT, Outcome.UNSUBSCRIBE),
            (ReplyLabel.HOSTILE, Outcome.HOSTILE),
            (ReplyLabel.OUT_OF_OFFICE, Outcome.AUTO_REPLY),
        ],
    )
    def test_each_label_maps_to_its_own_outcome(self, label, expected):
        assert outcome_for_label(label) is expected

    def test_a_referral_is_good_news_and_not_a_rejection(self):
        """ "Wrong person, talk to Priya" is a lead, not a loss.

        Collapsing it into "negative" is the single most expensive mistake available here,
        because referrals are the highest-converting thing cold outreach produces.
        """
        assert outcome_for_label(ReplyLabel.WRONG_PERSON_REFERRAL).is_encouraging is True

    def test_has_vendor_is_distinguished_from_not_interested(self):
        """They have the budget and the problem; somebody else is serving it.

        That is a displacement and timing signal, and folding it into `not_relevant` would
        delete it.
        """
        assert outcome_for_label(ReplyLabel.HAS_VENDOR) is not outcome_for_label(
            ReplyLabel.NOT_INTERESTED
        )

    def test_not_now_is_not_counted_against_the_email(self):
        assert outcome_for_label(ReplyLabel.NOT_NOW).is_encouraging is True

    def test_an_unclear_reply_is_not_forced_into_a_class(self):
        """`other` means a person decides. Guessing would manufacture training labels."""
        assert outcome_for_label(ReplyLabel.OTHER) is None

    def test_a_bounce_is_deliverability_not_an_opinion(self):
        """It says nothing about the email's content, so it must not look like it does."""
        assert outcome_for_label(ReplyLabel.BOUNCE).is_encouraging is False
        assert outcome_for_label(ReplyLabel.BOUNCE) is Outcome.BOUNCE


class TestMeetingBookedIsNeverInferred:
    def test_no_reply_label_maps_to_meeting_booked(self):
        """A locked product rule since Phase 1: only a human click or a booking webhook.

        A warm reply is not a meeting, and a model that learned to call it one would quietly
        inflate every funnel this system ever reports.
        """
        assert all(outcome_for_label(label) is not Outcome.MEETING_BOOKED for label in ReplyLabel)


class TestTheTaxonomyStaysWiredToTheActionTable:
    def test_every_label_still_has_a_policy(self):
        """The new labels must not arrive without an action.

        `POLICY` decides suppressions and opt-outs, so a label with no entry is a reply the
        system does not know what to do with.
        """
        missing = [label for label in ReplyLabel if label not in POLICY]
        assert missing == [], f"labels with no action: {missing}"

    def test_hostile_never_produces_an_automatic_reply(self):
        """Somebody threatening to report us as spam must not be written to by a machine."""
        assert POLICY[ReplyLabel.HOSTILE].draft_reply is False

    def test_hostile_is_suppressed(self):
        assert POLICY[ReplyLabel.HOSTILE].action.value == "suppress"


class TestFunnelStagesAreKindSpecific:
    def test_outreach_and_job_hunt_have_different_stages(self):
        assert stages_for_kind("outreach") != stages_for_kind("job_hunt")

    def test_both_start_at_sent_and_share_replied(self):
        for kind in ("outreach", "job_hunt"):
            stages = stages_for_kind(kind)
            assert stages[0] is Stage.SENT
            assert Stage.REPLIED in stages

    def test_an_unknown_kind_falls_back_to_outreach(self):
        """A kind added later must not make an existing export raise."""
        assert stages_for_kind("something_new") == FUNNEL_STAGES["outreach"]
