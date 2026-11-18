"""A logging `extra` may not use a name `LogRecord` already owns.

**Written because two of them did, and both crashed the code that was logging.**

`logging` merges `extra` into the record's `__dict__` and refuses to overwrite anything already
there, by raising:

    KeyError: "Attempt to overwrite 'created' in LogRecord"

`created` is the record's own timestamp. The clash is silent at import, silent in review, and
fatal at the moment the line runs -- which is always *after* the work, because the log describes
what just happened. So the work is done, committed, and then the handler dies:

- `handle_poll_ats_feed` logged `extra={"created": result.created}` as its last statement. Every
  ATS poll therefore stored its postings and *then* raised, the job failed, retried five times,
  and landed in `dead`. The scheduler stopped polling, and a watchlist with companies on it
  showed nothing -- which reads as "no jobs found" rather than as a crash.
- `POST /booking/{secret}` did the same after `session.commit()`. The booking was recorded and
  the provider got a 500, so it will have retried a webhook that had already succeeded. This is
  one of exactly two ways `MEETING_BOOKED` is ever reached.

**No test could have caught either one, and that is the point.** `Logger.info` checks
`isEnabledFor(INFO)` before it builds a record, and under pytest the effective level is WARNING
-- so the line is skipped, `makeRecord` never runs, and the clash cannot raise. The defect exists
only where INFO logging is configured, which is `app/core/logging.py`, which is production. An
integration test that drove the whole handler would have gone green.

So this cannot be a behavioural test; it has to be a structural one. The property is "no call in
the tree does this", which belongs to the tree -- the same bargain `test_send_guard.py` makes.
"""

from __future__ import annotations

import ast
import logging
import pathlib

import pytest

APP = pathlib.Path("app")

#: Everything `logging.LogRecord.__init__` sets, plus the three the formatter adds later.
#: Taken from the standard library rather than retyped from memory: `makeRecord` refuses any of
#: them, and a list that drifted from the real one would be a guard that lies.
RESERVED = frozenset(
    vars(logging.LogRecord("n", logging.INFO, "p", 1, "m", None, None))
    | {
        "message": None,
        "asctime": None,
        "taskName": None,
    }
)


def _reserved_extra_keys(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for key in keyword.value.keys:
                if isinstance(key, ast.Constant) and key.value in RESERVED:
                    found.append(f"{path.as_posix()}:{key.lineno}: {key.value!r}")
    return found


def test_no_log_extra_collides_with_a_record_attribute() -> None:
    problems: list[str] = []
    for path in sorted(APP.glob("**/*.py")):
        problems.extend(_reserved_extra_keys(path))

    assert not problems, (
        "logging refuses to overwrite these and raises KeyError when the line runs -- after the "
        "work it describes has already happened:\n  " + "\n  ".join(problems)
    )


def test_the_name_this_was_written_about_is_actually_reserved() -> None:
    """The guard's own premise. If `created` were not reserved, the rule would be noise."""
    assert "created" in RESERVED
    assert "message" in RESERVED
    #: Ordinary field names this project uses everywhere, which must stay allowed or the guard
    #: becomes a nuisance somebody switches off.
    assert not RESERVED & {"org_id", "lead_id", "job_id", "provider", "domain", "updated"}


def test_logging_really_does_refuse_it() -> None:
    """Proves the failure mode rather than asserting it, so the rule cannot rot into folklore."""
    logger = logging.getLogger("test_reserved_extra")

    with pytest.raises(KeyError, match="created"):
        logger.makeRecord("n", logging.INFO, "p", 1, "m", None, None, extra={"created": 1})
