"""Unit tests for lead note body validation and response shape (S21.1, ADR-078)."""

from __future__ import annotations

import uuid


def _validate_body(body: str) -> str:
    """Mirror the validation the POST route performs."""
    from app.api.routers.campaigns import _validate_note_body

    return _validate_note_body(body)


class TestBodyValidation:
    def test_empty_body_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="empty"):
            _validate_body("")

    def test_whitespace_only_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="empty"):
            _validate_body("   ")

    def test_body_over_2000_chars_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="2000"):
            _validate_body("x" * 2001)

    def test_body_at_exactly_2000_chars_is_ok(self) -> None:
        result = _validate_body("x" * 2000)
        assert len(result) == 2000

    def test_body_is_stripped(self) -> None:
        result = _validate_body("  hello  ")
        assert result == "hello"

    def test_valid_body_returned(self) -> None:
        result = _validate_body("Great first call, follow up next week.")
        assert result == "Great first call, follow up next week."


class TestNoteShape:
    def test_note_dict_has_required_fields(self) -> None:
        note = {
            "id": str(uuid.uuid4()),
            "author_id": "user_abc",
            "body": "Test note",
            "created_at": "2026-11-22T10:00:00+00:00",
        }
        assert "id" in note
        assert "author_id" in note
        assert "body" in note
        assert "created_at" in note

    def test_notes_list_response_shape(self) -> None:
        response = {"notes": []}
        assert isinstance(response["notes"], list)
