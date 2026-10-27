"""What the export must never leak, and what it must never lose.

The export is the one place this dataset leaves the database, so it is the one place a privacy
mistake becomes a file somebody copies. These tests are over the pure row-building function, with
no I/O, so they run everywhere and fail fast.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from app.scripts.export_dataset import SCHEMA_VERSION, manifest_for, pseudonymise, row_for

SALT = "a-test-salt"
SENT = datetime(2026, 11, 3, 10, 30, tzinfo=UTC)  # a Tuesday


def _row(**over):
    outcome = SimpleNamespace(
        outcome="positive_interest",
        censored=False,
        stage="replied",
        objection_kind=None,
        confidence=0.9,
        confirmed_by="system",
        campaign_kind="outreach",
        first_sent_at=SENT,
        resolved_at=datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
    )
    for key, value in over.pop("outcome", {}).items():
        setattr(outcome, key, value)
    base = {
        "outcome": outcome,
        "lead": SimpleNamespace(
            email_norm="Asha@Example.com",
            company_name="Globex",
            title="Founder",
            sector="saas",
        ),
        "campaign": SimpleNamespace(mode="research"),
        "first_message": SimpleNamespace(sent_at=SENT, warmup_day=9, variant="b", status="sent"),
        "ai_draft": SimpleNamespace(subject="AI wrote this"),
        "approved_draft": SimpleNamespace(
            subject="A human fixed this", claims=[{"id": "pp1"}], prompt_version="v3"
        ),
        "salt": SALT,
    }
    base.update(over)
    return row_for(**base)


class TestNothingIdentifyingLeavesInTheClear:
    def test_the_address_is_hashed(self):
        row = _row()
        assert "asha@example.com" not in json.dumps(row).lower()
        assert len(row["lead"]) == 64

    def test_the_company_name_is_hashed(self):
        assert "globex" not in json.dumps(_row()).lower()

    def test_hashing_is_stable_within_one_export(self):
        """Two rows for the same person must collide, or the hash is just noise."""
        assert pseudonymise("a@b.com", SALT) == pseudonymise("A@B.COM ", SALT)

    def test_a_different_salt_gives_a_different_digest(self):
        """Per-export salts are what stop two exports being joined to each other."""
        assert pseudonymise("a@b.com", SALT) != pseudonymise("a@b.com", "other-salt")

    def test_a_missing_identifier_stays_missing(self):
        assert pseudonymise(None, SALT) is None
        assert pseudonymise("   ", SALT) is None

    def test_the_manifest_does_not_contain_the_salt(self):
        """Writing the salt beside the hashes would make the hashing decorative."""
        manifest = manifest_for(org_id="org", rows=3, salt=SALT, started=SENT)
        assert SALT not in json.dumps(manifest)
        assert len(manifest["salt_fingerprint"]) == 16

    def test_the_manifest_says_it_is_still_personal_data(self):
        """`PRIVACY.md`'s honesty rule: a hash of an email address is pseudonymisation."""
        assert (
            "personal data" in manifest_for(org_id="org", rows=1, salt=SALT, started=SENT)["note"]
        )


class TestCensoringSurvivesTheExport:
    def test_a_censored_row_says_so(self):
        """**The one field a consumer must branch on before counting anything.**"""
        row = _row(outcome={"outcome": "no_reply_after_window", "censored": True})
        assert row["censored"] is True

    def test_an_unresolved_outcome_exports_as_null_not_as_a_class(self):
        """NULL is "still watching". A `pending` string would be counted as a label."""
        row = _row(outcome={"outcome": None, "censored": False, "resolved_at": None})
        assert row["outcome"] is None
        assert row["observed_days"] is None


class TestTheSignalsAreKept:
    def test_the_human_edit_pair_is_exported(self):
        row = _row()
        assert row["ai_subject"] == "AI wrote this"
        assert row["subject_as_sent"] == "A human fixed this"
        assert row["was_human_edited"] is True

    def test_an_unedited_draft_is_marked_as_such(self):
        row = _row(ai_draft=None)
        assert row["ai_subject"] is None
        assert row["was_human_edited"] is False

    def test_timing_is_derived_from_sent_at(self):
        """Derived at export rather than stored, so it cannot disagree with its source."""
        row = _row()
        assert row["sent_weekday"] == 1  # Tuesday
        assert row["sent_hour"] == 10

    def test_sender_context_survives(self):
        row = _row()
        assert row["warmup_day"] == 9
        assert row["variant"] == "b"

    def test_a_human_confirmation_is_distinguishable_from_a_model_one(self):
        """A person's answer must be weightable differently from a model's."""
        assert _row()["confirmed_by_human"] is False
        assert _row(outcome={"confirmed_by": "asha@example.com"})["confirmed_by_human"] is True

    def test_every_row_carries_a_schema_version(self):
        assert _row()["schema_version"] == SCHEMA_VERSION

    def test_a_lead_erased_after_the_fact_still_exports(self):
        """`erase_lead` detaches the person and keeps the features.

        A deletion request must not silently destroy the fact that *an* email of this shape got
        this result -- that is the anonymised half the design promises to keep.
        """
        row = _row(lead=None, first_message=None)
        assert row["lead"] is None
        assert row["outcome"] == "positive_interest"
