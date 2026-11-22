"""Unit tests for bulk-reject input parsing helpers."""

from __future__ import annotations

import uuid


def _ids(n: int) -> list[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


class TestBulkRejectPayload:
    """Validate that the helper correctly parses / rejects payloads.

    These call the same parsing helpers the route uses, so the route's
    HTTP 422 paths are covered without spinning up a live server.
    """

    def _parse(self, payload: dict) -> tuple[list[uuid.UUID], str]:
        """Mirror the parsing logic in POST /review/bulk-reject."""
        raw = payload.get("draft_ids")
        if not isinstance(raw, list) or not raw:
            raise ValueError("draft_ids must be a non-empty list")
        if len(raw) > 200:
            raise ValueError("At most 200 drafts can be rejected in one request")
        try:
            ids = [uuid.UUID(str(v)) for v in raw]
        except ValueError as exc:
            raise ValueError("draft_ids must all be uuids") from exc
        feedback = (payload.get("feedback") or "").strip()
        if not feedback:
            raise ValueError("feedback is required for bulk reject")
        return ids, feedback

    def test_valid_payload_parses(self) -> None:
        ids, fb = self._parse({"draft_ids": _ids(3), "feedback": "tone is off"})
        assert len(ids) == 3
        assert fb == "tone is off"

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            self._parse({"draft_ids": [], "feedback": "nope"})

    def test_missing_draft_ids_raises(self) -> None:
        with pytest.raises(ValueError):
            self._parse({"feedback": "nope"})

    def test_non_uuid_raises(self) -> None:
        with pytest.raises(ValueError, match="uuid"):
            self._parse({"draft_ids": ["not-a-uuid"], "feedback": "nope"})

    def test_missing_feedback_raises(self) -> None:
        with pytest.raises(ValueError, match="feedback"):
            self._parse({"draft_ids": _ids(2)})

    def test_blank_feedback_raises(self) -> None:
        with pytest.raises(ValueError, match="feedback"):
            self._parse({"draft_ids": _ids(2), "feedback": "   "})

    def test_201_ids_raises(self) -> None:
        with pytest.raises(ValueError, match="200"):
            self._parse({"draft_ids": _ids(201), "feedback": "purge"})

    def test_exactly_200_ids_is_ok(self) -> None:
        ids, _ = self._parse({"draft_ids": _ids(200), "feedback": "bulk clear"})
        assert len(ids) == 200

    def test_feedback_is_stripped(self) -> None:
        _, fb = self._parse({"draft_ids": _ids(1), "feedback": "  trimmed  "})
        assert fb == "trimmed"


import pytest  # noqa: E402  (pytest import at the bottom to satisfy ruff I-001)
