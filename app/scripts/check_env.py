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
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.settings import (  # noqa: E402
    FEATURE_KEYS,
    FUTURE_KEYS,
    REQUIRED_KEYS,
    Settings,
    looks_like_placeholder,
)

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


def main(settings: Settings | None = None) -> int:
    """Print the report. Returns a process exit code.

    `settings` is injectable so this can be tested: the report's job is to be safe to paste into
    a chat window, and that is only worth asserting against a configuration holding real-looking
    secrets.
    """
    settings = settings or Settings()
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

    print("\nConfigured ahead of the phase that uses it")
    for group, names in FUTURE_KEYS.items():
        statuses = [
            f"{key}={'set' if _status(str(getattr(settings, key, '') or '')) == OK else 'unset'}"
            for key in names
        ]
        print(f"  {group}")
        print(f"    {', '.join(statuses)}")

    print("\nSafety")
    print(f"  SEND_MODE    = {settings.SEND_MODE}")
    if settings.SEND_MODE == "disabled":
        print("                 nothing will be sent, to anyone (this is the default)")
    elif settings.SEND_MODE == "allowlist":
        # The list itself is per org and lives in the database now (ADR-023), so this script
        # cannot count it -- and should not pretend to. Saying where it is beats a number that
        # would be wrong for every workspace but one.
        print("                 may send only to each workspace's own allow-listed addresses")
        print("                 (Settings -> Send mode in the dashboard; no longer in .env)")
    else:
        print("                 LIVE: may send to anyone")
    print(f"  AUTO_APPROVE = {'on — human review is SKIPPED' if settings.AUTO_APPROVE else 'off'}")

    # Who may get an account at all belongs beside who may be emailed. It is reported rather
    # than grouped with the set/unset values above because it always has a value: the question
    # is which one, and "closed" looks identical to "unconfigured" from the outside.
    print(f"  SIGNUP_MODE  = {settings.SIGNUP_MODE}")
    if settings.SIGNUP_MODE == "closed":
        print("                 nobody new can sign up (this is the default)")
    elif settings.SIGNUP_MODE == "allowlist":
        domains = len(settings.signup_allowed_domains)
        print(f"                 anyone at {domains} allow-listed domain(s) may sign up")
    else:
        print("                 OPEN: anyone with a Supabase login gets their own workspace")

    # How long a connected Gmail lasts. Reported here rather than in the set/unset groups above
    # for the same reason as SIGNUP_MODE: it always has a value, and the question is which one.
    # An operator who does not know this number reads a weekly reconnect as a fault.
    if settings.GOOGLE_GRANT_DAYS > 0:
        print(f"  GMAIL GRANT  = {settings.GOOGLE_GRANT_DAYS} days, then each mailbox reconnects")
        print("                 expected: the OAuth client is unpublished on purpose (ADR-018)")
    else:
        print("  GMAIL GRANT  = does not expire (the OAuth client is published)")

    # ADR-005: a DATABASE_URL on the default port very likely belongs to another project —
    # but only on a developer machine, where another project's Postgres may already hold 5432.
    # Inside the Compose network each container has its own address space and `postgres:5432` is
    # both correct and unavoidable, so warning there is noise during a deploy, at exactly the
    # moment somebody is deciding whether to trust what they are reading.
    parts = urlsplit(settings.DATABASE_URL)
    print(f"  DATABASE     = {parts.hostname}:{parts.port}/{(parts.path or '').lstrip('/')}")
    local_host = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if parts.port == 5432 and local_host:
        print("                 WARNING: 5432 is the Postgres default and is very likely")
        print("                 another project's database. This project expects 5433.")

    # A JWKS URL from a different Supabase project than SUPABASE_URL would verify tokens
    # against the wrong keys, which fails confusingly at request time rather than at startup.
    if settings.SUPABASE_URL and settings.SUPABASE_JWKS_URL:
        consistent = settings.SUPABASE_JWKS_URL.startswith(settings.SUPABASE_URL.rstrip("/"))
        print(f"  SUPABASE     = {'URL and JWKS agree' if consistent else 'MISMATCH'}")
        if not consistent:
            print("                 SUPABASE_JWKS_URL is not under SUPABASE_URL - check both")

    if missing_required:
        print(
            f"\n{len(missing_required)} required value(s) still needed: "
            f"{', '.join(missing_required)}"
        )
        print("Set them in .env in the project root. See env.example for the shape.")
        _diagnose_env_file()
        return 1

    print("\nAll required configuration is present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
