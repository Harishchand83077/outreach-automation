# Implementation plan — v4

Written 2026-11-17. Read with `CLAUDE.md`, `docs/PROGRESS.md` and `docs/OUTREACH_PLATFORM_V4.md`.

`docs/OUTREACH_PLATFORM_V4.md` is the **product** source of truth — what the job-switch copilot is
for, its screens, its data model and its acceptance table. It was written from commit `e7ec2d7`
(8 November 2026) by reading code without running anything, and six merges have landed since. This
file is the **build** plan: the reconciliation against the repo as it actually is, and the ordered
slices.

**Where the repo and the v4 document disagree, the repo wins.** §1 lists every disagreement found.

The build order is no longer v4's P17–P24 as written. It is the usability slices **S18.0 – S19.7**
below, which came out of touring Instantly, Smartlead and Huntr signed in and the public sites of
lemlist, Clay, Teal, Simplify and Jobright. ADR numbers continue from **063** (ADR-062 is the last
accepted one).

No hard rule is weakened by anything here: no auto-send, no auto-apply, no warm-up pool, no open
or click tracking, no LinkedIn fetching, no paid service, no shared provider key.

---

## 1. Reality check

Measured on this machine, Docker up (`outreach-pg` healthy), Python 3.12 venv at `.venv`. Lint,
format, types and the two JavaScript suites were re-run on this branch and are unchanged from
`origin/main` = `231e503`; the backend row is this branch's own run.

| Check | Command | Result |
| --- | --- | --- |
| Lint | `ruff check .` | **All checks passed** |
| Format | `ruff format --check .` | **385 files already formatted** |
| Types | `mypy app` | **Success: no issues found in 239 source files** |
| Backend | `pytest` | **2,806 passed, 0 failed**, 142 warnings, in 63m 49s |
| Dashboard | `cd frontend && npm test` | **3 files, 35 tests, all passed** |
| Extension | `cd extension && npm test` | **4 files, 38 passed, 1 skipped** |

### Where the v4 audit was wrong or out of date

| The v4 document says | The repo says |
| --- | --- |
| 50 tables | **54** `__tablename__` declarations in `app/db/models.py` |
| 43 migrations | **45** files in `app/db/migrations/versions/` |
| 138 tests | **148 test files.** 138 was a file count, not a test count, and is wrong as either |
| "the job-hunt screens have no navigation model", "no router", "screens picked by a state variable inside App.jsx" | **A router exists**: `frontend/src/router.jsx`, hand-written, no dependency, History API, ADR-057. `App.jsx:167-181` declares 15 real routes, and `/leads/:leadId` deep-links |
| React Router, TanStack Query, Radix UI and Testing Library are "pre-approved" | Moot for the router: ADR-057 already chose a hand-written one for stated reasons, so adding React Router now would be a deviation needing its own ADR. The others are untouched and stay unused until a slice needs one |
| S16.3 and S16.4 are the only open v3 slices — "confirm against PROGRESS.md" | **Both are merged.** ADR-058 is S16.3 (the OAuth 2.1 authorization server); ADR-059 is S16.4 (reviewer-edit learning). There are **no open v3 slices** |
| "I did not check whether the Google client has left Testing mode after ADR-050" | It has not, **deliberately**. ADR-018: a mailbox *read* scope is `restricted` and would need a paid annual CASA assessment, which hard rule 5 forbids; an unpublished client may request any scope for up to 100 test users, at the cost of a 7-day refresh-token expiry and a weekly Reconnect. ADR-050 did not change the client's state — it made an expired grant a stated reason for a mailbox to be unavailable |
| "still not built: a resume upload and parser" | **Confirmed.** `app/services/resume/` is `ops / pdf / store / tailor / validate`, and `pdf.py` *writes* a PDF. There is no upload route, no reader, and **no PDF or DOCX dependency in `pyproject.toml`** |
| `App.jsx` is 4,983 lines | **5,374** |
| P21 includes "extension field fill and resume download" | Contradicted by the shipped product: `extension/public/manifest.json` describes it as a tool that "never fills in or submits a form". **No field fill is planned**; S19.4 adds *Save this job* and nothing else |
| "object storage for files … the S3 bucket already used for backups" | There is no S3 client in `app/`, and ADR-016 already decided the opposite for `attachments`: capped bytes in Postgres, so the truth lives in one place and the nightly dump covers it. Documents follow that precedent (**ADR-069**) |
| `fit_scores` may be superseded by `fit_evaluations`, "only one may remain" | Settled in favour of `fit_scores` (ADR-055): one row per posting, and `matched` already carries the requirement, the fact id and the words that matched. S19.3 reads it. **No second table** |
| S18.1's instruction to "first check whether an out-of-office label exists" | **It exists.** `ReplyLabel.OUT_OF_OFFICE` in `app/domain/reply.py`, with `LabelPolicy(DEFER, draft_reply=False, notify=False)` and `DEFAULT_OOO_DEFER_DAYS = 5` in `app/services/reply/service.py`. No new label and no policy change: the slice gives it a folder and shows the deferred date |
| `PROGRESS.md`: "there is still no way to delete a campaign" | `DELETE /campaigns/{campaign_id}` exists (`app/api/routers/campaigns.py:1334`). Stale line, corrected in this PR |

### What exists and is reused rather than rebuilt

- **Review and approval.** `GET /review/drafts`, `POST /drafts/{id}/approve|reject|attach`.
  Nothing in S18/S19 adds a second way to approve a draft.
- **Counts from the record.** `GET /campaigns/{id}/stats` counts *messages* for sends and "ever
  reached" states from `events` — the semantics issue NEW-3 was about, and exactly what S18.2's
  table needs.
- **Next-step logic.** `frontend/src/setupSteps.js` exports `buildSteps`, `progress` and
  **`nextStep`**, with tests. S18.3 extends it; it does not copy a rule.
- **Contact lookup.** `app/services/contacts/lookup.py`: three refusals before any network call, a
  25-per-day workspace budget, and `tests/unit/test_contacts_button_press_only.py` asserting over
  the whole tree that nothing but a person can start one.
- **Reviewer-edit data.** `drafts.derived_from_id` is set only when a reviewer approved with
  changes (ADR-026); `lead_outcomes.outcome` is nullable and NULL means "still watching". Together
  they are everything S19.7 needs.
- **The UI kit.** `ui.jsx`: `Skeleton`, `SkeletonList`, `Empty`, `Drawer`, `CommandPalette`,
  `PageHeader`, `ActivityChart`, `Icons`, `useTheme`.
- **The ADR-048 guards**, which are the real contract between a screen and the API:
  `tests/unit/test_dashboard_calls_real_routes.py` (a screen asks for a route that exists),
  `test_dashboard_reads_real_fields.py` (and reads fields it returns),
  `test_dashboard_state_is_declared.py`. **All three glob `frontend/src/*.jsx` — flat, not
  recursive**, which is why §4 keeps new screens flat.

---

## 2. Slices

One pull request each. Branch `feat/p<phase>-<slice>` from an up-to-date `origin/main` (local
`main` diverges, because merges are squashed). Failing test first, shown failing for the right
reason; then the minimum that passes; then `ruff check`, `ruff format`, `mypy app` and all three
suites green before the pull request.

| Slice | What it is | Migration | needs-human |
| --- | --- | --- | --- |
| S18.0 | Split `App.jsx`; add the route-registration guard | — | no |
| S18.5 | Deterministic membership resolution | — | **yes** (auth) |
| S18.1 | Inbox (`/inbox`) | `inbox_flags` | no |
| S18.2 | Campaigns table (`/campaigns`) | — | no |
| S18.3 | Next-step panel | — | no |
| S18.4 | Contact waterfall | `provider_credentials.priority` | no |
| S19.1 | Resume upload and parse | `documents` | no |
| S19.2a | Opportunities: tables and status machine | 4 tables | no |
| S19.2b | Opportunities: API, Pipeline, Today | — | no |
| S19.2c | Backfill `applications` → opportunities | alters rows | **yes** (existing data) |
| S19.3 | Resume workspace | `resume_versions` columns | no |
| S19.4 | Job freshness and the clipper | `job_posts` columns | no |
| S19.5 | Proof pack and clarity check | — | no |
| S19.6 | Referral ranking | `connections` columns | no |
| S19.7 | Edited versus unedited | — | no |

### S18.0 — Split `App.jsx`, and a route-registration guard

Three new screens going into a 5,374-line file would make every later diff unreviewable.

- **Files.** `App.jsx` → extracted, flat, into `frontend/src/`: `Review.jsx` (ReviewQueue,
  Evidence), `Leads.jsx` (Leads, Brief, LeadDetail), `Import.jsx`, `Campaign.jsx` (CampaignScreen,
  ExperimentPanel, NewCampaign), `Admin.jsx` (Admin, Team, OAuthClients, McpTokens),
  `Settings.jsx` (Settings, SendAllowlist, SendModeControl, Suppressions, ModeOption),
  `Platform.jsx`. Shared `Field`, `TextArea`, `Centered` and the tone helpers move to `ui.jsx`.
  `App.jsx` keeps `App`, `ConsentPage`, `ROUTES`, `Dashboard` and `nav`.
- **Flat, deliberately.** Nesting into `screens/` would blind all three ADR-048 guards, whose
  globs are not recursive. This slice also widens those globs to `**/*.jsx`, so a future nesting
  cannot silently blind them.
- **Tests added.** `tests/unit/test_dashboard_routes_are_registered.py`: every `ROUTES` id has a
  renderer, every `nav` destination matches a `ROUTES` pattern, and no `ROUTES` entry is
  unreachable. This is how every new screen registers from here on.
- **Acceptance moved.** None. The diff is moves and imports, and the pull request says so.

### S18.5 — Membership resolution

- **The defect.** `app/api/deps.py` resolves a membership with
  `select(Member).where(Member.user_id == claims.subject)` and **no `order_by`**. A person with
  two memberships gets an arbitrary workspace, so two refreshes can show different data.
- **The fix.** Deterministic order (oldest membership, then id); `/me` names the active workspace
  and lists the others. Switching workspaces is *not* in this slice.
- **Tests.** Two memberships resolve to the same `org_id` on repeated calls; `/me` names it.
- **needs-human: yes** — it changes how sign-in picks a workspace.

### S18.1 — Inbox (`/inbox`)

One list of inbound replies across campaigns.

- **Backend.** New `app/api/routers/inbox.py`: `GET /inbox/threads` (folder filter, cursor
  pagination), `GET /inbox/threads/{thread_key}`, `POST /inbox/threads/{thread_key}/snooze`,
  `POST /inbox/threads/{thread_key}/archive`.
- **Pure module.** `app/domain/inbox.py` decides a thread's folder from the classification label,
  the lead state, the pending draft's status and the flags. It is pure and unit-tested, because
  the dashboard suite renders nothing and logic written inside JSX is logic nothing tests.
- **Migration.** `inbox_flags`: `org_id`, `thread_key`, `snoozed_until`, `archived_at`,
  `updated_by`, unique on org and thread. **Snooze and archive are flags, never lead states**, and
  a structural test asserts the router imports nothing that can write `leads.state`.
- **Folders.** Needs reply, Drafts waiting, Interested, Meeting booked, Out of office, Wrong
  person, Not interested, Snoozed, Archived.
- **Out of office.** The label, its `DEFER` policy and the five-day push-out already exist. The
  screen shows the proposed next follow-up date. Nothing sends by itself.
- **Frontend.** `frontend/src/Inbox.jsx`, route `/inbox`, nav under Workspace. Folder rail, thread
  list, then the thread with its lead and its pending draft; Approve and Reject call the existing
  review routes.
- **Tests.** Domain unit tests per folder; integration tests for the filters and for tenant
  isolation; `inbox_flags` added to the erasure path and its test; the router added to
  `test_routers_take_injected_settings.py`.

### S18.2 — Campaigns table (`/campaigns`)

- **Backend.** `GET /campaigns/overview`: one row per campaign with leads, sent, replied,
  positive, out of office, bounced, meetings and status, counted from the record the way
  `campaign_stats` does, in grouped queries rather than N+1 calls to `/stats`.
- **Route order.** `/campaigns/{campaign_id}` is declared at `campaigns.py:365`, so a literal
  `/campaigns/overview` must be declared above it or `overview` is read as an id. A test asserts
  the route does not 404.
- **No opened or clicked columns, ever.** A test asserts that neither the route nor the screen
  names them: there are no tracking pixels and no link rewriting in this product.
- **Frontend.** `frontend/src/Campaigns.jsx`, route `/campaigns` declared above `/campaigns/new`;
  tabs for running, paused and draft.

### S18.3 — Next-step panel

- **Pure.** `setupSteps.js` gains `nextStepFor(screen, sources)` built on the existing `nextStep`,
  tested in `setupSteps.test.js`. No model, and no second copy of any rule.
- **Frontend.** A `NextStep` card in `ui.jsx`, placed on Overview, Sending, Job hunt and Inbox,
  naming one thing to do and linking to it.

### S18.4 — Contact waterfall

- **Backend.** `lookup.waterfall()` walks the workspace's chosen provider order and stops at the
  first **verified** address; each provider call counts against `DAILY_LOOKUP_BUDGET`.
  `GET /contact-lookup/preview` returns the ordered providers, which of them hold a live key, and
  the credit cost per provider as `docs/CONTACT_PROVIDERS.md` declares it — declared, never
  invented.
- **Migration.** `provider_credentials.priority`, nullable; unset falls back to the catalogue
  order, so no existing row is rewritten.
- **Guards kept.** `test_contacts_button_press_only.py` passes unchanged: no job kind, no
  scheduler pass, no shared key.
- **Apollo.** ADR-067 decides **not** to write a `mixed_people/organization_top_people` adapter
  until a real response has been seen. An adapter against a guessed shape is how the current
  Apollo assumption got into the repo.

### S19.1 — Resume upload and parse

- **Migration.** `documents`: `org_id`, kind, filename, `content_type`, `size_bytes`, `sha256`,
  bytes, extracted text, `uploaded_at`; unique on org and hash. **Capped bytes in Postgres**
  (2 MB), following ADR-016; ADR-069 records the departure from v4's S3 plan.
- **Files.** `app/services/candidate/resume_parse.py`; a `parse_resume` job kind;
  `routers/candidate.py` gains `POST /documents`, `GET /documents`, `GET /documents/{id}`,
  `DELETE /documents/{id}` and `POST /documents/{id}/parse` (202).
- **Proposals only.** Facts land in `profile_facts` unconfirmed. Nothing unconfirmed reaches a
  prompt; the resume validator already enforces the consuming half.
- **Dependencies.** `pypdf` and `python-docx` — free, MIT, no service and no cost, approved for
  this slice with exact pinned versions named in the pull request.
- **Tests.** A traversing filename and an oversize file are refused; parsing a fixture resume
  proposes and confirms nothing; `documents` is in the erasure path and the isolation test.
- **Evaluation (the gate).** 5 to 10 resumes with facts labelled by hand: **zero invented facts.**
  The committed fixtures are synthetic; the owner's own resume is run locally and only counts are
  reported, because `local/` is never read into this repo.

### S19.2 — Opportunities, in three pull requests

- **S19.2a — tables and the status machine.** `opportunities`, `opportunity_events`, `tasks`,
  `notes`; `app/domain/opportunity.py` with **one** transition function, row-locked, writing the
  event in the same transaction, mirroring `domain/state.transition()`; a check constraint on
  status; and a structural test that nothing else writes `opportunities.status`. Eight states, as
  the v4 document's status machine defines them.
- **S19.2b — API and screens.** `routers/opportunities.py`; `Opportunities.jsx` with
  `/opportunities`, `/opportunities/:id` and `/today`; a Pipeline board and a Today page.
- **S19.2c — the backfill.** `applications.opportunity_id` and `channel`, and one opportunity per
  existing application. **needs-human**, because it alters existing rows. `applied_at` stays
  something only the user sets. If the migration cannot be run safely from here, the pull request
  carries the exact commands and stops.

### S19.3 — Resume workspace

Three panes: the sections on the left, the rendered document in the middle, and on the right the
match against one posting split into qualifications, responsibilities and keywords, **each line
naming the confirmed fact behind it** — read from the existing `fit_scores.matched`, not
recomputed. Suggested edits are accept-or-reject per line and go through the existing
`resume/validate.py` and `ops.py` path. A resume is never PATCHed; a new version is created.
Migration: `resume_versions.opportunity_id` and `base_document_id`, both nullable.

### S19.4 — Job freshness and the clipper

`job_posts.first_seen_at` and `last_verified_at`, **nullable with no backfill**, so no existing row
is rewritten and the screen honestly says "not recorded" for older rows. The existing daily ATS
poll writes them, and the screen shows them as **observations** — no job is ever labelled fake or
ghost. The extension gains *Save this job* (`POST /ext/v1/jobs/capture`) with **no manifest
permission change**; the tests that it never submits, never writes a file input and never contacts
a LinkedIn host stay exactly as they are and must still pass.

### S19.5 — Proof pack and clarity check

`app/domain/proofpack.py`, pure: for one posting, pick the confirmed facts and evidence links that
match its top requirements, **by rules, with no generated prose**. A spelling and clarity check
that **suggests and never rewrites**, rules-first, so there is nothing to hallucinate. No
cover-letter generator.

### S19.6 — Referral ranking

`connections.strength`, `tags` and `relationship_note`, all nullable: strength is the user's own
judgement and is never inferred. A pure ranking module puts moderately weak ties first and shows
the reason. No LinkedIn request anywhere, and the test proving that still passes.

### S19.7 — Edited versus unedited

A pure module over `drafts.derived_from_id`, `lead_outcomes` and `messages`: reply outcomes for
drafts sent as written against drafts a person edited, **with sample sizes, pending counted
separately, and a "too few" floor**. Silence is "no response yet", never a rejection.
`GET /insights/edited-vs-unedited`, shown on Operations.

---

## 3. ADRs, numbered from 063

| ADR | Slice | Decision |
| --- | --- | --- |
| **063** | **S18.0** | **Accepted.** One file per screen, flat; shared primitives in `ui.jsx`; the ADR-048 globs become recursive; two new guards -- a module must import what it uses, and a screen must be registered in all four places; a guard names behaviour, not a file |
| 064 | S18.1 | The inbox is a view, not a state: snooze and archive are flags on a thread, and the out-of-office label that already exists keeps its DEFER policy |
| 065 | S18.2 | One aggregate query for the campaigns table, counted from the record — and no open or click column, ever |
| 066 | S18.3 | The next step is decided once, in `setupSteps.js`, and drawn in several places |
| 067 | S18.4 | A lookup walks the user's own provider order and stops at the first verified address; Apollo's people endpoint gets no adapter until a response has been seen |
| 068 | S18.5 | One person, several memberships: the active workspace is deterministic and is named on `/me` |
| 069 | S19.1 | An uploaded document is capped bytes in Postgres, and parsing proposes facts nobody has confirmed |
| 070 | S19.2 | An opportunity wraps an application; one function owns its status, and the backfill is its own reviewed migration |
| 071 | S19.4 | Freshness is an observation; the extension saves a link and still cannot fill or submit |
| 072 | S19.5 | A proof pack is a selection of confirmed facts, and the clarity check suggests |
| 073 | S19.6 | Tie strength is the user's judgement, and the ranking shows its reason |
| 074 | S19.7 | Edited against unedited, reported with counts and a floor |

`docs/DECISIONS.md` and `docs/PROGRESS.md` — handoff header rewritten — go in the same pull request
as the slice they describe.

---

## 4. Frontend plan

- **Router.** Keep `router.jsx` (ADR-057). Nothing in S18 or S19 needs nested outlets, loaders or
  lazy splitting, and replacing it would be a deviation needing its own ADR.
- **Folder structure.** **Flat `frontend/src/*.jsx`**, following `JobHunt.jsx`, `Sending.jsx`,
  `Signals.jsx`, `Operations.jsx`, `Overview.jsx` and `Setup.jsx`. This is not a style
  preference: all three ADR-048 guards glob that directory non-recursively, so a `screens/` folder
  would leave them reading nothing while still passing. S18.0 widens the globs to `**/*.jsx` so
  the guards tell the truth either way.
- **The split carries no behaviour change.** Component bodies move verbatim; only imports change.
  It is guarded by the three dashboard guards, `api.test.js`, `build.test.js`,
  `setupSteps.test.js` and the new route-registration test.
- **How a new screen registers.** A `ROUTES` entry, a `nav` entry, a branch in the
  `ErrorBoundary`-wrapped render, and a flat `frontend/src/<Screen>.jsx`. The new guard fails if
  any of the four is missing.
- **Typing.** New files stay `.jsx` with JSDoc. Shared API shapes come from a dumped
  `openapi.json` rather than a types-generator dependency; the real contract remains the two
  source-scanning guards, which already catch a wrong path and a wrong field name — the two
  defects generated types were wanted for. Revisit after S19.
- **Order screens are built.** Inbox → Campaigns → next-step card → Opportunities and Today →
  Resume workspace → Insights.
- **UI definition of done, for every slice that adds or changes a screen.** A route; the component
  kit; distinct loading, empty and error states, with "not loaded yet" never reading as "nothing
  here"; keyboard and command-palette access; checked in both themes; a component test and a route
  smoke test; and the pull request says what was seen when the screen was opened in a browser, or
  that it could not be.

---

## 5. Spikes

| Spike | Measures | Pass threshold |
| --- | --- | --- |
| PDF renderer memory on the production box | Peak RSS of `resume/pdf.render_pdf` on a three-page resume, and ten renders back to back, on the 2 GB VM | No OOM; peak RSS under 150 MB; the worker's other four tasks keep their heartbeat. A failure means rendering moves to a separate memory-capped job before S19.3 |
| Resume parsing on real PDFs | Facts found, missed and **invented** on 5 to 10 real resumes, including a two-column layout | **Zero invented facts.** Found and missed are reported; the owner sets the bar after seeing the first report |
| ATS feed coverage | Of the owner's top 30 target companies, how many publish a Greenhouse, Lever or Ashby feed | Report only. The number goes on the Jobs screen as an explicit coverage note, so the list is never read as complete |

Each spike is a throwaway script in the scratchpad whose numbers go into the slice's pull request.
Nothing from a spike is committed.

---

## 6. Risks and reordering

**Reordered against the brief, with reasons.**

1. **S18.0 inserted first.** Three new screens landing in a 5,374-line file would make every S18
   diff unreviewable, which defeats the one-pull-request-per-sitting rule.
2. **S18.5 moved from last to second.** It is a correctness bug about *which workspace you are
   looking at*; every screen built above it inherits the ambiguity. It needs a human review in any
   case, so that review is better spent before four new screens read through it.
3. **S19.2 split into three pull requests**, so the two reviewable parts are not blocked behind a
   `needs-human` data migration.

**Risks.**

- The backend suite is slow on this machine — **64 minutes**, dominated by the e2e mail tests —
  so it is run once per slice rather than per commit, and the unit and integration subsets carry
  the inner loop. No slice started on an unknown baseline: §1 records the real number, 2,806.
- Apollo's response shape is still unseen. ADR-067 keeps it that way rather than guessing twice.
- S19.2c alters existing rows. It is `needs-human`, and if the migration cannot be run safely from
  here the pull request carries the exact commands and stops.
- The v4 document stays the product source of truth and is now partly stale. Rather than rewriting
  it, §1 of this file is the standing reconciliation, and each slice's ADR records where it
  departed.
- Anything found outside a slice's scope is logged in `docs/ISSUES.md` rather than fixed silently.

---

## 7. Decisions taken, and what is still the owner's

**Taken (2026-11-17).**

| Question | Answer |
| --- | --- |
| Resume reader dependency for S19.1 | **`pypdf` and `python-docx`**, both free and MIT, pinned versions named in the S19.1 pull request |
| Where uploaded documents live | **Capped bytes in Postgres**, 2 MB per file, following ADR-016 rather than v4's S3 plan (ADR-069) |
| Identity that commits and merges | **`Harishchand83077`**, the account `gh` is authenticated as. No `Co-Authored-By` and no "Generated with" line, per `CLAUDE.md` |
| TypeScript for new screens | **No.** New files stay `.jsx` with JSDoc; revisit after S19 |
| Split `App.jsx` before the new screens | **Yes**, as S18.0: moves and imports only |

**Still the owner's, and not guessed here.**

- The fit-agreement bar, set after seeing the first report.
- The three evaluation sets only the owner can supply: their own resume, about 30 job postings with
  the move they would choose, and about 100 recruiter emails labelled by type.
- Everything already listed under "Still outstanding for the owner" in `docs/PROGRESS.md`.
