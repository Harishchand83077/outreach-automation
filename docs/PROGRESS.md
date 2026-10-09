# Progress

Updated 2026-10-10 (Phase 1 in progress). Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are. Phase 0 is complete.**

- §00 bootstrap and §0 repo analysis: done. Every tracked file read, all 43 original issue IDs
  verified (all confirmed — none "Partly", none "Not reproducible"), 8 new issues added, baseline
  measured and tagged `baseline-pre-rewrite`.
- **Safety patch: merged to `main`** (squashed as `fb64407`). The prototype can no longer send to a
  stranger, cannot email error text, cannot act on a reply without a human, and refuses to start on
  placeholder credentials.
- **CI and docs: on branch `chore/P0-ci`**, pushed, PR not yet opened. Everything green locally:
  ruff, ruff format, mypy strict, 76 tests, none touching the network.

**Decisions taken** (see `DECISIONS.md`): Postgres in Compose with Supabase for Auth only (ADR-001);
no `BCC_SELF` dependency for send recovery (ADR-002); vertical-slice sequencing rather than the
spec's P0→P10 breadth-first order (ADR-003); `org_id` in the first migration but the members/roles
UI deferred (ADR-004).

**Next up.**
1. Open and merge the `chore/P0-ci` PR. First CI run will be the first ever on this repo, so expect
   to fix workflow details (the `uv sync --frozen` step and the gitleaks action are the likely ones).
2. Then **Phase 1**: the `app/` data layer — SQLAlchemy models with `org_id` everywhere, Alembic
   migration 1, `domain/state.transition()` as the single writer of `leads.state`, the `jobs` queue
   claimed with `FOR UPDATE SKIP LOCKED`, lease and reaper, and the outbox with a unique
   `idempotency_key`. Targets B9, B12, B16, E2, E3, E4, and B8 structurally.
3. Owner action still outstanding: the two `.claude/settings.json` fixes in `QUESTIONS.md` **Q-0**.
   Nothing is blocked by them any more — the template was renamed to `env.example` (undotted) so it
   stays editable — but `"Bash(curl:*|*sh*)"` is invalid syntax and currently matches nothing.

**Live environment notes.** Python 3.12.1 venv at `.venv`; dependencies now come from
`pyproject.toml` + `uv.lock` (`uv sync --all-groups`). `gh` is authenticated as `Harishchand83077`.
The repo is private. `.env` is fully populated and verified via `python -m app.scripts.check_env`,
with `SEND_MODE=disabled`. Branch protection on `main` is **not yet confirmed** — see `QUESTIONS.md`
Q-A. GitHub reported 16 Dependabot vulnerabilities against the old unpinned `requirements.txt`;
re-check once `chore/P0-ci` is merged.

**Note on local `main`.** The safety patch was squash-merged, so local `main` diverged from
`origin/main`. Create new branches from `origin/main`, not from local `main`.

**New lead statuses introduced by the patch** (the legacy flat schema has no enum, so these are
just strings): `SKIPPED_NOT_ALLOWLISTED`, `INSIGHTS_FAILED`, `EMAIL_DRAFT_FAILED`,
`REPLIED_NEEDS_REVIEW`. They are superseded by the Phase 1 state machine.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| §00 | Bootstrap, tooling, permissions | **Done** |
| §0 | Repo analysis, issue verification, baseline | **Done** |
| 0 | Safety patch + CI + docs | Safety patch **done** (76 tests green, unpushed); CI next |
| 1 | Data layer, state machine, job queue, outbox | **In progress** — schema, state machine and queue done (162 tests); outbox claim/mark next |
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
| **Done** | 14 | B1, B2, B3, B4, B6, C2, C8, E6, F1, F2, F3, NEW-1, NEW-2, NEW-5 |
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
