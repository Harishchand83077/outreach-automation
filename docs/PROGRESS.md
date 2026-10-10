# Progress

Updated 2026-10-11 (Phase 9 slice 1). Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are. Phases 0 through 8 slice 1 are merged, and Phase 9 slice 1 — ops — is on
`feat/P9-ops`.** 1094 tests pass; ruff, ruff format and mypy strict are clean. **56 of 59 issues
are resolved.**

**Phase 9 slice 1 (this session): the system can be deployed, and it can be watched.**

Everything before this ran on one laptop. The hosted dashboard was pointing at an API that
existed nowhere else, which is why it showed nothing.

- **`Dockerfile`** — one image, two commands. The api and the worker are the same code, so they
  cannot drift apart: a deploy updates both or neither, and a schema the api expects is a schema
  the worker has. Non-root (uid 10001), multi-stage, dependencies from `uv.lock` with `--locked`
  so a build in six months installs the same packages. Built and smoke-tested locally **and** in
  CI, because a Dockerfile that is only exercised on the VM is a Dockerfile first tested during a
  deploy.
- **`deploy/compose.prod.yml`** — Postgres, api, worker, Caddy. Postgres publishes **no port at
  all**; it is reachable only on the Compose network, and a dump is taken by running `pg_dump`
  inside that network. Memory limits on all four, because on a small VM the alternative to a
  limit is the kernel choosing what to kill. Log rotation on all four, because a full disk stops
  Postgres rather than just the logging.
- **`deploy/Caddyfile`** — TLS obtained and renewed by Caddy itself, no certbot to forget. It
  strips any `X-Forwarded-For` a caller sent, and the api trusts the header only from the Compose
  network: without that pair, the address-keyed rate limit is an honour system.
- **`deploy/deploy.sh`** — refuses a dirty tree, migrates *before* starting the new containers,
  waits for `/health`, and puts the previous image back if the api never answers. **It cannot
  touch `SEND_MODE` and cannot run `down -v`**, and CI asserts both of those by grep.
- **`deploy/backup.sh`** — nightly dump, 14 days, verified readable, written to a temporary name
  and renamed only on success. ADR-001 put Postgres on the VM, which removed five problems and
  introduced one: the database dies with the box. The weekly off-box copy is the mitigation, and
  it is manual.
- **`docs/DEPLOY.md`** — the runbook, Oracle Always Free assumed. Includes the two things that
  waste an afternoon: Oracle has **two** firewalls and the Ubuntu image ships with iptables
  closed, and `POSTGRES_PASSWORD` takes effect only on the first start of the volume.

**Structured logging (E7).** `app/core/logging.py`: one JSON object per line, human-readable in
development. The decision worth recording is that **context travels in `contextvars`, not in
arguments** — the api binds `request_id` once per request, the runner binds `job_id`, `org_id`
and `lead_id` once per job, and every line written underneath carries them without the emitting
code knowing they exist. A request id that has to be passed down in order to be logged is a
request id that will be missing from the one line that mattered.

Secrets are scrubbed from every rendered line as a *second* defence. The first is not logging
them; this one exists because secrets arrive in logs by accident, inside the text of an exception
raised by a driver that was handed a URL with a password in it. The tests pass a secret through
every path it would really travel: the message, a format argument, an `extra` field, and an
exception.

**`/ready` now fails when the worker is dead (ADR-009).** It checked only the database until now,
so **an api serving perfectly while the worker was dead reported itself ready** — and the symptom
of a dead worker is that nothing is researched, drafted or sent, which is invisible from outside
for hours. The worker upserts a `worker_heartbeats` row once a minute and `/ready` fails on its
**age**. `health.py`'s argument for keeping health in memory still holds; it is about the liveness
signal, and the api simply cannot see another process's memory. Point an external monitor at
`/ready`, never `/health`.

**Alerts (`alerts` table plus an optional webhook).** The table is the record, the webhook is the
nudge, in that order — an alert that exists only inside a failed HTTP request is an alert nobody
will ever see, and a push failure never propagates, or the system stops working *because* it
tried to say it was in trouble. One hour's cool-down per kind; acknowledging re-arms it. The
worker raises `worker_task_stalled` and `worker_task_failing:<task>` about itself, after **three**
consecutive failures rather than one, because alerting on blips trains whoever receives them to
ignore them.

**Two deliberate deviations from ADR-004**, both recorded in ADR-009: `worker_heartbeats` carries
no `org_id`, and `alerts.org_id` is nullable, meaning "the system". A process is not tenant data,
and a stalled worker stops every org's work at once — attributing it to one org would hide it
from the others.

**A near miss worth recording, because it is a hard-rule-2 incident.** A test built a bare
`Settings()`, which loaded the real `.env`, and a failing assertion then printed every configured
API key into the terminal. The test was about something else entirely and had not asked for them.
Removing the variables from `os.environ` is not enough, because `pydantic-settings` reads the file
itself — so `tests/conftest.py` now has an **autouse, session-scoped `hermetic_settings`** fixture
that detaches `Settings` from `.env` for the whole run. A test that forgets `_env_file=None` is
hermetic anyway. The stale `settings_factory` fixture, which still imported the deleted
`config.config`, was fixed at the same time.

**C6 and D5 close here.** ADR-006 had already settled the account — paid Google Workspace on a
dedicated domain, where SMTP relay is supported and ~2000/day leaves real headroom over our 100 —
and `DEPLOY.md` §6 is the runbook it promised: the SPF, DKIM and DMARC records as GoDaddy's panel
wants them (the apex is `@`, and the DKIM name is `google._domainkey`, not the full hostname), the
two steps everyone forgets (press **Start authentication**, and never add a second SPF record),
and a verify-as-received gate before the first send.

**Six dependencies deleted.** `langchain`, `langchain-core`, `langchain-groq`, `langgraph`, `rich`
and `aiosqlite` went with the prototype in Phase 7 but stayed in `pyproject.toml`. Nothing in
`app/` imports them. They are gone now rather than later because an image that ships six unused
dependency trees ships their vulnerabilities too. The suite is green afterwards.

**Next up.**
1. **Phase 9 slice 2 — privacy and retention (D6).** `DELETE /leads/{id}` as a real erasure, the
   retention sweep in the `Scheduler` (Q7's default is 12 months), and a `PRIVACY.md` that
   describes what this system actually does with a lead's data — including that public page text
   goes to providers marked `allow_pii=false` and a lead's name, address and reply never do.
   **D6 is the last issue outside Phase 8.**
2. **Phase 8 polish:** TypeScript and TanStack Query (the target in `CLAUDE.md`; slice 1 stayed in
   plain JS to keep the rewrite reviewable), campaign creation, persona editing, lead detail with
   its timeline, bulk actions. **E8** and **NEW-3** close with those.
3. **Phase 10:** rehearsal in `SEND_MODE=allowlist`, then the §12 go-live gate.

**Still outstanding for the owner.**
- **Set up the Oracle VM** and send the address. `docs/DEPLOY.md` is written for it. Nothing in
  this repository has ever been deployed, and nothing in Phase 9 was run against a real server —
  hard rule 5 forbids creating cloud resources, so the first run of every command there is yours.
  Expect §1.3 (Oracle's *two* firewalls) and §3.1 (`POSTGRES_PASSWORD` applies only on the first
  start of the volume) to be where it bites.
- **Q2 — the postal address in the email footer.** Still unanswered, and a go-live blocker for
  CAN-SPAM posture. The recommendation is to include it. The default if nothing is said is to omit
  it and record the gap.
- Set `VITE_API_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` as Cloudflare **build**
  variables, then redeploy. Without `VITE_API_URL` the dashboard asks its own origin for
  `/api/v1`, gets `index.html` with a 200, and shows the `<!DOCTYPE` error.
- Dependabot PR **#4** (React 19 / Vite 8) left open deliberately — a major framework bump.
- **One** `.claude/settings.json` fix in `QUESTIONS.md` **Q-0**: the `curl` deny entry, confirmed
  by test to enforce nothing.
- Open questions for go-live: **Q2** (footer address), **Q5** (Indian holidays), **Q7** (retention
  — defaulting to 12 months in slice 2 unless told otherwise).

**Live environment notes.** Python 3.12 venv at `.venv`; dependencies from `pyproject.toml` plus
`uv.lock` (`uv sync --all-groups`). Postgres in Compose on **port 5433** (ADR-005), container
`outreach-pg`. Docker Desktop must be running for the 342 integration tests; without it they skip
rather than fail. `gh` is authenticated as `Harishchand83077`; the repo is private. `.env` is
populated and verified with `python -m app.scripts.check_env`, `SEND_MODE=disabled`. IMAP and SMTP
are both verified against the real mailbox.

**Note on local `main`.** Merges are squashed, so local `main` diverges from `origin/main`. Create
new branches from `origin/main`.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| §00 | Bootstrap, tooling, permissions | **Done** |
| §0 | Repo analysis, issue verification, baseline | **Done** |
| 0 | Safety patch + CI + docs | **Done** |
| 1 | Data layer, state machine, job queue, outbox | **Done** — 213 tests |
| 2 | Import, persona, campaigns | **Done** — 318 tests |
| 3 | LLM router, research engine | **Done** - 539 tests |
| 4 | Drafting, grounding, review queue | **Done** - 633 tests |
| 5 | Send engine | **Done** - 757 tests. The first allowlist send gate is now runnable. |
| 6 | Reply engine, follow-ups | **Done** - 952 tests. Poller, classifier, actions, reply drafter, ingest loop, ADR-002 recovery. |
| 6c | Worker process | **Done** - 980 tests. The five asyncio tasks of §4, `python -m app.worker`. |
| 7 | Auth, multi-tenancy | **Done** - 1026 tests. Auth, tenancy, CORS, rate limits, pagination, campaigns, leads, review, imports, suppressions, mailbox, admin. **The legacy `api_server.py` is deleted**, and with it nine issues that described it. |
| 8 | UI completion | **In progress** - dashboard rewired to `/api/v1` with Supabase magic-link login; 12 frontend tests. TypeScript, TanStack Query, campaign editing, lead detail and bulk actions remain (E8, NEW-3). |
| 9 | Ops: Docker, deploy, logs, alerts, backups | **Slice 1 done** - 1094 tests. Dockerfile, production Compose, Caddy, `deploy.sh`, `backup.sh`, `docs/DEPLOY.md`, JSON logs with request and job ids, the `alerts` table and webhook, and `/ready` wired to the worker heartbeat (ADR-009). Closes C6, D5, E7. **Slice 2 is privacy and retention (D6).** |
| 10 | Rehearsal, go-live gate | Not started |

## Issue register

**59 tracked issues**: 51 lettered IDs (A1–A8, B1–B18, C1–C8, D1–D6, E1–E8, F1–F3) plus NEW-1…8.
Every one must end `Done`, `Superseded-by-rewrite` (with a test proving the behaviour) or
`Won't fix` (with a reason). The counts below are computed from `ISSUES.md`, which holds the full
text, the evidence and the test that proves each resolution.

| Status | Count | IDs |
|---|---|---|
| **Superseded-by-rewrite** (each with a test) | 38 | A1-A8, B5, B7-B18, C1, C3, C4, C5, C7, D1-D4, E1-E5, NEW-4, NEW-6, NEW-7 |
| **Done** | 17 | B1, B2, B3, B4, B6, C2, C6, C8, D5, E6, E7, F1, F2, F3, NEW-1, NEW-2, NEW-5 |
| Partly | 1 | NEW-8 (`.xls` rejected and `.csv` accepted; the docstring went with the F1-F3 rewrite) |
| Verified, not yet fixed | 3 | D6, E8, NEW-3 |

**56 of 59 resolved.** What remains is one phase each: **D6** is Phase 9 slice 2 (per-lead
erasure, the retention sweep, an honest `PRIVACY.md`), and **E8** and **NEW-3** are Phase 8 polish.

**The safety guarantees, and where each one is proved now.** This table was rewritten in Phase 9
because PR #26 deleted the prototype *and* the three test files three of these rows cited —
`test_error_text_never_emailed.py`, `test_reply_never_autosends.py` and `test_upload_security.py`.
The guarantees survived the deletion; the citations did not. A guarantee whose test no longer
exists is a claim, so each row below was traced to a test that is in the suite today, and the one
that had no replacement — C2's — was written in Phase 9 rather than quietly dropped.

| Guarantee | Proved by |
|---|---|
| Nothing reaches SMTP unless one function permits it, and nothing else decides on `SEND_MODE` | `tests/unit/test_send_guard.py` (including the structural test over the whole tree) |
| `SEND_MODE=disabled` is the default and blocks every recipient, allowlisted or not | `test_send_guard.py::test_default_mode_blocks`, `::test_disabled_blocks_every_recipient` |
| An unlisted recipient is refused before a socket opens, and the SMTP server receives nothing | `test_send_guard.py::test_unlisted_recipient_is_blocked_and_nothing_is_received` |
| A blocked or failed send is never recorded as sent | `tests/integration/test_send_loop.py`, `tests/integration/test_send_recovery.py` |
| Placeholder credentials stop startup, and nothing reports itself configured when it is not | `tests/unit/test_settings.py::TestPlaceholderRejection` |
| A stale `DRY_RUN_EMAILS` in the environment stops startup rather than being ignored | `tests/unit/test_settings.py::test_dry_run_emails_in_env_is_rejected` |
| An LLM exception or a rejected draft can never become email content | `tests/unit/test_drafting_generator.py::TestFailureHandling`, `::TestTheClaimCheck` |
| A reply can never trigger a send, and `MEETING_BOOKED` is never inferred from one | `tests/unit/test_reply_rules.py::test_a_hostile_reply_cannot_reach_meeting_booked`, `tests/unit/test_state_machine.py::test_meeting_booked_is_never_reached_from_a_machine_state`, and the structural test that the review router imports nothing that can send |
| An uploaded filename can never reach the filesystem | `tests/integration/test_api_operations.py::test_a_traversing_filename_never_reaches_the_filesystem` (**written in Phase 9** to replace the deleted `test_upload_security.py`) |
| No test reads the developer's `.env`, so a failing assertion cannot print a real key | `tests/conftest.py::hermetic_settings` (autouse) |
| A worker that has died cannot report itself ready | `tests/integration/test_ops.py::TestReady` |

## PR log

| PR | Branch | Issues | Status |
|---|---|---|---|
| — | — | — | none opened yet |
