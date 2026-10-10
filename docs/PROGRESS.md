# Progress

Updated 2026-10-10 (Phase 6 complete). Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are. Phases 0 through 6 are complete and merged, except Phase 6 which is on its
branch.** 952 tests pass; ruff and mypy strict are clean.

- Phase 6a (merged, `b1d6aa4`): inbound mail parsing — type decided from headers first, hard and
  soft bounces split, quoted text stripped — plus the ten reply labels, the keyword opt-out rules
  including the Hinglish forms, and the `reconcile` precedence (bounce > auto-reply > keyword
  opt-out > model).
- Phase 6b (branch `feat/P6b-reply-actions`, this session): the `ImapPoller`, the reply service
  that matches, stores and acts, the LLM classifier, the reply drafter, the ingest loop and the
  ADR-002 send recovery.

**What Phase 6b closes:** A6, A8, B5, B10, B11, B17, C5, D2 — each with a test named in
`ISSUES.md`. A7 (follow-ups) closed in Phase 5.

**Three design gaps the tests found, all fixed here rather than worked around:**
1. `messages.lead_id` was `NOT NULL`, which made §10.2's "unmatched mail is never deleted"
   impossible. Now nullable, with `ck_messages_out_has_lead` keeping every *outbound* row attached
   to a lead (migration `e6ad2939ee88`).
2. The state machine had no `SENT → NOT_INTERESTED` edge, so a clear "no thanks" could not close a
   lead. Added to `SENT` and `FOLLOWUP_SENT`.
3. `strip_quoted` returned the original body when stripping left nothing — so a reply consisting
   only of our quoted pitch was handed to the classifier, which read our own enthusiasm and said
   `interested`. It now returns empty when the quote starts at the first line, and such a reply is
   labelled `other` for a human.

**ADR-002 is confirmed empirically**, not just by design: a pre-generated `Message-ID` was found in
`[Gmail]/Sent Mail` by `BODY.PEEK` search against the real Google Workspace mailbox. That is what
`recover_stuck_sends` relies on, and it needs no `BCC_SELF`.

**The issue register was wrong and is now recomputed from `ISSUES.md`.** The "43 original / 51
total" figures undercounted: there are **51 lettered IDs plus 8 NEW = 59 tracked**. `ISSUES.md`
also still showed Phase 0's fixes as unresolved while this file called them Done; both now agree,
and every test citation in `ISSUES.md` has been checked to resolve to a real file and class.

**Next up.**
1. ~~Push `feat/P6b-reply-actions`, open the PR, merge.~~ Done — **PR #19 merged** as `089970a`
   on 2026-10-10, so `main` now carries the whole reply engine.
2. **The worker process is still not assembled.** `app/worker/` has `send_loop.py` only; the five
   asyncio tasks of §4 (`JobRunner`, `SendLoop`, `ImapLoop`, `Scheduler`, `Heartbeat`) and the job
   handlers that call `draft_reply`, `ingest_folder` and `recover_stuck_sends` do not exist yet.
   Everything they need is built and tested; this is wiring. It is the first thing Phase 7 or a
   short `feat/P6c-worker` should do, and nothing runs end-to-end until it is done.
3. Then Phase 7 (auth, multi-tenancy): C1, C3, C4, C7, NEW-4, NEW-6.

**Still outstanding for the owner.**
- **One** `.claude/settings.json` fix in `QUESTIONS.md` **Q-0** — the `curl` deny entry, now
  confirmed by test to enforce nothing. The second item is closed: the undotted `env.example`
  already works around it, and the broad `.env.*` deny should stay as it is.
- Dependabot PR **#4** (React 19 / Vite 8) left open deliberately — a major framework bump, your
  call.
- Confirm whether the Cloudflare dashboard shows the dashboard project as a **Worker** or as
  **Pages**. `frontend/wrangler.jsonc` is written for a Worker; Pages wants
  `"pages_build_output_dir": "./dist"` instead of the `assets` block, and that is a one-line swap.
  Also set `VITE_API_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` as build-time
  variables there.

**Vercel is gone** (2026-10-10): the owner disconnected and deleted the project and connected this
repo to Cloudflare, as `ARCHITECTURE_TARGET.md` §3 specifies. The repo never held any Vercel
config — it was wired entirely through that dashboard — so nothing needed deleting here, and the
`Vercel` / `Vercel Preview Comments` checks should stop appearing on new PRs.

**Live environment notes.** Python 3.12 venv at `.venv`; dependencies from `pyproject.toml` +
`uv.lock` (`uv sync --all-groups`). Postgres in Compose on **port 5433** (ADR-005), container
`outreach-pg`. `gh` is authenticated as `Harishchand83077`; the repo is private. `.env` is
populated and verified with `python -m app.scripts.check_env`, `SEND_MODE=disabled`. IMAP and SMTP
both verified against the real mailbox. Integration tests need Postgres running and use a separate
`_test` database.

**Note on local `main`.** Merges have been squashed, so local `main` diverges from `origin/main`.
Create new branches from `origin/main`.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| §00 | Bootstrap, tooling, permissions | **Done** |
| §0 | Repo analysis, issue verification, baseline | **Done** |
| 0 | Safety patch + CI + docs | Safety patch **done** (76 tests green, unpushed); CI next |
| 1 | Data layer, state machine, job queue, outbox | **Done** — 213 tests |
| 2 | Import, persona, campaigns | **Done** — 318 tests |
| 3 | LLM router, research engine | **Done** - 539 tests |
| 4 | Drafting, grounding, review queue | **Done** - 633 tests |
| 5 | Send engine | **Done** - 757 tests. The first allowlist send gate is now runnable. |
| 6 | Reply engine, follow-ups | **Done** - 952 tests. Poller, classifier, actions, reply drafter, ingest loop, ADR-002 recovery. |
| 7 | Auth, multi-tenancy | Not started. **Assemble the worker's five tasks first** - see the handoff note. |
| 8 | UI completion | Not started |
| 9 | Ops: Docker, deploy, logs, alerts, backups | Not started |
| 10 | Rehearsal, go-live gate | Not started |

## Issue register

**59 tracked issues**, not the 51 this file claimed until now: 51 lettered IDs (A1–A8, B1–B18,
C1–C8, D1–D6, E1–E8, F1–F3) plus NEW-1…8. The old figure counted the lettered IDs as 43. Every one
must end `Done`, `Superseded-by-rewrite` (with a test proving the behaviour) or `Won't fix` (with a
reason). Counts below are computed from `ISSUES.md`, which holds the full text, evidence and the
test that proves each resolution.

| Status | Count | IDs |
|---|---|---|
| **Superseded-by-rewrite** (each with a test) | 29 | A1–A8, B5, B7–B17, C5, D1, D2, D3, D4, E2–E5 |
| **Done** | 14 | B1, B2, B3, B4, B6, C2, C8, E6, F1, F2, F3, NEW-1, NEW-2, NEW-5 |
| Partly | 2 | D5 (headers, plain text and the footer done; the SPF/DKIM/DMARC runbook is Phase 9) · NEW-8 (`.xls` rejected and `.csv` accepted; the docstring went with the F1–F3 rewrite) |
| Verified, not yet fixed | 14 | B18, C1, C3, C4, C6, C7, D6, E1, E7, E8, NEW-3, NEW-4, NEW-6, NEW-7 |

**43 of 59 resolved.** What is left is Phase 7 (C1, C3, C4, C7, NEW-4, NEW-6), Phase 8 (E8, NEW-3),
Phase 9 (C6, D6, E7, the D5 runbook) and the legacy-module leftovers that die with it (B18, E1,
NEW-7).

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
