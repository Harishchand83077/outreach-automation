"""Report which environment variables are set, by NAME only.

    python -m app.scripts.check_env

This exists so configuration can be verified without anyone — or anything — reading `.env`.
It prints a status per variable name and never prints, logs or returns a value. The closest it
comes is saying that a value still looks like a placeholder copied from `.env.example`.

Exit code 0 if everything required is present and real, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.config import REQUIRED_KEYS, Settings, looks_like_placeholder  # noqa: E402

#: Not required to start, but each disables a feature if absent.
FEATURE_KEYS: dict[str, str] = {
    "IMAP_EMAIL": "replies are not monitored",
    "IMAP_PASSWORD": "replies are not monitored",
    "CALENDAR_LINK": "drafts have no booking link to call to action with",
    "SEND_ALLOWLIST": "SEND_MODE=allowlist can reach nobody",
}

OK = "OK     "
MISSING = "MISSING"
PLACEHOLDER = "PLACEHOLDER"


def _status(value: str) -> str:
    if not (value or "").strip():
        return MISSING
    if looks_like_placeholder(value):
        return PLACEHOLDER
    return OK


def _diagnose_env_file() -> None:
    """Explain why a .env might not be taking effect. Prints names, never values."""
    print("\nWhere .env is being read from")
    expected = REPO_ROOT / ".env"
    print(f"  Looked for: {expected}")

    if not expected.is_file():
        print("  NOT FOUND at that path.")
        # The usual Windows causes: a hidden extension, or the file saved elsewhere.
        strays = sorted(
            p.name
            for p in REPO_ROOT.iterdir()
            if p.is_file() and p.name.lower().startswith(".env") and p.name != ".env.example"
        )
        if strays:
            print(f"  Similar files here: {', '.join(strays)}")
            print("  If one of those is your file, rename it to exactly '.env'.")
            print("  (Windows Explorer hides known extensions, so '.env.txt' looks like '.env'.)")
        else:
            print("  No .env-like file in the project root at all.")
        print(f"  Create it at: {expected}")
        return

    raw = expected.read_bytes()
    print(f"  Found it: {len(raw)} bytes")
    if not raw.strip():
        print("  The file is EMPTY.")
        return

    text = raw.decode("utf-8-sig", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        print("  The file is UTF-16 encoded, which python-dotenv cannot read.")
        print("  Re-save it as UTF-8 (in Notepad: Save As, Encoding = UTF-8).")
        return
    if raw.startswith(b"\xef\xbb\xbf"):
        print("  The file starts with a UTF-8 byte-order mark, so python-dotenv cannot")
        print("  parse line 1 (you will see 'could not parse statement starting at line 1').")
        print("  Harmless if line 1 is a comment, but re-save without the BOM to be safe:")
        print("  Notepad Save As -> Encoding 'UTF-8' (not 'UTF-8 with BOM').")

    named: list[str] = []
    commented: list[str] = []
    empty: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.lstrip("#").strip()
        if not key:
            continue
        if stripped.startswith("#"):
            commented.append(key)
        else:
            named.append(key)
            if not value.strip():
                empty.append(key)

    print(f"  Variable names set in the file: {', '.join(named) or '(none)'}")
    if empty:
        print(f"  Present but with an EMPTY value: {', '.join(empty)}")
    if commented:
        print(f"  Commented out, so ignored: {', '.join(commented)}")
    if not named:
        print("  Every line is a comment or blank, so no value is set.")
    print("  (Values are never read or printed by this script.)")


def main() -> int:
    settings = Settings()
    missing_required: list[str] = []

    print("Required")
    for key in REQUIRED_KEYS:
        status = _status(str(getattr(settings, key, "") or ""))
        print(f"  {status:<12} {key}")
        if status != OK:
            missing_required.append(key)

    print("\nOptional")
    for key, feature in FEATURE_KEYS.items():
        status = _status(str(getattr(settings, key, "") or ""))
        note = "" if status == OK else f"  (without it: {feature})"
        print(f"  {status:<12} {key}{note}")

    print("\nSafety")
    print(f"  SEND_MODE    = {settings.SEND_MODE}")
    if settings.SEND_MODE == "disabled":
        print("                 nothing will be sent, to anyone (this is the default)")
    elif settings.SEND_MODE == "allowlist":
        count = len(settings.send_allowlist)
        print(f"                 may send only to {count} allow-listed address(es)")
    else:
        print("                 LIVE: may send to anyone")
    print(f"  AUTO_APPROVE = {'on — human review is SKIPPED' if settings.AUTO_APPROVE else 'off'}")

    if missing_required:
        print(
            f"\n{len(missing_required)} required value(s) still needed: "
            f"{', '.join(missing_required)}"
        )
        print("Set them in .env in the project root. See .env.example for the shape.")
        _diagnose_env_file()
        return 1

    print("\nAll required configuration is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
