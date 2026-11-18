"""A screen that uses something from another file must import it.

**This is the guard the `App.jsx` split needed and the toolchain does not provide.** There is no
ESLint in `frontend/`, and neither Vite nor esbuild fails a build on an undefined identifier: the
bundle is produced, the page loads, and the first render that reaches `<Field />` throws
`Field is not defined`. Moving a component out of a 5,374-line file is a hundred opportunities to
make exactly that mistake, and the dashboard suite renders nothing, so nothing else would catch
it.

It is also the project's signature defect once more -- a correct producer, a correct consumer and
nothing checking they agreed -- pointed at the boundary a refactor creates.

**Two reference shapes, deliberately.** `<Name` for a component and `name(` for a call cover
every way one of these modules uses another's export, and neither appears in English prose, so
the check needs no JavaScript parser to stay honest about strings. A name used only as a bare
value -- passed as a prop, compared against -- is not checked here; the two shapes above are
where this goes wrong in practice, and a regex that chased bare identifiers would fail on every
sentence containing the word "Review".
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path("frontend/src")
#: Every module a screen could import from, test files excluded -- a test may legitimately
#: reference a name it does not import, through a mock.
SOURCES = sorted(
    path
    for path in [*SRC.glob("**/*.jsx"), *SRC.glob("**/*.js")]
    if not path.name.endswith(".test.js") and not path.name.endswith(".test.jsx")
)

_EXPORT_NAMED = re.compile(r"^export\s+(?:async\s+)?(?:function|const|let|var|class)\s+(\w+)", re.M)
_EXPORT_DEFAULT = re.compile(r"^export\s+default\s+(?:async\s+)?function\s+(\w+)", re.M)
_EXPORT_LIST = re.compile(r"^export\s*\{([^}]*)\}", re.M)

#: `import X, { a, b as c } from "./mod"` -> the local names X, a and c.
_IMPORT = re.compile(r"import\s+([^;]*?)\s+from\s*['\"]([^'\"]+)['\"]", re.S)

#: Anything declared anywhere in the file, at any indentation, including inside a function.
_DECLARED = re.compile(r"\b(?:function|const|let|var|class)\s+(\w+)")

_JSX_USE = re.compile(r"<([A-Z]\w*)")
_CALL_USE = re.compile(r"(?<![\w.$])([A-Za-z_]\w*)\s*\(")

#: Names the language or the platform provides, which no file imports.
_GLOBALS = frozenset(
    {
        "Array",
        "Boolean",
        "Date",
        "Error",
        "FormData",
        "Intl",
        "JSON",
        "Map",
        "Math",
        "Number",
        "Object",
        "Promise",
        "Set",
        "String",
        "URL",
        "URLSearchParams",
        "fetch",
        "isNaN",
        "parseFloat",
        "parseInt",
        "setTimeout",
        "clearTimeout",
        "structuredClone",
    }
)


def _strip_comments(source: str) -> str:
    """Blank `//` and `/* */` runs, keeping line count, so prose cannot look like a reference."""
    source = re.sub(r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", " ", m.group(0)), source, flags=re.S)
    return re.sub(r"//[^\n]*", "", source)


def _exports(source: str) -> set[str]:
    names = set(_EXPORT_NAMED.findall(source)) | set(_EXPORT_DEFAULT.findall(source))
    for group in _EXPORT_LIST.findall(source):
        for part in group.split(","):
            name = part.strip().split(" as ")[-1].strip()
            if name:
                names.add(name)
    return names


def _imported(source: str) -> set[str]:
    found: set[str] = set()
    for clause, _module in _IMPORT.findall(source):
        if clause.strip().startswith("*"):
            found.add(clause.split("as")[-1].strip())
            continue
        head, _, braced = clause.partition("{")
        default = head.strip().rstrip(",").strip()
        if default:
            found.add(default)
        for part in braced.rstrip("}").split(","):
            name = part.strip().split(" as ")[-1].strip()
            if name:
                found.add(name)
    return found


def _owner_map() -> dict[str, pathlib.Path]:
    """Every name another module exports, and which file exports it."""
    owners: dict[str, pathlib.Path] = {}
    for path in SOURCES:
        for name in _exports(path.read_text(encoding="utf-8")):
            owners.setdefault(name, path)
    return owners


def test_no_screen_uses_another_module_without_importing_it() -> None:
    owners = _owner_map()
    problems: list[str] = []

    for path in SOURCES:
        raw = path.read_text(encoding="utf-8")
        code = _strip_comments(raw)
        here = set(_DECLARED.findall(code)) | _imported(raw) | _exports(raw)
        used = set(_JSX_USE.findall(code)) | set(_CALL_USE.findall(code))

        for name in sorted(used - here - _GLOBALS):
            owner = owners.get(name)
            if owner is None or owner == path:
                continue
            problems.append(
                f"{path.name} uses {name!r}, which {owner.name} exports, without importing it"
            )

    assert not problems, (
        "a module reaches for something it never imported. Vite builds this happily and the "
        "page throws on the first render that touches it:\n  " + "\n  ".join(problems)
    )


def test_the_guard_can_see_the_modules_it_is_written_for() -> None:
    """If this ever reads nothing, its passing means nothing."""
    names = {path.name for path in SOURCES}
    assert {"App.jsx", "ui.jsx", "api.js"} <= names, names
    owners = _owner_map()
    assert "PageHeader" in owners
    assert "getJson" in owners
