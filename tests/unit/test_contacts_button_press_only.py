"""A contact lookup spends the user's money, so nothing but a person may start one (F9, ADR-041).

This is the structural half of F9's safety argument, and it is the same device as
`test_send_guard.py`: the rule is asserted over the whole tree with `ast`, so it holds against
code nobody has written yet. There are three claims.

1. **No background caller.** No job kind looks up contacts, the `Scheduler` has no pass that
   does, and no worker handler imports the lookup module. A lookup that could run by itself is
   one that bills somebody for work they did not ask for -- and the failure would be invisible
   until a provider's invoice arrived.
2. **No shared key.** `Settings` holds no provider key, and the lookup refuses without a stored
   credential. ADR-023 removed the deployment's mailbox for this reason: while one identity lives
   in the environment, every question about who may use it is a rule to maintain.
3. **No secret leaves.** No route returns a key or a provider's `invalid_reason`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


def _modules(*folders: str) -> list[tuple[Path, ast.Module]]:
    out: list[tuple[Path, ast.Module]] = []
    for folder in folders:
        for path in sorted((APP / folder).rglob("*.py")):
            out.append((path, ast.parse(path.read_text(encoding="utf-8"))))
    return out


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


class TestNothingInTheBackgroundCanSpendACredit:
    def test_no_worker_module_imports_the_lookup(self) -> None:
        """The worker is everything that runs without a person. None of it may reach this."""
        offenders = [
            path.name
            for path, tree in _modules("worker")
            if any("contacts" in name and "lookup" in name for name in _imports(tree))
        ]

        assert offenders == []

    def test_no_job_kind_is_a_contact_lookup(self) -> None:
        """A job kind is the only way to put work on the queue, so the absence is the guard."""
        from app.db.queue import JobKind

        kinds = {
            name: value
            for name, value in vars(JobKind).items()
            if not name.startswith("_") and isinstance(value, str)
        }
        assert len(kinds) > 5  # the guard is over a real list, not an empty one
        for name, value in kinds.items():
            assert "contact" not in value, (
                f"JobKind.{name} would let a contact lookup run in the background. A lookup "
                "spends the user's own provider credits and must stay a button press (ADR-041)."
            )

    def test_the_scheduler_does_not_look_up_contacts(self) -> None:
        source = (APP / "worker" / "scheduler.py").read_text(encoding="utf-8")

        assert "contacts" not in source

    def test_only_a_router_reaches_the_lookup(self) -> None:
        """Everything that calls `lookup.run` is a route. Written positively, so a new caller
        somewhere else fails rather than being quietly allowed."""
        callers = [
            path.relative_to(ROOT).as_posix()
            for path, tree in _modules("api", "services", "worker", "domain", "db", "scripts")
            if "lookup.run(" in path.read_text(encoding="utf-8") and path.name not in ("lookup.py",)
        ]

        assert callers == ["app/api/routers/contacts.py"]


class TestThereIsNoSharedKey:
    def test_settings_holds_no_provider_key(self) -> None:
        from app.core.settings import Settings

        fields = set(Settings.model_fields)
        for provider in ("APOLLO", "HUNTER", "SNOV"):
            offenders = {name for name in fields if provider in name.upper()}
            assert offenders == set(), (
                f"{offenders} would be a key belonging to the deployment rather than to a "
                "person. ADR-023 removed the last of those on purpose."
            )

    def test_the_lookup_resolves_its_key_from_the_database_only(self) -> None:
        """`keys.live` is the only source, and it is org-scoped (ADR-004)."""
        source = (APP / "services" / "contacts" / "lookup.py").read_text(encoding="utf-8")

        assert "keys.live(" in source
        # The module-level `config` singleton is deliberately not imported: it is built at
        # import time and reads `.env`, so a route reading it would ignore the settings its app
        # was constructed with. The lookup takes `Settings` as a parameter instead.
        assert "import config" not in source
        assert "config." not in source
        for provider in ("APOLLO", "HUNTER", "SNOV"):
            assert provider not in source.upper().replace("SNOVPROVIDER", ""), (
                f"lookup.py names {provider}, which would be a key in the environment."
            )


class TestNoSecretAndNoProviderMessageLeaves:
    @pytest.mark.parametrize("forbidden", ["invalid_reason", "secret", "api_key"])
    def test_the_router_never_returns_one(self, forbidden: str) -> None:
        """`invalid_reason` can quote a provider's reply, and a response reaches a screenshot.

        `api_key` appears as a request *parameter* on the save route, which is the one place it
        legitimately does, so the check is on what is returned: no `return` statement anywhere in
        the router may mention any of these.
        """
        tree = ast.parse((APP / "api" / "routers" / "contacts.py").read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Return) and node.value is not None:
                rendered = ast.unparse(node.value)
                assert forbidden not in rendered, (
                    f"a response in contacts.py mentions {forbidden!r}: {rendered[:120]}"
                )

    def test_the_credential_never_prints_itself(self) -> None:
        from app.db.models import ProviderCredential

        row = ProviderCredential(provider="hunter", kind="api_key", secret=b"ciphertext")

        assert "ciphertext" not in repr(row)
