"""Every `var(--x)` in the dashboard names a token that exists (S17.1).

**Six did not**, in three files, and the symptom was invisible: `var(--textMuted)` with no
fallback resolves to *nothing*, so the browser drops the declaration and the element inherits.
The sign-in page's muted paragraphs and the settings tick-boxes were rendering in body colour
rather than muted colour — a flat hierarchy on the first screen anybody sees, with no error
anywhere and nothing in a build log.

`index.css` states the rule the drift broke: **a component never uses a raw colour, and a shade
a component needs belongs in the token file.** The rule was followed — there is not a stray hex
in the tree — and what drifted was the *names*, `--textMuted` against `--text-muted` and
`--text-2` against `--text-muted`. That produces the same symptom as a raw colour would, which
is text that does not respond to the theme toggle, and it is harder to see.

A fourth dashboard guard, beside "the routes exist", "the fields exist" and "the setters are
declared". Same bargain as those three: a source check rather than a tool, because stylelint
would catch this and more and is a dependency tree to audit for the life of the project.
"""

from __future__ import annotations

import pathlib
import re

FRONTEND = pathlib.Path("frontend/src")

#: Where a token may be defined. Both files, because `app.css` legitimately defines a few of its
#: own for one component's internal use.
TOKEN_FILES = ("index.css", "app.css", "chrome.css")

#: A reference: `var(--name` — the closing paren and any fallback are not needed to read the
#: name, and a fallback is deliberately *not* treated as making a missing token acceptable. A
#: fallback is for a value that varies by context, not for a name somebody got wrong.
_USED = re.compile(r"var\(\s*(--[A-Za-z0-9-]+)")

#: A definition: a custom property at the start of a declaration.
_DEFINED = re.compile(r"^\s*(--[A-Za-z0-9-]+)\s*:", re.MULTILINE)


def _at_rule_body(css: str, header: str) -> str:
    """The text inside an at-rule, found by matching braces rather than by guessing at layout.

        A regex ending in `}
    }` depends on indentation, and the first time the file is reformatted
        the guard silently stops looking at anything. Counting braces is four lines.
    """
    start = css.find(header)
    if start < 0:
        return ""
    opened = css.find("{", start)
    depth = 0
    for index in range(opened, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[opened + 1 : index]
    return ""


def _read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def _defined() -> set[str]:
    found: set[str] = set()
    for name in TOKEN_FILES:
        path = FRONTEND / name
        if path.exists():
            found.update(_DEFINED.findall(path.read_text(encoding="utf-8")))
    return found


def _sources() -> list[pathlib.Path]:
    return sorted(
        path
        for pattern in ("*.css", "*.jsx", "*.js")
        for path in FRONTEND.glob(pattern)
        if not path.name.endswith(".test.js")
    )


def test_every_referenced_token_is_defined() -> None:
    defined = _defined()
    missing: list[str] = []

    for path in _sources():
        for name in sorted(set(_USED.findall(path.read_text(encoding="utf-8")))):
            if name not in defined:
                missing.append(f"{path.name}: {name}")

    assert missing == [], (
        "these refer to a token that is defined nowhere, so the declaration is dropped and the "
        "element inherits -- which is invisible rather than broken:\n  " + "\n  ".join(missing)
    )


def test_the_guard_is_looking_at_something() -> None:
    """A vacuity check, because a regex that stopped matching would make the test above pass
    for every input. The `tools/list` assertion that compared a list with itself is why every
    structural guard in this repository now has one of these."""
    defined = _defined()
    assert len(defined) > 40, f"only found {len(defined)} tokens; the definition regex is wrong"

    used: set[str] = set()
    for path in _sources():
        used.update(_USED.findall(path.read_text(encoding="utf-8")))
    assert len(used) > 40, f"only found {len(used)} references; the usage regex is wrong"


def test_a_planted_typo_would_be_caught() -> None:
    """Discriminating, asserted directly rather than by trusting the test above.

    Run against the real token set, so it also fails if somebody ever defines a token called
    `--definitely-not-a-token`, which would be a stranger problem.
    """
    assert "--definitely-not-a-token" not in _defined()


def test_every_theme_defines_the_same_colour_tokens() -> None:
    """The property the whole theme design rests on: dark and light define the *same names*, so
    no component ever asks which theme is on.

    A token defined in one block and not the other is a rectangle that keeps the other theme's
    colour -- the exact failure the token system exists to prevent, and one a human reviewer
    will not spot in a 150-line diff of hex values.
    """
    css = _read("index.css") + _read("chrome.css")

    def names(selector: str) -> set[str]:
        found: set[str] = set()
        for block in re.findall(re.escape(selector) + r"[^{]*\{(.*?)\n\}", css, re.DOTALL):
            found.update(_DEFINED.findall(block))
        return found

    dark = names(":root[data-theme='dark']")
    light = names(":root[data-theme='light']")

    assert dark, "no dark theme block found; this check is looking at nothing"
    assert light, "no light theme block found; this check is looking at nothing"

    only_dark = sorted(dark - light)
    only_light = sorted(light - dark)
    assert only_dark == [], f"defined only in dark: {only_dark}"
    assert only_light == [], f"defined only in light: {only_light}"


def test_the_unset_theme_is_not_a_mixture_of_both() -> None:
    """A visitor who has never touched the toggle gets one theme, not half of each.

    **This is the guard for a bug that shipped into a screenshot.** `:root[data-theme='light']`
    matches only once somebody has *used* the toggle; before that, a light-system visitor picks
    up the light base tokens from the `prefers-color-scheme` block and whatever `:root` says for
    everything else. When the brand tokens were added to `:root` and to `[data-theme='light']`
    but not to the media block, that visitor got a dark hero gradient under dark navy text --
    invisible, with no error anywhere.

    So every token with a light value has to appear in both places. The media block is allowed
    to define *more* than the attribute block (it also carries the base values), which is why
    this is a subset check in one direction only.
    """
    css = _read("index.css") + _read("chrome.css")

    attribute_light: set[str] = set()
    for block in re.findall(r":root\[data-theme='light'\][^{]*\{(.*?)\n\}", css, re.DOTALL):
        attribute_light.update(_DEFINED.findall(block))

    body = _at_rule_body(css, "@media (prefers-color-scheme: light)")
    assert body, "no prefers-color-scheme: light block found; this check is looking at nothing"
    unset_light = set(_DEFINED.findall(body))

    assert attribute_light, "no light theme block found; this check is looking at nothing"

    missing = sorted(attribute_light - unset_light)
    assert missing == [], (
        "these have a light value that only applies once the toggle has been used, so a "
        "visitor on a light system sees the dark value for them mixed with light surfaces:\n  "
        + "\n  ".join(missing)
    )


def test_no_class_is_styled_by_two_stylesheets() -> None:
    """One owner per class, which is `chrome.css`'s whole reason for existing as a third file.

    **Written after breaking it**: `.page-title` was defined in `app.css` and again in
    `chrome.css`, so which rule won depended on import order -- a cascade somebody has to hold
    in their head, and the thing that file's own header says it does not do. Two files styling
    one class is not always wrong, but it is never something to discover.
    """
    def classes(name: str) -> set[str]:
        css = _read(name)
        # Top-level class selectors only: a line that starts a rule with `.name`. Nested and
        # compound selectors (`.card h3`, `.badge[data-tone]`) are deliberately not counted,
        # because extending another file's class with a qualifier is the normal way to do it.
        return set(re.findall(r"^\.([a-z][a-z0-9-]*)\s*\{", css, re.MULTILINE))

    shared = sorted(classes("app.css") & classes("chrome.css"))
    assert shared == [], (
        "defined at the top level of both app.css and chrome.css, so which one wins depends on "
        f"import order: {shared}"
    )
