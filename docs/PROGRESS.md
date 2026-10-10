# Progress

Updated 2026-10-10 (Phase 6 complete). Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are. Phases 0 through 6 are complete and merged, and the worker process is now
assembled.** 980 tests pass; ruff and mypy strict are clean.

- Phase 6a (`b1d6aa4`): inbound mail parsing, the ten reply labels, the keyword opt-out rules
  including the Hinglish forms, and the `reconcile` precedence.
- Phase 6b (`089970a`, PR #19): the `ImapPoller`, the reply service, the LLM classifier, the reply
  drafter, the ingest loop and the ADR-002 send recovery.
- Cloudflare (`8c5f2e1`, PR #20): `frontend/wrangler.jsonc`, and Q-0 settled by measurement.
- Phase 6c (this session): **the worker process**. `app/worker/` now holds the five asyncio tasks
  of §4 — `JobRunner`, `SendLoop`, `ImapLoop`, `Scheduler`, `Heartbeat` — behind a `WorkerTask`
  base whose `tick()` does one cycle and returns how long to wait. Run it with
  `python -m app.worker`.

**What the worker assembly settles.** Every service built in phases 1 to 6 was reachable only
from a test until now. The pieces that were decisions rather than wiring:

- **A tick never raises.** An exception is recorded against that task's health and the task backs
  off and retries. A task dying silently while the other four carry on is the failure that is
  hardest to notice, and it is exactly what a bare `asyncio.gather` would produce.
- **`send_message` and `classify_reply` are deliberately not job kinds the runner handles.** The
  `SendLoop` is the only sender (hard rule 1), and two runners draining a send queue would make
  the daily cap a race. Classification already happens in the transaction that stores the
  message, so queueing it separately would reopen the window where a reply exists with no label.
  A test asserts both absences.
- **One send per cycle.** `send_one` returns the interval to wait and the loop honours it.
  Batching would set the gap between two emails to zero, which is the most recognisable
  signature of bulk mail.
- **Graceful shutdown asks rather than cancels.** A cancellation between the SMTP call and the
  row recording it leaves a message `sending` and costs a human a trip to the mailbox. Tasks get
  30 seconds to finish their cycle before being cancelled.
- **`PooledUsageStore`** was needed because `DbUsageStore` binds to one session, and the ingest
  loop opens a session per message while the router outlives all of them. Without it the LLM
  daily cap would silently not apply to reply classification.

**One assumption now in the code, stated plainly:** `SendLoop` takes the *first* campaign using a
mailbox. One campaign per mailbox is the shape the product has today. When that stops being true
the loop must interleave campaigns under one shared cap, which is a scheduling decision rather
than a lookup — better to meet that assumption loudly later than to guess at it now.

**Phase 7 slice 1 (this session): the API exists, and it is authenticated from its first
route.** `app/api/` was an empty package until now — a fact the plan had not noticed. Every
C-series issue was written against the *legacy* `api_server.py`, which still runs the prototype
on SQLite. So Phase 7 is not "add auth to the API"; it is "build the API, authenticated from the
start", and this is the first vertical slice of it.

- `app/api/security.py` verifies Supabase tokens locally against the project JWKS, cached for the
  vendor's 10-minute edge window. Asymmetric algorithms only, `iss` and `exp` checked, `aud`
  deliberately not (Supabase issues `authenticated` for everyone, so it distinguishes nothing).
- `app/api/deps.py` resolves the verified `sub` to a `members` row, which yields the org and the
  role. **`org_id` is never read from the request** — not from a path, query, body or header.
- `app/api/app.py` adds a CORS allowlist (never `*`), a request id on every response and log
  line, one uniform error envelope, and a rate limiter keyed on the caller rather than the
  address.
- Routes so far: `GET /health`, `GET /ready`, `GET /api/v1/me`. Migration `36178dafbbcb` adds
  `members`, with `role` constrained to owner/admin/member.

**Owner-only was the owner's decision (2026-10-10):** only `owner` is issued today; `admin` and
`member` exist in the constraint so inviting people later is a row, not a migration.

**Phase 7 slice 2 (this session): the endpoints the dashboard needs.** Cursor pagination
(`app/api/pagination.py`), campaigns, leads, lead detail and the review queue. 1034 tests.

- **Every list row is a summary.** That is issue C7 stated as a rule: the prototype's
  `GET /api/leads` returned each lead's full insights, draft body and reply text and the UI
  polled it every 2.5 seconds. Bodies now live behind `GET /leads/{id}`, one lead at a time.
  Tests assert the *absence* of those fields, which is the only way the rule survives new fields
  being added later.
- **Cursors, not offsets.** An offset shifts under inserts, so a reviewer paging while leads
  arrive silently skips rows. The cursor is unsigned on purpose: it holds a timestamp and an id
  the caller already has, and every query is still scoped by `org_id`.
- **Approval still sends nothing.** It makes a lead eligible; the send loop re-checks
  suppression, the window, the cap and the lead's state. A structural test proves the review
  router imports nothing that can send.

**A defect found by a test, fixed here:** a cross-org approve returned **500**. The repository
raises `LookupError` for "not in this org" and the router caught only `ValueError`. It refused
correctly — the draft was untouched — but answered with a server error. Now `LookupError` is 404
(as everywhere else: confirming an id exists in another org is itself a leak) and `ValueError`
is 409 for a stale queue.

**Phase 7 is complete, and the prototype is gone.** 1026 tests. The legacy flat modules —
`api_server.py`, `decision_engine.py`, `email_utils.py`, `nodes.py`, `graph.py`, `runner.py`,
`database.py`, `reply_monitor.py`, `approval_store.py`, `excel_loader.py`, `llm_client.py`,
`logger.py`, `main.py`, `state.py` and the `config/` package — are deleted. `app/` is the whole
system.

**The test count went down, from 1062 to 1026, and that is the point of the change.** Thirty-six
tests of deleted code went with it. Before deleting anything, the guarantees those tests held
were re-proved against the new modules:

- `tests/unit/test_send_guard.py` — the `SEND_MODE` gate in `app/services/mail/smtp.py`:
  `disabled` is the default and blocks everyone including allowlisted addresses, an unlisted
  recipient is refused before a socket opens, a substring is not a match, and a structural test
  asserts nothing outside that one function *decides* on `SEND_MODE`.
- `tests/unit/test_settings.py` — placeholder rejection, the `DRY_RUN_EMAILS` refusal, and that
  the committed template is never in the `env_file` chain.

Writing those first was deliberate: deleting a test and its subject together looks tidy and
quietly removes the proof.

**Nine issues close with this deletion**, because they described that file and it now does not
exist: **C1** (zero authentication), **C3** (wildcard CORS), **C4** (IP-keyed rate limit), **C7**
(whole-table responses), **B18**, **E1** (workers inside the API process), **NEW-4** (CSV formula
injection — the endpoint is deleted, not re-guarded), **NEW-6** (`os.chdir` at import), **NEW-7**
(string-interpolated SQL).

**Phase 8 slice 1 (this session): the dashboard talks to the new API, with Supabase login.**

Every endpoint the old `App.jsx` called belonged to the prototype's API and no longer exists, so
this is a rewrite, not a port.

- `frontend/src/supabase.js` — auth only (ADR-001). No table is ever read through this client;
  all data comes from our API, which verifies the token and resolves it to a `members` row.
- `frontend/src/Auth.jsx` — **magic link only, no password field anywhere, no sign-up form.**
  Public sign-ups are disabled, so an invite is a database row; a password would add a credential
  to phish and rotate for an application with one user. The "check your email" message is
  identical whether or not the address is a member, because telling a stranger which addresses
  exist is a free target list.
- `frontend/src/api.js` — attaches the bearer token in **one** place, and treats a 401 as its own
  case so the UI returns somebody to sign-in rather than showing a red box they cannot act on.
- `frontend/src/App.jsx` — review queue first (it is the only screen where human attention is the
  bottleneck), with the research hooks beside each draft. Then leads (cursor-paginated), import
  (preview then commit), and the suppression list. Plain text throughout; no
  `dangerouslySetInnerHTML` anywhere.

**The frontend now has tests** — `frontend/src/api.test.js`, 12 of them, run by `npm test` and
wired into CI. They cover the three things that have already gone wrong: an HTML answer with a
200 status, the token being attached everywhere, and a 401 meaning "sign in again" rather than
"something went wrong".

**A dependency note:** `vitest@3.2.4` pulls a `tinypool` with two critical advisories. PR #15
cleared every frontend advisory and reintroducing them would undo that, so the dev dependency is
`vitest@^5` instead, which audits clean.

**Next up.**
1. **Phase 9 (ops):** Docker, Compose, Caddy, `docs/DEPLOY.md`, structured logs, `/ready` wired
   to the worker heartbeat, backups. **This is what finally makes the hosted dashboard show real
   data** — it is still pointing at an API that exists only on this machine. Closes C6, D6, E7.
2. Remaining Phase 8 polish: TypeScript and TanStack Query (the target in `CLAUDE.md`; this slice
   stayed in plain JS to keep the rewrite reviewable), campaign creation and persona editing,
   lead detail with its timeline. E8 and NEW-3 close with those.
3. **Phase 10:** rehearsal in `SEND_MODE=allowlist`, then the go-live gate.

**Still outstanding for the owner.**
- **One** `.claude/settings.json` fix in `QUESTIONS.md` **Q-0** — the `curl` deny entry, now
  confirmed by test to enforce nothing. The second item is closed: the undotted `env.example`
  already works around it, and the broad `.env.*` deny should stay as it is.
- Dependabot PR **#4** (React 19 / Vite 8) left open deliberately — a major framework bump, your
  call.
- **Cloudflare is sorted** (2026-10-10). The instant-failing `Workers Builds` check on PR branches
  had nothing to do with this repo. Workers Builds keeps **two separate configurations**, and the
  dashboard's Builds panel shows them as tabs: **Production** and **Previews Base**. Production was
  corrected by the owner to build command `npm run build`, root directory `frontend`, deploy
  `npx wrangler deploy`. Previews Base was still at its defaults — no build command, root `/`, and
  preview command **`npx wrangler preview`**, a command removed back in Wrangler 2 and absent from
  the 4.149 the builder installs. It died with an unknown-command error in under a second, which is
  exactly the zero-duration failure we kept seeing. Preview-branch builds are now **off**, so PRs no
  longer carry a check that could never pass; `main` deploys are unaffected. Turn them back on only
  with the Previews tab configured like Production and the preview command set to
  `npx wrangler versions upload`.
- **The live site is wrong until the next `main` build, and that is expected.** The last production
  build ran while root directory was still `/`, so Wrangler found no config, ran its onboarding
  non-interactively, and generated one with `"assets": {"directory": "frontend"}` — publishing the
  frontend **source** tree (`index.html`, `src/`, `package.json`) at
  `outreach-automation.sanketyadavdohar123.workers.dev` instead of the built bundle. Nothing secret
  is in those files, and merging #20 with the corrected settings replaces them with `dist/`. Worth
  confirming the site actually renders after that build.
- While you are in there: set `VITE_API_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` as
  build-time variables. They are public by design and must not go in this repo.

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
| 6c | Worker process | **Done** - 980 tests. The five asyncio tasks of §4, `python -m app.worker`. |
| 7 | Auth, multi-tenancy | **Done** - 1026 tests. The legacy API is deleted. Auth, tenancy, CORS, rate limits, pagination, campaigns, leads, review, imports, suppressions, mailbox, admin. The legacy `api_server.py` still runs and is still unauthenticated. |
| 8 | UI completion | **In progress** - dashboard rewired to `/api/v1` with Supabase magic-link login; 12 frontend tests. TypeScript, TanStack Query, campaign editing and lead detail remain. |
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
| **Superseded-by-rewrite** (each with a test) | 38 | A1-A8, B5, B7-B18, C1, C3, C4, C5, C7, D1-D4, E1-E5, NEW-4, NEW-6, NEW-7 |
| **Done** | 14 | B1, B2, B3, B4, B6, C2, C8, E6, F1, F2, F3, NEW-1, NEW-2, NEW-5 |
| Partly | 2 | D5 (headers, plain text and the footer done; the SPF/DKIM/DMARC runbook is Phase 9) - NEW-8 (`.xls` rejected and `.csv` accepted; the docstring went with the F1-F3 rewrite) |
| Verified, not yet fixed | 5 | C6, D6, E7, E8, NEW-3 |

**52 of 59 resolved.** Everything left is Phase 8 (E8, NEW-3) or Phase 9 (C6, D6, E7, the D5
runbook). Counts are computed from `ISSUES.md`, which holds the evidence and the test proving
each resolution.

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
