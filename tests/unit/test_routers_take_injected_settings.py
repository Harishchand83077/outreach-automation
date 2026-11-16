"""A router reads the `Settings` it was given, never the module-level singleton (ADR-061).

ADR-041 found this and named it: sealing a provider key needs `CREDENTIAL_KEY`, and a router
reaching for the `config` singleton means **an app constructed with its own `Settings` -- which
is every test app -- is partially ignoring them.** A correct producer (`create_app(settings)`)
and consumers reading a different object: this project's signature shape, in the layer where it
is hardest to see, because in production the two happen to be the same object.

**Where it bites is not production, it is anywhere the two diverge**, and the symptoms are the
quiet kind:

- `unsubscribe` signs and verifies a public URL with a key derived from `CREDENTIAL_KEY`, and
  the send loop builds that URL from its own object. Two different keys means every opt-out is
  refused -- answering 200 after doing nothing, because ADR-033 deliberately made a refusal
  indistinguishable from a success so the endpoint is not an oracle. **Nothing anywhere would
  say so.**
- `operations` reported the send mode by calling `effective_send_mode(config, ...)`, so
  `/admin/sending-config` answered from a different object than the gate reads. Hard rule 1
  says the mode resolves in exactly one function; it does, and the screen was asking it the
  wrong question.

There are two legitimate readers of the singleton and the rule is about *where*: a **worker**
has no request to inject from and reads the process configuration at start, and `app.py` builds
the application. Neither is a router.
"""

from __future__ import annotations

import ast
import pathlib

ROUTERS = sorted(pathlib.Path("app/api/routers").glob("*.py"))

#: The import that makes the mistake expressible. `Settings` the *class* is fine -- a type
#: annotation is not a read -- and so is `app.core.settings` for anything else it exports.
_SINGLETON = "config"


def _imports_singleton(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.core.settings"):
            if any(alias.name == _SINGLETON for alias in node.names):
                return True
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.core.settings":
                    # `import app.core.settings` then `app.core.settings.config` -- the same
                    # read by a longer route.
                    return True
    return False


def test_there_is_something_to_check() -> None:
    """A glob that stops matching makes every assertion below pass trivially."""
    assert len(ROUTERS) > 10, f"only found {len(ROUTERS)} routers; the glob is broken"
    assert any("operations" in path.name for path in ROUTERS)


def test_no_router_imports_the_settings_singleton() -> None:
    """The whole rule, in one assertion.

    A router that needs configuration takes `SettingsDep`, which resolves to the object the
    application was actually constructed with. Importing `config` is how a route comes to answer
    from somewhere else, and it is invisible in production because the two are the same object
    there.
    """
    offenders = [
        path.name
        for path in ROUTERS
        if _imports_singleton(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == [], (
        "these routers read the module-level settings singleton instead of the Settings the "
        f"app was built with; take `SettingsDep` instead: {offenders}"
    )


def test_somebody_actually_takes_the_dependency() -> None:
    """The other half, so the rule cannot be satisfied by routers that read no configuration.

    Without this, deleting every `SettingsDep` in the tree would make the test above pass -- the
    vacuity trap that left `tools/list`'s ordering assertion comparing a list with itself.
    """
    takers = [path.name for path in ROUTERS if "SettingsDep" in path.read_text(encoding="utf-8")]
    assert len(takers) >= 5, (
        f"almost nothing takes injected settings, which is suspicious: {takers}"
    )
