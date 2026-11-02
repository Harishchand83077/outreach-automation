"""Structural guards on resume tailoring (F20, ADR-043).

Three claims this slice makes that a behaviour test cannot hold on its own, because each is about
what the code *cannot* do rather than what it does on one input:

1. **A proposal has nowhere to put a sentence that is not a rewording of one fact.** No header
   field, no heading field, no free text -- the same device as `propose_reply` having no
   `subject` argument: the mistake is not expressible.
2. **A version is never written without the rendered document being checked.** The check reads
   the artefact rather than the plan, so it holds when a second producer appears -- which is
   this project's dominant defect, now at nine instances.
3. **The extension cannot mark an application applied.** The acceptance criterion for the slice,
   and it has to be structural: there is no route it can reach that names the column.

Comments and docstrings are stripped before each assertion, because the prose explaining a rule
names every symbol the rule forbids -- and a string constant is ignored for the same reason,
which is the lesson the referral finder's own guard taught the same afternoon.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.resume import tailor

PACKAGE = Path("app/services/resume")
ROUTER = Path("app/api/routers/resumes.py")
EXTENSION_ROUTER = Path("app/api/routers/extension.py")
EXTENSION_PACKAGE = Path("extension/src")


def _trees(*paths: Path) -> list[tuple[Path, ast.Module]]:
    found: list[tuple[Path, ast.Module]] = []
    for path in paths:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for one in files:
            found.append((one, ast.parse(one.read_text(encoding="utf-8"))))
    return found


def _names(tree: ast.Module) -> set[str]:
    """Every identifier and attribute in the module. Strings and comments are not identifiers."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
    return found


class TestAProposalCannotWriteAFreeSentence:
    def test_the_proposal_schema_has_no_header_or_heading_field(self) -> None:
        """A header is name and contact details; a heading is a word from a fixed list. Either
        one, in a model's gift, is a line of prose outside the claim check."""
        fields = set(tailor.ProposedEdit.model_fields)

        assert fields == {"kind", "fact_id", "section", "text", "reason"}
        assert "header" not in fields
        assert "heading" not in fields

    def test_the_section_a_proposal_names_must_be_one_of_ours(self) -> None:
        """An unknown section is corrected to a known one rather than accepted: the vocabulary
        is the deployment's, not the model's."""
        from app.db.models import RESUME_SECTIONS

        ops = tailor._ops_from(
            tailor.ProposedEdits(
                edits=[tailor.ProposedEdit(fact_id="pp1", section="Why me")],
            )
        )

        assert ops[0].section in RESUME_SECTIONS

    def test_only_confirmed_facts_are_offered_to_the_model(self) -> None:
        """True one step earlier than the validator: a model cannot choose a fact it was never
        shown. The validator still checks, because a rule enforced once is a rule with one bug.
        """
        from app.services.resume.validate import KnownFact

        block = tailor._facts_block(
            {
                "pp1": KnownFact("pp1", "Built the payments API.", True),
                "pp2": KnownFact("pp2", "Led a team of four.", False),
            }
        )

        assert "pp1" in block
        assert "pp2" not in block
        assert "Led a team" not in block

    def test_the_posting_is_wrapped_as_untrusted(self) -> None:
        source = (PACKAGE / "tailor.py").read_text(encoding="utf-8")

        assert "untrusted(" in source


class TestTheRenderedDocumentIsAlwaysChecked:
    def test_create_version_checks_the_rendering_it_is_about_to_store(self) -> None:
        tree = ast.parse((PACKAGE / "store.py").read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_version"
        )
        called = _names(ast.Module(body=function.body, type_ignores=[]))

        assert "check_rendered" in called
        assert "validate_ops" in called

    def test_nothing_in_the_package_writes_a_version_but_create_version(self) -> None:
        """The positive half of the guard: a second insert would be a second place the
        validation could be forgotten, and it is the validation that would get forgotten."""
        offenders: list[str] = []
        for path, tree in _trees(PACKAGE, ROUTER):
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "ResumeVersion" and path.name != "store.py":
                        offenders.append(str(path))

        assert offenders == []

    def test_nothing_in_the_package_fetches_anything(self) -> None:
        """A resume is assembled from rows the person owns. No page is read for it."""
        banned = {"httpx", "requests", "urllib", "aiohttp", "socket"}
        offenders: list[str] = []
        for path, tree in _trees(PACKAGE, ROUTER):
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders += [
                        f"{path}: {alias.name}"
                        for alias in node.names
                        if alias.name.split(".")[0] in banned
                    ]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0] in banned:
                        offenders.append(f"{path}: {node.module}")

        assert offenders == []


class TestTheExtensionCannotSetApplied:
    def test_the_extension_router_never_names_the_tracker(self) -> None:
        """The acceptance criterion. An extension that could mark an application sent would have
        the tracker assert the person did something they did not do."""
        tree = ast.parse(EXTENSION_ROUTER.read_text(encoding="utf-8"))
        names = _names(tree)

        for forbidden in ("Application", "applied_at", "set_status", "upsert_application"):
            assert forbidden not in names, f"the extension gateway names {forbidden}"

    def test_the_extension_package_never_names_the_tracker(self) -> None:
        """Checked in the browser code too, because a panel that offered an Applied button would
        be asking for a route somebody would then add."""
        if not EXTENSION_PACKAGE.is_dir():  # pragma: no cover - the package is in the repo
            return
        offenders: list[str] = []
        for path in sorted(EXTENSION_PACKAGE.rglob("*.ts")):
            source = path.read_text(encoding="utf-8")
            stripped = "\n".join(
                line for line in source.splitlines() if not line.strip().startswith("//")
            )
            for forbidden in ("/applications", "applied_at", "resume-versions"):
                if forbidden in stripped:
                    offenders.append(f"{path}: {forbidden}")

        assert offenders == []

    def test_the_tracker_routes_are_on_the_dashboard_door(self) -> None:
        """`CurrentUserDep` is the Supabase door. ADR-039 refuses an `ext:suggest` token at every
        door but `/ext/v1`, so naming the dependency is what makes the criterion structural."""
        source = ROUTER.read_text(encoding="utf-8")

        assert "CurrentUserDep" in source
        assert "ExtensionTokenDep" not in source
