"""Three properties of the platform scope that no behavioural test can hold down.

The platform view is the only thing in this system that deliberately looks across orgs. ADR-004
is otherwise enforced by the shape of the code -- `org_id` comes from a membership row and from
nowhere else -- so the one place that steps outside it needs the exception to be *narrow*, and
narrow is a property of the source rather than of a response body.

Same bargain as `test_send_guard.py` and `test_dashboard_state_is_declared.py`: read the tree,
assert the shape. A behavioural test can only prove that today's code does the right thing with
today's routes.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.api import deps

API = Path(deps.__file__).resolve().parent
APP = API.parent


def _source_without_comments(path: Path) -> str:
    """Prose explaining a rule must not be able to satisfy an assertion about the code."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


class TestTheOperatorIdentityCannotNameAnOrg:
    """The dependency returns something that *cannot* be used to reach a repository call.

    `CurrentUser` carries a `ctx` property, which is how every org-scoped route gets its
    `OrgContext`. The operator identity deliberately has no such thing: not an empty one, not a
    nullable one -- none. So a route that tried to pass an operator where a user belongs fails at
    the attribute, at import-time-ish rather than at runtime against a real database.
    """

    def test_it_has_no_ctx_and_no_org_id(self):
        fields = set(deps.CurrentOperator.__dataclass_fields__)

        assert "org_id" not in fields
        assert not hasattr(deps.CurrentOperator, "ctx")
        assert not hasattr(deps.CurrentOperator, "org_id")

    def test_it_is_not_a_kind_of_current_user(self):
        """Inheritance would hand it `ctx` by the back door."""
        assert not issubclass(deps.CurrentOperator, deps.CurrentUser)
        assert deps.CurrentOperator is not deps.CurrentUser

    def test_it_is_frozen(self):
        """A caller must not be able to write an org onto the identity mid-request."""
        assert deps.CurrentOperator.__dataclass_params__.frozen


class TestTheRouterCannotReachTenantData:
    """The platform router may count rows; it may not use the repository.

    Every function in `app/db/repo.py` takes an `OrgContext` and is therefore *about* one org's
    data. The platform view answers questions about orgs in aggregate, so it has no business
    there -- and importing it is how the first "just this once" row of lead data would arrive.
    """

    def test_it_does_not_import_the_repository(self):
        source = _source_without_comments(API / "routers" / "platform.py")
        tree = ast.parse(source)

        imported = {
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        }

        assert "app.db.repo" not in imported
        assert not any(name.startswith("app.services.outcomes") for name in imported)

    def test_it_names_no_lead_or_draft_text_column(self):
        """A grep, deliberately blunt.

        These are the attribute names that would carry somebody's *content* into the response --
        a person's name, a company, an email body, a campaign's strategy, the sentence somebody
        typed when they paused their workspace. A count never needs any of them, so naming one is
        the signal, whatever the intent was.

        `invalid_reason` is **not** on this list, and the omission is deliberate rather than an
        oversight. A mailbox counts as connected when its credential has not been refused, so the
        router has to read that column to answer the one question this screen exists for. What
        must never happen is *returning* it, since the text can quote a provider's reply -- and
        that is asserted against the real response body in
        `tests/integration/test_platform_scope.py`, which is where it can actually be checked.
        """
        source = _source_without_comments(API / "routers" / "platform.py")

        for forbidden in (
            "email_norm",
            "email_raw",
            "company_name",
            "full_name",
            ".body",
            ".subject",
            "persona",
            "pause_reason",
        ):
            assert forbidden not in source, f"the platform router names {forbidden}"


class TestNothingCanGrantTheFlag:
    """`platform_operators` is written by an INSERT and by nothing else.

    A route that granted it would be a privilege escalation with no ceiling: the scope sits
    *above* every org, so there is no role that could legitimately hand it out. Keeping it to
    SQL means the only person who can create one is somebody who already has the database.
    """

    def test_no_api_module_constructs_a_platform_operator(self):
        offenders = []
        for path in API.rglob("*.py"):
            if "PlatformOperator(" in _source_without_comments(path):
                offenders.append(path.name)

        assert offenders == [], f"these construct a PlatformOperator: {offenders}"

    def test_no_service_constructs_one_either(self):
        """Including `provision()`, which is the one place that already creates memberships."""
        offenders = []
        for path in (APP / "services").rglob("*.py"):
            if "PlatformOperator(" in _source_without_comments(path):
                offenders.append(path.name)

        assert offenders == []

    def test_the_only_writes_in_the_tree_are_the_migration_and_the_tests(self):
        source = inspect.getsource(deps)

        assert "PlatformOperator(" not in source
