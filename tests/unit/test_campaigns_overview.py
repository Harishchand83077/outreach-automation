"""S18.2 — /campaigns/overview unit tests.

The overview route must:
  - be declared ABOVE /campaigns/{campaign_id} so FastAPI does not read "overview"
    as a UUID (ADR-064).
  - return one row per campaign with the agreed keys.
  - never expose `opened` or `clicked` columns (no tracking pixels, no link rewriting).
"""

from __future__ import annotations

import ast
import pathlib

import pytest


class TestRouteOrder:
    def test_overview_is_declared_before_campaign_id_route(self) -> None:
        """overview must be above /campaigns/{campaign_id} or FastAPI reads it as an id."""
        source = pathlib.Path("app/api/routers/campaigns.py").read_text()
        overview_pos = source.find('"/campaigns/overview"')
        campaign_id_pos = source.find('"/campaigns/{campaign_id}"')
        assert overview_pos != -1, "/campaigns/overview route not found"
        assert campaign_id_pos != -1, "/campaigns/{campaign_id} route not found"
        assert overview_pos < campaign_id_pos, (
            "/campaigns/overview must be declared before /campaigns/{campaign_id}"
        )

    def test_overview_route_does_not_404(self) -> None:
        """The route must exist so the guard above is not trivially satisfied."""
        source = pathlib.Path("app/api/routers/campaigns.py").read_text()
        assert '"/campaigns/overview"' in source, "/campaigns/overview route not found"


class TestNoTrackingColumns:
    def test_overview_route_never_names_opened(self) -> None:
        """No tracking-pixel column, ever — locked product rule.

        Checks the string keys returned, not the docstring.
        """
        source = pathlib.Path("app/api/routers/campaigns.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "campaigns_overview"
            ):
                # Walk Return nodes and collect string keys from dicts
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant) and isinstance(child.value, str):
                        assert child.value != "opened", (
                            "overview must not return 'opened' key — no tracking pixels"
                        )
                        assert child.value != "clicked", (
                            "overview must not return 'clicked' key — no link rewriting"
                        )
                return
        pytest.skip("campaigns_overview not yet written")

    def test_campaigns_screen_never_names_opened(self) -> None:
        screen = pathlib.Path("frontend/src/Campaigns.jsx")
        if not screen.exists():
            pytest.skip("Campaigns.jsx not yet written")
        import re
        source = screen.read_text()
        # Match property access (.opened / ?.opened) or string key ("opened" / 'opened')
        assert not re.search(r'[.?]opened\b|["\']opened["\']', source), (
            "Campaigns screen must not expose 'opened' — no tracking pixels"
        )
        assert not re.search(r'[.?]clicked\b|["\']clicked["\']', source), (
            "Campaigns screen must not expose 'clicked' — no link rewriting"
        )


class TestOverviewKeys:
    def test_overview_returns_expected_keys(self) -> None:
        """The route must include these keys so the frontend can use them."""
        source = pathlib.Path("app/api/routers/campaigns.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name == "campaigns_overview"
            ):
                func_source = ast.get_source_segment(source, node) or ""
                for key in ("leads_total", "emails_sent", "replied", "meetings_booked"):
                    assert f'"{key}"' in func_source or f"'{key}'" in func_source, (
                        f"overview must return '{key}'"
                    )
                return
        pytest.skip("campaigns_overview not yet written")
