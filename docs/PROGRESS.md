# Progress

Updated 2026-10-09. Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are.** §00 bootstrap and §0 repo analysis are **complete** and committed (`f3b383f`):
every tracked file read, all 43 original issue IDs verified (all confirmed — none "Partly", none
"Not reproducible"), 8 new issues added, baseline measured and tagged `baseline-pre-rewrite`.

The **Phase 0 safety patch** is committed on branch `fix/P0-safety-patch` with **76 tests passing**
and nothing pushed yet. It fixes B1, B2, B3, B4, B6, C2, C8, NEW-1, NEW-2, part of B7 and part of
B18. The prototype can no longer send to a stranger, cannot email error text, cannot act on a reply
without a human, and refuses to start on placeholder credentials.

**Decisions taken** (see `DECISIONS.md`): Postgres in Compose with Supabase for Auth only (ADR-001);
no `BCC_SELF` dependency for send recovery (ADR-002); vertical-slice sequencing rather than the
spec's P0→P10 breadth-first order (ADR-003); `org_id` in the first migration but the members/roles
UI deferred (ADR-004).

**Next up.**
1. Owner action: the two `.claude/settings.json` fixes in `QUESTIONS.md` **Q-0** — one of them
   (`Read(./.env.*)`) currently blocks editing `.env.example`, so **`.env.example` still documents
   the deleted `DRY_RUN_EMAILS` flag and does not mention `SEND_MODE`.** That is the one piece of
   the safety patch that could not be completed; startup refuses the stale flag, so the gap is
   loud rather than silent, but the file is misleading until it is edited.
2. Push `fix/P0-safety-patch` and open the PR (label `needs-human` — it changes sending behaviour).
3. `chore/P0-ci`: `pyproject.toml` dependencies + `uv.lock`, ruff, mypy, GitHub Actions,
   `pre-commit` with `gitleaks`, Dependabot, and the F1–F3 documentation rewrite. Note four
   pre-existing ruff findings are deliberately left for that PR: unused imports in `api_server.py`,
   `logger.py`, `reply_monitor.py` and an unused variable in `database.py`.
4. Then Phase 1 (data layer, state machine, job queue, outbox).

**Live environment notes.** Python 3.12.1 venv at `.venv` with the prototype's deps plus `pytest`,
`pytest-asyncio`, `aiosmtpd`, `pydantic-settings` and `ruff` (left as found; 3.12.12 is also
installed via uv). `gh` is authenticated as `Harishchand83077`. The repo is private. Branch
protection on `main` is **not yet confirmed** — see `QUESTIONS.md` Q-A.

**New lead statuses introduced by the patch** (the legacy flat schema has no enum, so these are
just strings): `SKIPPED_NOT_ALLOWLISTED`, `INSIGHTS_FAILED`, `EMAIL_DRAFT_FAILED`,
`REPLIED_NEEDS_REVIEW`. They are superseded by the Phase 1 state machine.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| §00 | Bootstrap, tooling, permissions | **Done** |
| §0 | Repo analysis, issue verification, baseline | **Done** |
| 0 | Safety patch + CI + docs | Safety patch **done** (76 tests green, unpushed); CI next |
| 1 | Data layer, state machine, job queue, outbox | Not started |
| 2 | Import, persona, campaigns | Not started |
| 3 | LLM router, research engine | Not started |
| 4 | Drafting, grounding, review queue | Not started |
| 5 | Send engine — **first `allowlist` send gate** | Not started |
| 6 | Reply engine, follow-ups | Not started |
| 7 | Auth, multi-tenancy | Not started |
| 8 | UI completion | Not started |
| 9 | Ops: Docker, deploy, logs, alerts, backups | Not started |
| 10 | Rehearsal, go-live gate | Not started |

## Issue register

51 tracked issues. Every one must end `Done`, `Superseded-by-rewrite` (with a test proving the
behaviour) or `Won't fix` (with a reason). Full text and evidence in `ISSUES.md`.

| Status | Count | IDs |
|---|---|---|
| **Done** | 9 | B1, B2, B3, B4, B6, C2, C8, NEW-1, NEW-2 |
| Partly done | 3 | B7 (crash on a long row fixed; header aliasing deferred to Phase 2) · B18 (numeric/bool parsing and `get_event_loop` fixed; `utcnow`, import-time semaphore, `on_event` and the timestamp-format mismatch deferred) · NEW-8 (`.xls` now rejected with a clear message and `.csv` accepted; the `--skip-monitor` docstring is part of the F1–F3 rewrite) |
| Verified, not yet fixed | 39 | everything else |

**What the safety patch actually guarantees** (each with a test):

| Guarantee | Test |
|---|---|
| Nothing can be sent unless `is_send_allowed` permits it, in one function | `test_send_guard.py` |
| `SEND_MODE=disabled` is the default and blocks every recipient | `test_default_mode_blocks`, `test_disabled_blocks_every_recipient` |
| An unlisted recipient is refused and the SMTP server receives nothing | `test_unlisted_recipient_is_blocked_and_nothing_is_received` |
| A blocked or failed send is never recorded as `EMAIL_SENT` | `TestLeadIsNotMarkedSentWhenNothingWasSent` |
| Placeholder credentials fail startup, and `/api/config/check` reports honestly | `TestPlaceholderRejection` |
| A stale `DRY_RUN_EMAILS` in the environment stops startup instead of being ignored | `test_dry_run_emails_in_env_is_rejected` |
| An LLM exception can never become email content | `test_error_text_never_emailed.py` |
| A positive reply sends nothing and never books a meeting | `test_reply_never_autosends.py` |
| An uploaded filename can never reach the filesystem | `test_upload_security.py` |

**Phase 0 CI targets:** E6, and the F1–F3 documentation rewrite.

**Deliberately deferred past Phase 0** (they need the rewritten foundation, not a patch):
B5, B7, B8, B9, B10, B11, B12, B13, B14, B15, B16, B17, B18, C1, C3, C4, C5, C6, C7, all of A, all
of D, all of E except E6, and NEW-3 to NEW-8. Rationale: patching the legacy modules for these
would be throwaway work, and `ISSUES.md` records the evidence so nothing is lost.

## PR log

| PR | Branch | Issues | Status |
|---|---|---|---|
| — | — | — | none opened yet |
