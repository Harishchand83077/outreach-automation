"""S18.4 — contact lookup waterfall.

The waterfall walks the workspace's provider credentials in priority order and
returns the first provider that could be tried (has a live, non-invalid key).

Three invariants:

1. **Button-press only.** The waterfall is a pure helper that returns an ordered list; it does
   not call any network. Calling the providers is still `lookup.run`, which the button-press
   guard already covers.
2. **No shared key.** The waterfall reads only stored credentials; it has no Settings argument
   and cannot fall back to an env var.
3. **Priority order**: a credential with a lower `priority` number comes first; ties are broken
   by provider name so the order is deterministic.
"""

from __future__ import annotations

from app.services.contacts.waterfall import ordered_providers


class TestOrderedProviders:
    def test_lower_priority_comes_first(self) -> None:
        creds = [
            _cred("hunter", priority=20),
            _cred("snov", priority=10),
        ]
        result = ordered_providers(creds)
        assert [r.provider for r in result] == ["snov", "hunter"]

    def test_none_priority_sorts_last(self) -> None:
        creds = [
            _cred("hunter", priority=None),
            _cred("snov", priority=10),
        ]
        result = ordered_providers(creds)
        assert result[0].provider == "snov"
        assert result[1].provider == "hunter"

    def test_ties_broken_by_provider_name(self) -> None:
        creds = [
            _cred("snov", priority=10),
            _cred("hunter", priority=10),
        ]
        result = ordered_providers(creds)
        assert [r.provider for r in result] == ["hunter", "snov"]

    def test_invalid_key_is_excluded(self) -> None:
        creds = [
            _cred("hunter", priority=10, invalid=True),
            _cred("snov", priority=20),
        ]
        result = ordered_providers(creds)
        assert len(result) == 1
        assert result[0].provider == "snov"

    def test_revoked_key_is_excluded(self) -> None:
        creds = [
            _cred("hunter", priority=10, revoked=True),
            _cred("snov", priority=20),
        ]
        result = ordered_providers(creds)
        assert len(result) == 1
        assert result[0].provider == "snov"

    def test_empty_list_returns_empty(self) -> None:
        assert ordered_providers([]) == []

    def test_all_invalid_returns_empty(self) -> None:
        creds = [_cred("hunter", priority=10, invalid=True)]
        assert ordered_providers(creds) == []


class TestButtonPressOnly:
    def test_waterfall_module_imports_no_network_clients(self) -> None:
        """The waterfall is a pure sort; it must not import httpx or any provider adapter."""
        import ast
        import pathlib

        source = pathlib.Path("app/services/contacts/waterfall.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    assert "httpx" not in name, "waterfall must not import httpx"
                    assert "providers" not in name or "contacts" not in name, (
                        "waterfall must not import contact provider adapters"
                    )

    def test_waterfall_module_has_no_settings_argument(self) -> None:
        """The waterfall must not accept Settings — it cannot fall back to an env key."""
        import ast
        import pathlib

        source = pathlib.Path("app/services/contacts/waterfall.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for arg in node.args.args:
                    assert arg.arg != "settings", (
                        f"{node.name}() must not take a 'settings' argument"
                    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cred(
    provider: str, *, priority: int | None = None, invalid: bool = False, revoked: bool = False
):
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    c = MagicMock()
    c.provider = provider
    c.priority = priority
    c.invalid_reason = "bad" if invalid else None
    c.revoked_at = datetime.now(tz=UTC) if revoked else None
    return c
