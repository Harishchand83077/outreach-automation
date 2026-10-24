"""Generate a Fernet key for `CREDENTIAL_KEY`.

    python -m app.scripts.make_key

Prints one key and nothing else, so it can be piped. It is **not** written to `.env` by this
script and never will be: hard rule 2 says never read or write `.env`, and a tool that edits the
secrets file is a tool that will one day print it.

Keep the key. Every mailbox credential encrypted under it becomes unreadable if it is lost, and
the only recovery is for each person to reconnect their mailbox.
"""

from __future__ import annotations

from cryptography.fernet import Fernet


def main() -> None:
    print(Fernet.generate_key().decode("ascii"))


if __name__ == "__main__":  # pragma: no cover - entry point
    main()
