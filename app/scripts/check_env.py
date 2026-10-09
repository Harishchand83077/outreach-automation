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
        return 1

    print("\nAll required configuration is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
