"""The rehearsal report's verdict (F1, slice S10.3)."""

from __future__ import annotations

from app.scripts import rehearsal_report as rr


def test_a_clean_rehearsal_passes():
    out = rr.render(["x"], {"sent": 3, "failed": 1, "other": 0, "off_allowlist": 0})

    assert "OK:" in out
    assert "FAIL" not in out


def test_a_delivered_message_off_the_allowlist_fails():
    out = rr.render(["x"], {"sent": 3, "failed": 0, "other": 0, "off_allowlist": 1})

    assert "FAIL:" in out


def test_blocked_messages_alone_do_not_fail_it():
    """A rehearsal in allowlist mode should be full of blocked sends; that is the gate working."""
    out = rr.render(["x"], {"sent": 0, "failed": 9, "other": 0, "off_allowlist": 0})

    assert "OK:" in out


def test_the_since_argument_is_treated_as_utc_when_naive():
    assert rr._parse_since("2026-10-01").tzinfo is not None
