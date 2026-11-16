# Progress

Updated 2026-11-17 (restructured; full history in `docs/archive/`). Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Fixed from production, 2026-11-17: a job-hunt campaign could not draft a single email.**
`_persona()` in the worker validated every campaign's persona as an outreach `Persona`, so a
`job_hunt` campaign -- whose persona column holds a `CandidateProfile` -- killed every
`research_lead` job on `sender_name Field required`. The queue said `dead`, the campaign said
`running`, and the review queue said 0, which reads as "nothing has happened yet" rather than as
a crash. See `docs/ISSUES.md`. **After deploying, the dead jobs have to be requeued by hand from
Operations -- nothing drafts retroactively.**

**Fixed from production, 2026-11-17: a hiring-contact lookup returned 500 for a form filled in
correctly.** The dashboard's provider `<select>` rendered `value={lookupWith || usable[0].provider}`
while posting `lookupWith`, so an untouched dropdown displayed a provider and submitted an empty
string; `keys.live` refused the empty name, correctly, and the refusal escaped as a 500. Two more
escapes from the same refusal path were found and closed with it -- an undecryptable key, whose
message names its own fix, and a duplicate address that violated `uq_contact_candidates_email`
*after* the provider had been paid. See `docs/ISSUES.md`.


**Where we are.** The v3 plan is complete. Phases 0–9 are done and deployed; Phases 10–17 (multi-tenant sign-up, Connect Gmail OAuth, MCP connector, browser extension, job-hunt mode, booking webhook, search chain, signals/watchlist, branding, OAuth 2.1 auth server, learning from reviewer edits, full dashboard coverage, Setup page) are all merged and running. **59 of 59 issues resolved.** The API is live at `https://api.arkvane.in`; the dashboard at `https://outreach-automation.sanketyadavdohar123.workers.dev/`.

**Current branch: `feat/p18-app-split`** — S18.0, the first v4 slice: `App.jsx` split into one
file per screen, plus the two guards that make such a split safe. No migration, no API change,
no behaviour change. It stacks on `docs/implementation-plan-v4` (the build plan), so review that
one first. The banner fix merged as #167.

**S18.0, in one line.** `App.jsx` went from 5,374 lines to 731, eleven screen files came out of
it, eleven shared primitives moved to `ui.jsx`, and all 52 declarations were checked
byte-for-byte (comments stripped) to prove nothing but imports changed. **ADR-063.**

**Two new guards, and why they exist.** There is no ESLint in `frontend/`, and neither Vite nor
esbuild fails on an undefined identifier — so a split that forgot an import would build, deploy
and throw on the first render that touched it, and the dashboard suite renders nothing.
`test_dashboard_imports_what_it_uses.py` fails when a module uses something another module
exports without importing it. `test_dashboard_routes_are_registered.py` fails when a screen is
missing any of the four things that register it: a `ROUTES` entry, a `view ===` branch, a `nav`
entry with a real route id, and a nav destination a route pattern matches. Both were proved by
breaking the tree on purpose and watching them fail for the right reason. The three ADR-048
globs are now recursive, so nesting a screen can never blind them, and
`test_connect_sends_mailbox_id.py` no longer names `App.jsx` — it reads every screen, because a
guard that has to be edited whenever code moves is a guard people edit to make it pass.

**v4 planning is done and the repo has been reconciled against it.** `docs/OUTREACH_PLATFORM_V4.md`
stays the *product* source of truth; `docs/IMPLEMENTATION_PLAN_V4.md` is the *build* plan and its §1
lists every point where that document is now out of date (it was written from `e7ec2d7`, before
ADR-057 to ADR-062 landed). Notably: a router already exists, S16.3 and S16.4 are merged so no v3
slice is open, the out-of-office label and its five-day defer already exist, `fit_scores` stands and
`fit_evaluations` is not built, and there is still no resume reader of any kind. The build order is
now the usability slices **S18.0 to S19.7**, and ADR numbers continue from **063**.

**The real baseline, measured 2026-11-17:** `ruff check` clean, `ruff format` clean on 385 files,
`mypy app` clean on 239 source files, 35 dashboard tests pass, 38 extension tests pass with 1
skipped, and **2,806 backend tests pass with none failing** -- an hour's run, which is why the
backend suite goes once per slice and the unit and integration subsets carry the inner loop.

**`SEND_MODE=disabled` verified in the running container.** Nothing has ever been sent to a stranger. All four containers healthy.

**What was recently shipped (see `docs/archive/HISTORY_V3_PLAN.md` for details):**
- ADR-057/S17.1: brand, pages with real URLs, light/dark, router
- ADR-058/S16.3: OAuth 2.1 authorization server so connecting Claude no longer requires pasting a token
- ADR-059/S16.4: reviewer-edit learning — the system can notice patterns and propose standing instructions
- ADR-060: provider connection catalogue (Apollo, Hunter, Snov) with deep links and honest one-click claims
- ADR-061/S17.3: `GET /version`, `PUBLIC_PATHS` guard, five routers fixed to take `SettingsDep`
- ADR-062: Setup page listing what a workspace still has to connect; Apollo endpoint corrected

**Phase 10 — rehearsal and the go-live gate — is what remains.**

## Next up

1. ~~**Phase 9 slice 2 — privacy and retention (D6).**~~ Done. `DELETE /leads/{id}` as a real erasure, the
   retention sweep in the `Scheduler` (Q7's default is 12 months), and a `PRIVACY.md` that
   describes what this system actually does with a lead's data — including that public page text
   goes to providers marked `allow_pii=false` and a lead's name, address and reply never do.
   **D6 is the last issue outside Phase 8.**
2. ~~**S18.0 — split `App.jsx` and add the registration guard.**~~ Done, in review (ADR-063).
   Next: **S18.5**, deterministic membership resolution — `app/api/deps.py` selects a membership
   with no `order_by`, so a person with two of them gets an arbitrary workspace per request.
   `needs-human`, because it changes how sign-in picks a workspace. Then the Inbox (S18.1), the
   campaigns table (S18.2) and the next-step panel (S18.3).
   `docs/IMPLEMENTATION_PLAN_V4.md` §2 is the ordered list.
3. **Phase 8 remainder:** TypeScript and TanStack Query stay deferred on purpose — new screens are
   `.jsx` with JSDoc, because the two source-scanning ADR-048 guards already catch the defects
   generated types were wanted for. Persona *editing*, lead detail with its timeline, bulk actions
   and filters remain. `DELETE /campaigns/{id}` **does** exist (`campaigns.py:1334`); the earlier
   note here that it did not was wrong.
4. **Phase 10:** rehearsal in `SEND_MODE=allowlist`, then the §12 go-live gate.

**Still outstanding for the owner.**
- **Set the Cloudflare build variables** so the dashboard can reach the API that is now live:
  `VITE_API_URL=https://api.arkvane.in`, plus `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.
  Then redeploy the Cloudflare project. Without `VITE_API_URL` the dashboard asks its own origin
  for `/api/v1`, gets `index.html` with a 200, and shows the `<!DOCTYPE` error.
- **Create the first `members` row** (`docs/DEPLOY.md` §4.1). Access is a database row: a valid
  Supabase token with no membership gets no org and therefore no data, by design. This needs the
  `sub` of your Supabase user, which only you can read from that dashboard.
- **Confirm DKIM is actually signing.** The record is published but DNS cannot show whether
  Google is using it — send one message from the arkvane.in mailbox to a Gmail address and check
  **Show original** for `DKIM: PASS`. If it fails, press **Start authentication** in the
  Workspace admin console. This gates the first real send.
- **`SMTP_EMAIL` must be an `@arkvane.in` address.** SPF, DKIM and DMARC protect the domain in
  the From header, and those records are on `arkvane.in`. If the sending mailbox is still the
  `@athenaedu.in` one used in testing, mail goes out unauthenticated against a domain with no
  matching records, which is worse than no setup at all.
- **Q2 — the postal address in the email footer. Answered 2026-10-11: proceed without it for now,
  the owner will supply one later.** So the footer ships without a postal address and
  the gap is carried in `QUESTIONS.md` Q2 until Phase 10's `COMPLIANCE.md` records it: no postal
  address weakens CAN-SPAM posture for any **US** recipient, and the audience is India-only, which
  is why it is tolerable meanwhile. Revisit before the audience widens.
- **API keys will be rotated by the owner later** (after the `.env` exposure recorded above). Not
  blocking anything; worth doing before go-live.
- Dependabot PR **#4** (React 19 / Vite 8) left open deliberately — a major framework bump.
- **One** `.claude/settings.json` fix in `QUESTIONS.md` **Q-0**: the `curl` deny entry, confirmed
  by test to enforce nothing.
- Open questions for go-live: **Q2** (footer address), **Q5** (Indian holidays), **Q7** (retention
  — defaulting to 12 months in slice 2 unless told otherwise).

**The dashboard is at `https://outreach-automation.sanketyadavdohar123.workers.dev/`**, built by Cloudflare on every merge to `main`. Written down because it was not anywhere in this repository, and a screen nobody can open is the defect ADR-048 is about.

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
| 9 | Ops: Docker, deploy, logs, alerts, backups, privacy | **Done** - 1134 tests. Dockerfile, production Compose, Caddy, `deploy.sh`, `backup.sh`, `docs/DEPLOY.md`, JSON logs with request and job ids, the `alerts` table and webhook, `/ready` wired to the worker heartbeat (ADR-009), and per-lead erasure with an hourly retention sweep. **Deployed and serving at `https://api.arkvane.in`.** Closes C6, D5, D6, E7. |
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

**59 of 59 resolved (PR #61 closed the register).** The table above reflects the state before PR #61; D6, E8 and NEW-3 were the last three and are now Done.

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

## Implementation history

For detailed decisions, defect descriptions, and phase notes, see the archive:

- [`docs/archive/HISTORY_PHASES_0_9.md`](archive/HISTORY_PHASES_0_9.md) — Phases 0–9: initial build, prototype deletion, first deployment, Phase 8 completion (ADR-001 through ADR-010, PRs up to #61).
- [`docs/archive/HISTORY_V3_PLAN.md`](archive/HISTORY_V3_PLAN.md) — v3 plan Phases 10–17: multi-tenant sign-up, Connect Gmail, MCP connector, browser extension, job-hunt mode, booking webhook, search chain, signals/watchlist, OAuth 2.1 auth server, branding, Setup page (ADR-017 through ADR-062, slices S10–S17).
- [`docs/archive/HISTORY_APPENDIX.md`](archive/HISTORY_APPENDIX.md) — Implementation notes appended after the v3 plan: ADR-011 (research cache key), ADR-012 (template mode), ADR-013 (persona requirements), import undo, reply engine fixes, architecture review (ADR-014/015), dashboard redesign, follow-up threading, Postmaster monitoring (ADR-029), MCP smoke check.
