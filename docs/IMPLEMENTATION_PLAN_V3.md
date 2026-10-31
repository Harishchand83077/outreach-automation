# Implementation plan for `OUTREACH_PLATFORM_V3.md`

Written 2026-10-31 against `main` at **`5ed8b13`**. The v3 doc was written against `b3572eb`, so
three merges have landed since (#114 FastAPI, #117 Postmaster/ADR-029, #118 MCP proposals/ADR-030).
**Where the repo and the doc disagree, the repo wins**; §1.2 lists every disagreement I found.

This plan is the slice list. It does not restate the v3 doc's reasoning, and the doc stays the
source of truth for scope, feature ids and acceptance criteria.

---

## 1. Reality check

### 1.1 What already exists, by phase

| Phase | Already built | Not built |
|---|---|---|
| **P10** F1, F2 | `docs/GO_LIVE.md` (full checklist), `tests/e2e/test_go_live.py` (walks a lead's whole life against a real DB, real SMTP server, fake model), nightly S3 backup + restore drill 2026-10-13, `SEND_MODE=allowlist`, per-org `send_allowlist` rows | No single GO/NO-GO **script**; no check that a booking URL actually books; no rehearsal report |
| **P11** F3, F4, F21 | **Most of F21**: `jobs.dedupe_key`, a partial unique index, a lease (`locked_by`/`locked_until`), `reap_expired()` **wired into the `Scheduler`**, capped backoff, a `dead` status, `/admin/dead-jobs` + requeue. `List-Unsubscribe` header exists. Hard/soft bounce distinction, suppression on hard bounce | F21: dedupe index **missing `org_id`** (§1.2.1), no per-workspace queue cap, no version stamps. F4: no https one-click endpoint, no per-domain SPF/DKIM/DMARC check. F3: no `mailbox_health`, no bounce-rate auto-pause |
| **P12** F5, F6, F19 | `campaigns.kind` + `CandidateProfile` compiling into `Persona` (ADR-025), claim resolution (`pp\d+`), `domain_research` 90-day cache keyed on the registrable domain (ADR-011), Tavily only when evidence is thin | `job_posts`, `profile_facts`, `answer_bank`, `fit_scores`, `company_profiles`, any feed polling |
| **P13** F8, F9, F10, F20 | `api_tokens` with a DB-enforced scope CHECK and hash-only storage, Fernet sealing for credentials, per-route rate limiter | Everything else: gateway, extension, `captures`, `suggestions`, `provider_credentials`, `contact_candidates`, `resume_versions`, `applications` |
| **P14** F7, F15, F17 | Per-mailbox cap, round-robin across campaigns inside a mailbox, warm-up ramp | `campaign_mailboxes`, `leads.mailbox_id`, booking webhook, provider lookup steps |
| **P15** F11, F12, F13, F22 | `get_meeting_brief` (composed from the other read paths), `messages.variant` column, hooks ageing at 30 days | `watchlist`, `signal_events`, `entities`, `edges`, variant **allocation** |
| **P16** F14, F16, F24 | **MCP Phase 3 is built and merged** (§1.2.2): `propose_draft`, `propose_reply`, `mcp:propose` in the DB CHECK, scope-filtered `tools/list`, optimistic concurrency on replacement, 5-per-lead-per-day cap. Outcome dataset + export (ADR-026) | New read tools for new tables, tick-box scopes (F24), MCP Phase 2 OAuth server, learning from edit diffs |

### 1.2 Where the doc is wrong or out of date

1. **`jobs` dedupe is not org-scoped, and this is the most important finding in this review.**
   The doc asks for `unique on (org_id, kind, dedupe_key)`. The index that exists is
   `uq_jobs_kind_dedupe_live` on **`(kind, dedupe_key)`** only, partial on
   `dedupe_key IS NOT NULL AND status IN ('queued','running')`. So **two workspaces cannot hold a
   live job with the same kind and key at the same time** — the second `enqueue` is refused and
   that workspace's work silently does not happen.
   It is latent today because every key in the tree is built from a UUID (`draft:{lead.id}`), which
   cannot collide across orgs. It stops being latent the moment a key is built from a *shared*
   identifier — `feed:greenhouse:{token}`, `company:{domain}`, `poll:{ats_slug}` — which is
   exactly what P12's feed polling and P15's signal polling introduce. **Fix before any new job
   kind lands**, which is also what the doc's own ordering note says, for a different reason.
2. **F14 is done, not a P16 "Could".** MCP Phase 3 shipped today (ADR-030, PR #118): proposals
   enter the review queue as `pending_review` with `created_by='mcp:<user>'` through the same
   validator, the `mcp:propose` scope is in the check constraint, and `tools/list` is filtered by
   scope. The doc's row "New scope `mcp:propose`, added by migration to the CHECK — not before
   phase 3 is approved" is satisfied. What is left of F14 is **Phase 2 only** (our own OAuth
   server), plus new read tools for the new tables.
3. **`jobs.dedupe_key` is described as a new column.** It exists, as `String(200)`, and the index
   comment in `models.py` records that autogenerate once dropped it silently. Only the index
   changes.
4. **Lease reclaim exists and is wired.** The doc's "confirm what the current queue already does
   before adding this" resolves to: claim takes a lease, `heartbeat()` extends it, `reap_expired()`
   requeues or kills, and the `Scheduler` calls it every pass. F21's crash-safety half needs a
   *test*, not a build.
5. **"The outcome list has no interview or offer member" is wrong.** `Stage.INTERVIEW` and
   `Stage.OFFER` exist in `app/domain/outcome.py`, with `FUNNEL_STAGES["job_hunt"] = (SENT,
   REPLIED, SCREEN, INTERVIEW, OFFER)`. They are deliberately on the **funnel-stage** axis rather
   than in `Outcome`, because an outcome is a standing result and a stage is how far it got. No
   change needed; the doc's suggested fix would have added a second taxonomy.
6. **One-click unsubscribe is worse than "unverified": it is actively wrong.**
   `message.py` sets `List-Unsubscribe-Post: List-Unsubscribe=One-Click` **while
   `List-Unsubscribe` is a `mailto:` only**. RFC 8058 one-click requires an `https` URL that
   accepts `POST`. Announcing one-click and offering only a mailto is a claim a receiver can test
   and find false, which is a deliverability liability rather than a missing feature. Fix early in
   P11 (S11.0), separately from building the endpoint.
7. **Erasure is not a list of tables.** `erase_lead` does one `session.delete(lead)` and relies on
   `ON DELETE CASCADE`. So the rule for new tables is "carry an FK to `leads` with
   `ON DELETE CASCADE`, or be covered by the retention sweep", not "add yourself to the erasure
   function". Org-scoped tables with no lead FK (`job_posts`, `answer_bank`, `company_profiles`)
   need the sweep instead.
8. **Counts.** 27 tables (unchanged), **23** migrations (the doc's 22 plus the `mcp:propose` scope
   widening). Head is `f2a4c81b9e07`.
9. **No `app/scripts/go_live.py`.** The doc's P10 criterion names a script; what exists is a doc
   checklist plus an e2e test. S10.1 builds the script.
10. **ADR numbering.** The repo is at **ADR-030**, so the ADRs this plan needs start at **031**.
11. **ATS feed endpoints are unverified.** From one third-party article, as the doc admits. S12.1
    tests all three against the live services before any code depends on their shape.
12. **`leads.mailbox_id` does not exist** (F7's sticky sender needs it) and neither does
    `campaign_mailboxes`. The doc is right that this is new; noting it so P14 is not under-scoped.

---

## 2. Slices

One slice is one pull request. `needs-human` is marked **H** where the slice touches auth, a
migration that alters existing rows, the sending path, or deploy files.

### P10 — go-live gate (F1, F2)

| # | Slice | Files | Migration | Tests | Moves |
|---|---|---|---|---|---|
| S10.1 | **Preflight GO/NO-GO script.** `python -m app.scripts.preflight` checks, per sending domain taken from connected mailboxes: exactly one SPF record, a DKIM key at `google._domainkey`, a DMARC record, whether `rua` points at a mailbox we poll, whether one-click unsubscribe is configured, the send mode, and the allowlist count. Prints OK/FAIL per check and a final GO or NO-GO; exits non-zero on NO-GO. Resolver is injected, so tests never touch the network | `app/scripts/preflight.py` | — | unit with a fake resolver for every pass/fail branch; structural test that it never prints a secret (same AST guard as `mcp_smoke`) | "the go-live check script returns GO" | 
| S10.2 | **A booking link must book.** `Persona` validation refuses a URL that is a known self-calendar pattern (`calendar.google.com/calendar/u/...`, `calendar.google.com/calendar/r`), with a message naming the problem. The live campaign currently has exactly this URL | `app/domain/persona.py`, campaign create/patch | — | unit: the known-bad URLs are refused, a Cal.com/Calendly/Google *Appointment schedule* URL passes, an existing campaign with a bad URL still *reads* (display never raises) | "a booking link books" |
| S10.3 | **Rehearsal report.** `python -m app.scripts.rehearsal_report --since <date>` reads `messages` and `events` and reports: every recipient, whether each was on the allowlist at the time, counts sent/blocked/failed, and any recipient not on the list (which must be zero) | `app/scripts/rehearsal_report.py` | — | integration: a blocked send and a sent one are both reported; an off-allowlist recipient makes the report fail loudly | "an allowlist rehearsal sends only to listed addresses and records every message" |

**Owner-only items in P10** (no code can do these): connect an `@arkvane.in` Workspace mailbox
through OAuth, check `Show original` for DKIM pass, replace the booking URL with one that books.

### P11 — deliverability and queue hardening (F3, F4, F21)

**F21 first.** Nothing in P12 onwards may add a job kind until S11.1 and S11.2 are merged.

| # | Slice | Files | Migration | Tests | Moves |
|---|---|---|---|---|---|
| S11.0 | **Stop claiming one-click we do not offer.** Send `List-Unsubscribe-Post` only when an `https` unsubscribe URL exists. Until S11.4 that means not at all | `app/services/mail/message.py` | — | unit: with no https URL the header is absent and the mailto remains; with one, both are present | F4 (removes a false claim) **H** (sending path) |
| S11.1 | **Org-scoped job dedupe (ADR-032).** Replace `uq_jobs_kind_dedupe_live` with `(org_id, kind, dedupe_key)`, same partial predicate, in both the migration and `models.py` metadata (or autogenerate drops it) | `app/db/models.py`, new migration | index only; **no row changes** | integration written first: two orgs enqueue the same `(kind, dedupe_key)` → today one is refused, after the fix both exist; within one org the duplicate is still refused; a finished job does not block a requeue | F21 "enqueueing the same job twice creates one row" **H** |
| S11.2 | **Per-workspace queue cap + fairness.** `MAX_QUEUED_JOBS_PER_ORG` (default 500); `enqueue` refuses over the cap with a reason and raises an alert; `claim` orders so one workspace's backlog cannot starve another | `app/db/queue.py`, `app/core/settings.py`, `app/worker/scheduler.py` | — | integration: a workspace at its cap is refused and says so; a second workspace still gets claimed work while the first holds a large backlog | F21 "a workspace over its queue cap cannot starve another" |
| S11.3 | **Crash-safety test** (build nothing). Kill a worker mid-job; assert the lease expires, the job is requeued, nothing is sent twice and nothing is lost | `tests/integration/test_queue_crash.py` | — | the test itself | F21 "a worker killed mid-job leaves nothing sent twice or lost" |
| S11.4 | **Real one-click unsubscribe (ADR-033).** `POST /u/{token}` and `GET /u/{token}`, mounted **outside `/api/v1`**, unauthenticated, token = HMAC over `(org_id, lead_id)`; POST suppresses and returns 200 with no body; GET renders a one-line confirmation. `List-Unsubscribe` carries the https URL and the mailto | new `app/api/routers/unsubscribe.py`, `message.py`, `app/core/settings.py` | — | integration: POST suppresses and is idempotent; a tampered or truncated token 404s; the suppression is honoured by the send guard; structural test that the router imports nothing that can send | F4 "one-click unsubscribe" **H** (sending path, public route) |
| S11.5 | **Sender-requirement report.** `GET /admin/sender-requirements` reports SPF/DKIM/DMARC/one-click per sending domain, reusing S10.1's checker so the script and the screen cannot disagree | `app/api/routers/operations.py`, shared checker | — | integration with a fake resolver; a test asserting the route and the script call the same function | F4 "a check reports SPF, DKIM, DMARC and one-click unsubscribe per sending domain" |
| S11.6 | **`mailbox_health` + auto-pause (ADR-034).** Daily rollup per mailbox from `messages` and `classifications`; auto-pause when bounce or complaint rate crosses a configured limit over a minimum volume; raise an alert naming the mailbox; **never un-pause and never raise a cap** | new table, `app/services/deliverability/*`, `app/worker/scheduler.py` | new table (additive) | integration: crossing the limit pauses and alerts once; below the limit or below minimum volume does nothing; a paused mailbox is still *read* by IMAP (an opt-out we refuse to read is one we breach); no code path raises a cap | F3 **H** (sending path) |

### P12 — job-hunt core and shared company cache (F5, F6, F19)

| # | Slice | Migration | Tests | Moves |
|---|---|---|---|---|
| S12.1 | **Verify the three ATS feeds** against the live services, record the real response shapes and any rate limits in `docs/ATS_FEEDS.md`. No application code | — | none (a recorded finding, plus `respx` fixtures captured from the real shapes) | de-risks F5 |
| S12.2 | **`job_posts` + pasted intake.** A URL or pasted text creates a row; `body_text` cleared by the retention sweep; unique on `(org_id, source, external_id)` | new table | tenant isolation; retention clears body text; a pasted posting creates exactly one row | F5 (half) |
| S12.3 | **Feed polling job** (`poll_ats_feed`), dedupe key `feed:{source}:{slug}` — **requires S11.1**, since that key is shared across workspaces | — | `respx` against captured fixtures; two workspaces polling the same board both get rows; a feed outage is non-fatal | F5 |
| S12.4 | **`profile_facts` + `answer_bank`.** Facts carry `pp\d+` ids the existing claim check already resolves; mined facts start as proposals; only approved answers are offered | two new tables | a draft citing an unapproved fact fails the existing claim check; only approved answers are returned | F6 (half) |
| S12.5 | **`fit_scores`.** A model extracts requirements (treated as untrusted, wrapped); deterministic rules compare against `profile_facts`; the row lists each match with its fact id, each gap, and no bare number. Input versions stamped, recomputed only on change | new table | a fit score names a fact id per match; a requirement with no matching fact appears as a gap; recompute is skipped when inputs are unchanged | F6 "a fit score lists each match with its fact id" |
| S12.6 | **`company_profiles` shared cache (ADR-031).** Public facts only, keyed on the registrable domain, **no `org_id`**, per-field `source_url`, `content_hash`, refreshed past 30 days only if the hash changed. Per-workspace daily fair share of fetch/search slots; a cache hit costs no slot | new table | **the opposite structural test**: the table has no org, lead, contact or message column, and no code path writes workspace data into it; a second workspace researching a cached company makes **no search call** | F19 + "the ADR for the ADR-004 exception is merged" |

### P13 — extension and resume tailoring (F8, F9, F10, F20)

| # | Slice | Migration | Tests | Moves |
|---|---|---|---|---|
| S13.1 | **Extension gateway, third door (ADR-035).** `/ext/v1/*`, scope `ext:suggest` added to the `api_tokens` CHECK, own rate limit, an audit event per call | CHECK widened | an `ext:` token gets 401 from `/api/v1`; a Supabase JWT gets 401 from `/ext/v1`; an `oa_` MCP token gets 401 from both; structural test the router imports nothing that can send | F8 "its token cannot read `/api/v1`" **H** (auth) |
| S13.2 | **`captures` + `suggestions`**, short retention (7 days), listed in `PRIVACY.md` | two new tables | retention deletes captures past the window; accept/edit/reject is recorded | F8 |
| S13.3 | **Suggestion endpoint.** Labels + URL in; approved `answer_bank` matches out, gaps drafted from `profile_facts` with fact ids, everything else flagged as needing the user. DB-only, no model call in the request | — | an unknown question is flagged, never guessed; no model is called in the request path | F8 "proposes answers only from approved entries, flags unknown questions" |
| S13.4 | **The extension package.** MV3 + side panel, TypeScript, mappers **inside the package**, `activeTab` on a button press. Playwright against saved copies of Greenhouse, Lever and Ashby forms | — | three fixture tests; a test asserting no submit/upload call exists in the package; capture-only fallback for shadow-DOM pages | F8 "never submits" |
| S13.5 | **F9 provider lookups.** `provider_credentials` (Fernet, same scheme as mail), `contact_candidates`, button-press only, user's own keys and caps | two new tables | no shared key path exists; a lookup runs only on an explicit call; results never enter sending until imported | F9 |
| S13.6 | **F10 referral finder.** The user uploads their own LinkedIn connections export; matches at a target company produce a suggested note and a link the user opens themselves | — | nothing fetches LinkedIn; a structural test that no LinkedIn host appears in any request | F10 |
| S13.7 | **F20 resume tailoring (ADR-036).** Model proposes edit operations; a validator rejects any sentence that does not map to a confirmed fact id; `resume_versions` immutable; `applications` tracker; PDF rendering as its own memory-limited job | two new tables | a tailored resume with a number or skill not in confirmed facts is **rejected**; an application points at an immutable version; the extension cannot set `applied` | F20 |

### P14 — scale sending (F7, F15, F17)

| # | Slice | Tests | Moves |
|---|---|---|---|
| S14.1 | **`campaign_mailboxes` + `leads.mailbox_id`**, weighted rotation, sticky sender | two mailboxes share a campaign; a lead's follow-ups stay on its first mailbox; the per-mailbox cap still binds | F7 **H** |
| S14.2 | **F15 signed booking webhook** → `MEETING_BOOKED` | a valid signature sets it; a bad signature is refused; **a reply still cannot set it** (existing test extended) | F15 **H** |
| S14.3 | **F17 ordered provider lookups** behind the research interface, short-circuiting on first hit, on the user's keys | the second provider is not called after a hit; a cache hit calls none | F17 |

### P15 — signals and graph (F11, F12, F13, F22)

S15.1 `watchlist` + `signal_events` (a signal **proposes** a lead, never creates one).
S15.2 `entities` + `edges` with **mandatory provenance** — an edge with no `source_url` or
`message_id` is refused by a constraint, not by convention.
S15.3 Brief upgrade: five sections, every line citing an edge; a model may phrase, never add.
S15.4 **F13 variant allocation** — random assignment with a fixed exploration share, recorded on
the message; funnel views by variant, weekday, hour and warm-up day, censored shown as pending.
S15.5 **F22** daily poll with `external_id` diff and a page-hash fallback; per-source terms checked
before a company joins the poll.

### P16 — connector and learning loop (F14 remainder, F16, F24)

S16.1 New MCP **read** tools for the new tables (`list_job_posts`, `get_fit_score`,
`get_entity_edges`, `list_signals`), same untrusted wrapper, same scope filtering.
S16.2 **F24 tick-box scopes (ADR-037)** — read, profile (minus excluded fields), research notes as
proposals, propose drafts. Each scope is a CHECK migration plus a test that the served tool list
matches the granted scopes. Half of the machinery exists already: `tools/list` is scope-filtered
and `required_scope` is derived, from ADR-030.
S16.3 **MCP Phase 2** (our own OAuth 2.1 server). Still gated: do it when pasting tokens becomes
the complaint, not before.
S16.4 **F16 learning from edit diffs** — describe, then compare, then summarise recurring edits for
a person to review before anything enters a prompt. Needs ~1,050 sends per arm (the doc's
calculation), so not before real volume.

---

## 3. ADRs this plan needs

| ADR | Decision in one line |
|---|---|
| **031** | `company_profiles` is shared across workspaces with no `org_id`: a deliberate exception to ADR-004, holding public facts only, enforced by a structural test that the table has no org, lead, contact or message column. |
| **032** | Job idempotency is **org-scoped**: the dedupe index becomes `(org_id, kind, dedupe_key)`, because a key built from a shared identifier otherwise lets one workspace silently suppress another's work; lease reclaim already exists and is tested rather than built. |
| **033** | One-click unsubscribe is a signed `https` endpoint mounted outside `/api/v1`, and `List-Unsubscribe-Post` is sent **only** when that URL exists — announcing one-click while offering a mailto is a false claim a receiver can test. |
| **034** | Deliverability may only **pause**: `mailbox_health` drives auto-pause on bounce and complaint rates over a minimum volume, with no manual override, no automatic un-pause, and no path that raises a cap. |
| **035** | The extension gateway is a **third door**: scope `ext:suggest`, unable to reach `/api/v1`, with field-mapping rules shipped inside the package and the server returning data and never logic or scripts. |
| **036** | Resume tailoring happens by **validated edit operations** over confirmed facts; versions are immutable; facts mined from GitHub or a resume are proposals until the user confirms them. |
| **037** | Connector scopes are ticked at connect time, enforced by the database CHECK and the tool catalogue, and there is no scope for sending, approving, submitting or changing `SEND_MODE`. |
| **038** | Public ATS feeds are the sanctioned substitute for scraping job boards; each source's terms and robots rules are checked before a company joins a poll, and LinkedIn data enters only through a file the user uploads. |

---

## 4. Risks and ordering changes

1. **Reordered: S11.0 (stop claiming one-click) moves to the front of P11**, ahead of F21. It is a
   one-line change that removes a false claim from every message we send, and it should not wait
   behind queue work.
2. **Unchanged but reinforced: F21 before any new job kind.** My §1.2.1 finding gives this a second,
   sharper reason than the doc's — the current index can silently drop one workspace's job.
3. **S12.1 (verify the ATS feeds) before S12.2/S12.3.** Three endpoint shapes from one third-party
   article is not a foundation; if a feed needs a token or has moved, the intake design changes.
4. **F7 (mailbox pool) stays after F3**, per the doc: rotation without a health signal just spreads
   an unmeasured problem across more mailboxes.
5. **Risk — P13 is three or four PRs of frontend/extension work** with no server-side precedent in
   this repo. If it over-runs, S13.1–S13.3 (gateway, tables, suggestions) are useful on their own,
   and the extension package itself can slip a phase without blocking anything.
6. **Risk — the 2 GB host.** PDF rendering (S13.7) and feed polling (S12.3) both add memory
   pressure to a box with ~1 GB free. Both run as separate jobs with their own limits, and the
   plan assumes measurement rather than guessing the concurrency.
7. **Risk — DPDP.** None of this changes the open legal question about lawful basis for B2B cold
   email. It does not block the build; it blocks *widening beyond your own use*, and it is not
   mine to decide.
8. ~~**New dependency, stated reason:** `dnspython` for S10.1/S11.5.~~ **Wrong — it is already
   a dependency** (`pyproject.toml` pins `dnspython==2.9.0`, used by the importer's MX check).
   Corrected 2026-10-31 while building S11.5. The new code follows that existing usage: lazy
   import, explicit timeouts, and a failed lookup distinguished from a negative answer.

---

## 5. Decisions taken (2026-10-31)

The owner answered §5's questions. Recorded here because the slices below depend on them.

1. **Multi-user from the start.** Tenant isolation and onboarding are built for several
   workspaces; testing stays on the owner's and a friend's addresses, 10–20 people.
   **The Google client stays unpublished**, because publishing a restricted-scope app
   (`https://mail.google.com/`) requires an annual **paid** CASA security assessment and hard rule
   5 forbids spending without a decision. So the 7-day refresh-token expiry stays a product state
   (ADR-021) and nothing in the design prevents publishing later.
2. **Extension: load unpacked**, architecture kept ready for a store listing (rules in the
   package, privacy policy and single-purpose description deferred).
3. **All three providers — Apollo, Hunter, Snov — with the user's own key optional**, and the
   product fully usable without one; a mock provider in tests. The owner asked whether a "Connect"
   button could fetch the key instead of the user pasting it. **Unverified and deferred to S13.5:**
   an OAuth connect flow needs the provider to offer OAuth *and* us to register an app with them,
   which is key-based for Hunter, client-credentials the user still copies for Snov, and
   partner-gated for Apollo, as far as I know without checking. So `provider_credentials` carries a
   `kind` column (`api_key` | `oauth`) from the first migration, mirroring `mail_credentials.kind`,
   and adding a Connect button later is a handler rather than a migration.
4. **Shared `company_profiles` accepted** — public facts only, no tenant data, enforced by test
   (ADR-031).
5. **Sending stays flexible.** No restriction to `arkvane.in`; every workspace sends from an
   account its owner connected (ADR-023), which is already how it works. P10's go-live checks are
   therefore written **per connected sending domain**, not against one hard-coded domain.

**Ordering the owner asked for, ahead of everything else:** S11.0 (unsubscribe claim), S11.1
(org-scoped dedupe), S11.3 (crash-safety test), S12.1 (verify the ATS feeds), and these doc
corrections. Then P10 onwards as listed.

---

## 6. Questions (answered — kept for the record)

Five, each with the default I will take if you say "default".

1. **Build for yourself first, or multi-user from the start?** It decides whether the Google client
   gets published (an annual paid security assessment) and how much onboarding to build.
   **Default: yourself first** — client stays unpublished, the 7-day reconnect stays a product
   state, onboarding stays minimal.
2. **Chrome extension distribution: public store listing, or load-unpacked only?** A listing needs a
   one-time developer fee (about $5, to confirm at sign-up), a privacy policy and a single-purpose
   description. **Default: load unpacked**, no fee, no listing, revisit when somebody else needs it.
3. **Which contact-provider keys do you already hold (Apollo, Hunter, Snov)?**
   **Default: none** — F9 ships with the key field empty, a fake provider in tests, and no paid
   signup anywhere.
4. **Is the shared `company_profiles` cache acceptable (ADR-031)?** Public facts only, no `org_id`,
   no lead or contact columns, enforced by a structural test. **Default: yes.**
5. **Sending domain: keep `arkvane.in`, or a separate look-alike domain for cold mail?** A second
   domain recurs yearly and needs your purchase, and it would need its own SPF/DKIM/DMARC and its
   own warm-up from zero. **Default: keep `arkvane.in`.**

Not blocking, and yours rather than mine: legal advice on DPDP lawful basis (and whether it covers
F23 pooling, which stays unbuilt until it does).
