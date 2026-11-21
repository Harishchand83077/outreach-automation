"""Domain logic for the inbox folder assignment.

Each test checks one routing rule in isolation; the pure function takes no I/O so
there is nothing to mock.  Run with:
    pytest tests/unit/test_inbox_domain.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain import inbox


NOW = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 22, 10, 0, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)


def _folder(
    *,
    lead_state: str = "REPLIED",
    latest_label: str | None = None,
    has_pending_draft: bool = False,
    snoozed_until: datetime | None = None,
    archived_at: datetime | None = None,
) -> str | None:
    return inbox.folder(
        lead_state=lead_state,
        latest_label=latest_label,
        has_pending_draft=has_pending_draft,
        snoozed_until=snoozed_until,
        archived_at=archived_at,
        now=NOW,
    )


class TestArchiveAndSnooze:
    def test_archived_takes_priority_over_everything(self) -> None:
        result = _folder(
            archived_at=EARLIER,
            lead_state="MEETING_BOOKED",
            latest_label="interested",
        )
        assert result == "archived"

    def test_snoozed_until_future_takes_priority(self) -> None:
        result = _folder(snoozed_until=LATER)
        assert result == "snoozed"

    def test_expired_snooze_does_not_snooze(self) -> None:
        result = _folder(snoozed_until=EARLIER, lead_state="REPLIED")
        assert result != "snoozed"

    def test_archived_beats_active_snooze(self) -> None:
        result = _folder(archived_at=EARLIER, snoozed_until=LATER)
        assert result == "archived"


class TestTerminalStates:
    def test_meeting_booked(self) -> None:
        assert _folder(lead_state="MEETING_BOOKED") == "meeting_booked"

    def test_not_interested_state(self) -> None:
        assert _folder(lead_state="NOT_INTERESTED") == "not_interested"

    def test_opted_out_state(self) -> None:
        assert _folder(lead_state="OPTED_OUT") == "not_interested"

    def test_bounced_state(self) -> None:
        assert _folder(lead_state="BOUNCED") == "not_interested"


class TestReplyLabels:
    def test_not_interested_label(self) -> None:
        assert _folder(latest_label="not_interested") == "not_interested"

    def test_opt_out_label(self) -> None:
        assert _folder(latest_label="opt_out") == "not_interested"

    def test_hostile_label(self) -> None:
        assert _folder(latest_label="hostile") == "not_interested"

    def test_wrong_person_label(self) -> None:
        assert _folder(latest_label="wrong_person_referral") == "wrong_person"

    def test_out_of_office_label(self) -> None:
        assert _folder(latest_label="out_of_office") == "out_of_office"

    def test_interested_label(self) -> None:
        assert _folder(latest_label="interested") == "interested"

    def test_question_label_needs_reply(self) -> None:
        assert _folder(latest_label="question") == "needs_reply"

    def test_objection_label_needs_reply(self) -> None:
        assert _folder(latest_label="objection") == "needs_reply"


class TestDraftWaiting:
    def test_pending_draft_gives_drafts_waiting(self) -> None:
        assert _folder(has_pending_draft=True) == "drafts_waiting"

    def test_pending_draft_beats_interested_label(self) -> None:
        assert _folder(has_pending_draft=True, latest_label="interested") == "drafts_waiting"

    def test_pending_draft_does_not_beat_terminal(self) -> None:
        # A MEETING_BOOKED lead with a stale draft stays in meeting_booked
        assert _folder(has_pending_draft=True, lead_state="MEETING_BOOKED") == "meeting_booked"


class TestNeedsReply:
    def test_replied_state_no_draft(self) -> None:
        assert _folder(lead_state="REPLIED") == "needs_reply"

    def test_in_conversation_state(self) -> None:
        assert _folder(lead_state="IN_CONVERSATION") == "needs_reply"


class TestNotInInbox:
    def test_new_lead_not_in_inbox(self) -> None:
        assert _folder(lead_state="NEW") is None

    def test_enriching_not_in_inbox(self) -> None:
        assert _folder(lead_state="ENRICHING") is None

    def test_sent_without_reply_not_in_inbox(self) -> None:
        assert _folder(lead_state="SENT") is None

    def test_skipped_not_in_inbox(self) -> None:
        assert _folder(lead_state="SKIPPED") is None


class TestStructural:
    def test_inbox_router_imports_nothing_that_writes_leads_state(self) -> None:
        """The inbox router must not import state.transition — only the one writer may."""
        import ast
        import pathlib

        router_path = pathlib.Path("app/api/routers/inbox.py")
        if not router_path.exists():
            pytest.skip("inbox router not yet written")
        tree = ast.parse(router_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    assert "state" not in name or "lead" not in name, (
                        "inbox router must not import the state transition module"
                    )
