"""S19.2b — opportunities API structural guards.

1. Status changes only go through POST /opportunities/{id}/status.
2. The route order guard: /opportunities/today must be declared before /opportunities/:id.
3. The timeline cannot be edited through the API (no PUT/PATCH/DELETE on opportunity_events).
"""

from __future__ import annotations

import ast
import pathlib

import pytest


class TestRouteOrder:
    def test_today_route_declared_before_id_route(self) -> None:
        """The literal /today path must be declared before the /{opportunity_id} path."""
        source = pathlib.Path("app/api/routers/opportunities.py").read_text()
        # Find the literal today path (in any quoting style)
        today_pos = -1
        for needle in ('"/opportunities/today"', "'/opportunities/today'", '"/today"', "'/today'"):
            p = source.find(needle)
            if p != -1:
                today_pos = p
                break
        # Find the {opportunity_id} path parameter (looks for the /{id}" form)
        id_pos = -1
        for needle in (
            '"/opportunities/{opportunity_id}"',
            "'/opportunities/{opportunity_id}'",
            '"/{opportunity_id}"',
            "/{opportunity_id}",
        ):
            p = source.find(needle)
            if p != -1:
                id_pos = p
                break
        assert today_pos != -1, "GET /today route not found in router"
        assert id_pos != -1, "GET /{opportunity_id} route not found in router"
        assert today_pos < id_pos, (
            "today route must be declared before /{opportunity_id} to avoid shadowing"
        )


class TestStatusOnlyThroughTransition:
    def test_router_does_not_assign_opp_status_directly(self) -> None:
        """The router must not assign .status on an opp/opportunity variable."""
        source = pathlib.Path("app/api/routers/opportunities.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and target.attr == "status"
                        and isinstance(target.value, ast.Name)
                        and "opp" in target.value.id.lower()
                    ):
                        pytest.fail(
                            f"Line {node.lineno}: router assigns opp.status directly — "
                            "use db.opportunity.transition() instead"
                        )

    def test_router_imports_transition_function(self) -> None:
        source = pathlib.Path("app/api/routers/opportunities.py").read_text()
        assert "from app.db.opportunity import" in source or "db.opportunity" in source, (
            "Opportunities router must import from app.db.opportunity"
        )


class TestTimelineNotEditable:
    def test_no_put_patch_delete_on_events(self) -> None:
        """opportunity_events rows must never be editable through the API."""
        source = pathlib.Path("app/api/routers/opportunities.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ("put", "patch", "delete"):
                    # check if any argument mentions events
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and "event" in str(arg.value).lower():
                            pytest.fail(
                                f"Line {node.lineno}: router exposes a mutating route on events"
                            )
