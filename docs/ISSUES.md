# outreach-automation — Audit Register

Repo audited: `Harishchand83077/outreach-automation` @ `990fa9f` (33 files, all read).
Severity: **P0** = will send wrong/harmful email, lose data, or is exploitable. **P1** = breaks the
end goal (research → send → reply loop). **P2** = quality/robustness. **P3** = hygiene.

**Verdict column** added 2026-10-09 after independent verification of every ID against the code at
`990fa9f` (see `REPO_ANALYSIS.md`). `Confirmed` = reproduced or evidenced at a specific line.
`Confirmed (absence)` = the claimed feature provably does not exist anywhere in the tree.
`Measured` = demonstrated by running a no-network repro script.
**Result: 43 of 43 original IDs confirmed. None were "Partly". None were "Not reproducible".**
8 further issues were found and added as `NEW-n`.

**Status column** tracks remediation: each ID must end as `Done`, `Superseded-by-rewrite` (with a
test proving the behaviour) or `Won't fix` (with a reason). Live status is in `PROGRESS.md`.

---

## A. Product gaps vs. the end goal

| ID | Sev | Verdict | Issue | Where |
|----|-----|---------|-------|-------|
| A1 | P0 | **Superseded-by-rewrite** | **There is no research.** `generate_insights` asks the LLM to "research" a company from *only its name* (prompt literally says "Base analysis on the company name"). No web search, no site scrape, no news, no enrichment. Output is hallucination dressed as research. | nodes.py:73 |
| A2 | P0 | **Superseded-by-rewrite** | **Nothing researches the person.** `name` reaches the model only as a "Contact:" line and a greeting. No role, LinkedIn, recent posts. | nodes.py:61,206 |
| A3 | P0 | **Superseded-by-rewrite** | **The email prompt never tells the LLM who "we" are or what we offer** ("what we offer", "our product/service and traction" with no data supplied). The model invents the product and traction claims, and they get sent to real people. | nodes.py:219-220 |
| A4 | P1 | **Superseded-by-rewrite** (persona/campaign schema plus the prompts that read them) | Use case is hardcoded to *funding/VC* ("startup and venture capital analyst", "Funding Potential", "Series A/B/C"). Not a generic outreach engine. | nodes.py:53-73 |
| A5 | P1 | **Superseded-by-rewrite** | Excel extra columns (title, website, LinkedIn, notes, segment) are dropped, so they can't feed research or personalization. | excel_loader.py:47-51,82 |
| A6 | P1 | **Superseded-by-rewrite** (`ImapPoller` tracks UID cursors, not lead state; `match_lead` ignores the lead's state entirely, and `NO_RESPONSE -> REPLIED` is a legal transition so a late reply reopens a lead. Tests: `test_reply_ingest.py`, `test_reply_service.py::TestMatching`) | **Reply loop is single-shot.** After one human reply, status → `WAITING_REPLY`, but `reply_monitor` only watches `EMAIL_SENT`. A second reply is never seen. Worse than first audited: when *no* lead sits in `EMAIL_SENT` the inbox is not polled at all. | decision_engine.py:229, reply_monitor.py:31,33-35 |
| A7 | P1 | **Superseded-by-rewrite** (follow-ups scheduled by `domain/scheduling`, sent by `SendLoop`, and cancelled in the same transaction that records a reply, bounce or opt-out. Tests: `test_scheduling.py`, `test_send_loop.py`, `test_reply_service.py::TestActions::test_a_reply_cancels_the_pending_followup`) | No follow-up sequence, no stop-on-reply guarantee, no scheduling/send windows. | — |
| A8 | P1 | **Superseded-by-rewrite** (ten labels in `domain/reply.ReplyLabel`; bounces and auto-replies decided from headers and overruling the model in `reconcile`. Tests: `test_reply_rules.py`, `test_reply_classify.py`) | Reply classifier has only 3 buckets. Missing: unsubscribe/opt-out, out-of-office, bounce, wrong person/referral, hostile. Auto-replies and bounces get classified as real replies. | decision_engine.py:44-50 |

## B. Correctness bugs

| ID | Sev | Verdict | Issue | Where |
|----|-----|---------|-------|-------|
| B1 | P0 | **Done** (replaced by `SEND_MODE`, enforced in one function. Tests: `test_send_guard.py`, `test_config.py::TestLegacyDryRunFlagIsRefused`) | **`DRY_RUN_EMAILS=0` silently disables sending.** `os.getenv("DRY_RUN_EMAILS","")` is truthy for `"0"`. Emails are skipped *and the lead is marked `EMAIL_SENT`*. **Understated in the original audit:** `.env.example:44` instructs "Leave unset or 0 to send real emails", so an operator following the documentation disables every send while the dashboard reports success. | email_utils.py:56, nodes.py:393-400 |
| B2 | P0 | **Done** (the nodes raise instead of returning error text. Tests: `test_error_text_never_emailed.py`) | **LLM failure text gets emailed.** On exception `generate_email` returns `"Error generating email: …"` as the draft *with a success status* and the graph carries on to approval/send. Same for `generate_insights`, and for the `"(simulated LLM response)"` fallback when LangChain is missing. | nodes.py:246-252, 88-89; llm_client.py:106 |
| B3 | P0 | **Done** (`.env.example` is no longer loaded as defaults and `your_`-prefixed values fail startup. Tests: `test_config.py::TestPlaceholderRejection`, `test_config.py::TestEnvExampleIsNotLoaded`) | **Config validation is defeated by placeholders.** `.env.example` is loaded as defaults and lines 18-26 actively copy its placeholders into `os.environ`; `validate()` only tests non-empty. Measured on a clean machine: all three required keys hold placeholders, `validate()` passes, `/api/config/check` reports `groq_configured: true`. | config/config.py:13,18-26,63-76 |
| B4 | P0 | **Done** (a positive reply sends nothing and never books a meeting; `MEETING_BOOKED` is human-only. Tests: `test_reply_never_autosends.py`) | **Positive reply auto-sends an email with no human gate, and marks `MEETING_BOOKED` / `meeting_booked=1` though no meeting exists** (it only sent a Calendly link). The UI claims "Nothing is sent without your approval" in three places. | decision_engine.py:119-124,161-167; App.jsx:255,444,463 |
| B5 | P0 | **Superseded-by-rewrite** (a reply needing a human becomes a `kind='reply'` draft at `pending_review` in the same queue as a first email - no CLI `input()`, no per-lead blocking. Tests: `test_reply_drafter.py`) | **`question_reply` approval is never wired.** `wait_approval` is called only with `"insights"` and `"email"`; nothing registers `"question_reply"`. Measured: `approve_question_reply()` returns `False`, so the endpoint always 404s and the dashboard card is unreachable. `handle_question` uses CLI `input()` instead, and leads are processed sequentially, so one stuck lead blocks all others. | decision_engine.py:181-233, nodes.py:124,286, approval_store.py:105 |
| B6 | P1 | **Done** (`APPROVAL_MODE` is a declared setting. Tests: `test_config.py::TestDeclaredSettings`) | **`config.APPROVAL_MODE` is not defined on `Config`**, only monkey-patched in `api_server.startup`. Measured: `'APPROVAL_MODE' in Config.__dict__` is `False`, so CLI mode with `AUTO_APPROVE=0` raises `AttributeError` at the first approval. | nodes.py:121,283; config/config.py |
| B7 | P1 | **Superseded-by-rewrite** | **Excel loader crashes on any row longer than the header** (`col_map[i]` → measured `KeyError: 3`). Header normalisation is `.title()` only — measured failures: `E-mail`→`E-Mail`, `Company Name`, `First Name`, `full_name` all miss the required set. CSV branch computes normalised headers and ignores them; `.xls` is accepted by the API but openpyxl cannot read it. | excel_loader.py:19-20,34,63-76; api_server.py:189 |
| B8 | P1 | **Superseded-by-rewrite** | **Concurrency slot is held during human approval.** The semaphore wraps the whole workflow including `wait_approval`, whose timeout defaults to `None` (waits forever). With `MAX_CONCURRENT_LEADS=5`, five un-reviewed leads freeze the pipeline. | runner.py:65, approval_store.py:30,53 |
| B9 | P1 | **Superseded-by-rewrite** | **In-flight leads are orphaned on restart.** `workflow.compile()` is called with no checkpointer and approvals live in an in-memory dict. After a restart leads sit in `INSIGHTS_GENERATED`/`EMAIL_DRAFTED` forever because `/api/run` only picks `INIT`. | graph.py:109, approval_store.py:18, api_server.py:218 |
| B10 | P1 | **Superseded-by-rewrite** (`readonly=True` on SELECT, `BODY.PEEK[]` on FETCH, progress by UID; no `STORE` anywhere. Tests: `test_mail_imap.py::TestTheMailboxIsNeverModified`) | **Replies can be silently lost.** Search is `UNSEEN` but `fetch(RFC822)` implicitly sets `\Seen` *before* the sender filter runs, then `store +FLAGS \Seen` sets it again. Mail for a lead not in `EMAIL_SENT` is discarded yet marked read, and unrelated inbox mail is marked read as a side effect on the operator's real mailbox. Should be `BODY.PEEK[]` + UID tracking. | email_utils.py:99,108,132,142 |
| B11 | P1 | **Superseded-by-rewrite** (matching is threading headers first, then a DSN `Final-Recipient`, then sender+subject bounded to 30 days; quoted text stripped before classification. Tests: `test_reply_service.py::TestMatching`, `test_mail_parse.py::TestQuoteStripping`) | **Reply matching is by sender address only.** No `In-Reply-To`/`References` threading, no stored `Message-ID` of what we sent. Any unrelated mail from that address counts as "the reply". Quoted original text is not stripped before classification. | reply_monitor.py:54-55 |
| B12 | P1 | **Superseded-by-rewrite** | **No idempotency on send.** SMTP succeeds → DB write fails/crash → lead not `EMAIL_SENT` → resend. Same ordering in `handle_positive`. No send-intent record / outbox. | nodes.py:386-400, decision_engine.py:119-167 |
| B13 | P1 | **Superseded-by-rewrite** | **Subject parsing is fragile.** Line 1 is used only if it starts with `Subject:`; an LLM preamble ("Here's your email:") sends a generic subject and leaks the preamble into the body. No validation that an approved draft is non-empty. | nodes.py:364-367 |
| B14 | P2 | **Superseded-by-rewrite** | HTML body built with `replace("\n","<br>")` and no escaping; `<`/`&` from LLM or human text breaks or injects markup. Footer repeats the calendar link the prompt already placed in the body. (Also: closing tags inverted, `</html></body>`.) | nodes.py:370,378,381 |
| B15 | P2 | **Superseded-by-rewrite** | Rejection loops are uncapped. `*_attempts` is incremented but measured: the string `attempts` never appears in either conditional-edge router, so nothing limits the loop → unbounded LLM spend until LangGraph's recursion limit (25) raises. | graph.py:40-63, nodes.py |
| B16 | P2 | **Superseded-by-rewrite** | `INSERT OR IGNORE` means re-uploading a corrected sheet never updates name/company. `email` is globally `UNIQUE`, so the same person at two companies, or a re-contact in a later campaign, is impossible. | database.py:33,63 |
| B17 | P2 | **Superseded-by-rewrite** (the label is an enum field in a validated schema, and the classification is persisted before anything acts on it. Tests: `test_reply_classify.py::TestTheLabelIsAField` - including the exact `not positive` text - and `test_reply_service.py::TestStoringAndClassifying`) | Classification parse is substring-based and order-dependent. **Measured: a reply reading `"not positive"` classifies as `positive`**, as does "This is not a question, and not positive either" — which, combined with B4, auto-sends and marks a meeting booked. Classification is not persisted before acting, so it re-runs on every retry. | decision_engine.py:68-82 |
| B18 | P2 | **Superseded-by-rewrite** - the module is deleted. `app/core/settings.py` parses every value through pydantic-settings at construction, and no semaphore is built at import. Test: `tests/unit/test_settings.py::TestSendModeIsNamedNotBoolean` | `int(os.getenv(...))` at class-body scope crashes on a bad value at import. `asyncio.Semaphore` created at import time. Deprecated `get_event_loop()`, `datetime.utcnow()`, `@app.on_event`. `created_at` uses SQL `CURRENT_TIMESTAMP` (space-separated) while `updated_at` is written ISO-`T`, so the two are not comparable and `ORDER BY updated_at` is unreliable. | config/config.py:40,43,48,59,60; runner.py:23; llm_client.py:115; email_utils.py:163; database.py:42,85,157,168; api_server.py:128 |

## C. Security (the API is currently an open relay for your Gmail)

| ID | Sev | Verdict | Issue | Where |
|----|-----|---------|-------|-------|
| C1 | P0 | **Superseded-by-rewrite** - `api_server.py` is deleted. Every `/api/v1` route requires a verified Supabase token resolved to a `members` row. Test: `tests/integration/test_api_auth.py::TestNoTokenGetsNothing` | **Zero authentication on every endpoint.** Anyone who finds the URL can upload leads, start runs, approve drafts, export every lead as CSV, and send email from your account. | api_server.py (all routes) |
| C2 | P0 | **Done** (the client filename never reaches the filesystem; the sheet is parsed from memory. Tests: `test_upload_security.py`) | **Path traversal on upload.** `path = _root/"uploads"/file.filename` uses the client-supplied filename unsanitised (`../../x.xlsx` passes the extension check). Files are never cleaned up. | api_server.py:192 |
| C3 | P1 | **Superseded-by-rewrite** - CORS is an explicit allowlist from `CORS_ALLOWED_ORIGINS`, never `*`. Test: `tests/integration/test_api_auth.py::TestHardening::test_cors_is_never_a_wildcard` | CORS `allow_origins=["*"]` with `allow_credentials=True`. | api_server.py:77-83 |
| C4 | P1 | **Superseded-by-rewrite** - the limiter is keyed on the token subject, covers every method, and prunes idle keys. Test: `tests/integration/test_api_auth.py::TestRateLimiting` | Rate limit keyed on `request.client.host` (all clients share the proxy IP behind a PaaS), in-memory, POST-only, never pruned of idle IPs. | api_server.py:54,88,89 |
| C5 | P1 | **Superseded-by-rewrite** (replies are fenced and named as data in both the classify and reply-draft prompts, output is a structured schema, and no label in the policy table can send. Tests: `test_reply_classify.py::TestThePrompt`, `test_reply_drafter.py::test_the_reply_is_fenced_in_the_prompt`, `test_reply_never_autosends.py`) | **Prompt injection.** Lead replies and Excel values are pasted raw into prompts with no delimiting or structured output. A reply arguing for a positive classification triggers an automatic send (B4). | decision_engine.py:52-63 |
| C6 | P1 | **Confirmed** | Uses a Gmail **app password over SMTP/IMAP** for bulk cold outreach: account-suspension risk, no API-based send, no per-domain separation. | email_utils.py, .env.example |
| C7 | P2 | **Superseded-by-rewrite** - every list is cursor-paginated and every list row is a summary; bodies only on `GET /leads/{id}`. Test: `tests/integration/test_api_endpoints.py::TestLeadLists::test_a_list_row_carries_no_bodies` | `GET /api/leads` returns every lead's full insights/draft/reply on every poll, and the frontend polls 5 endpoints every 2.5 s unconditionally. Leaks PII to anyone (C1) and does not scale. | api_server.py:164, App.jsx:104-110 |
| C8 | P3 | **Done** (`sample_leads.xlsx` deleted; fixtures are `@example.com` only, per `tests/fixtures/leads_example.csv`. History rewrite deliberately skipped - see ADR notes) | `sample_leads.xlsx` was committed to a then-public repo with two real personal Gmail addresses. Both are confirmed to be the owner's own, and the repo is now **private** (verified). | sample_leads.xlsx |

## D. Deliverability & compliance (will get you blocked/blacklisted at volume)

| ID | Sev | Verdict | Issue |
|----|-----|---------|-------|
| D1 | P0 | **Superseded-by-rewrite** | No unsubscribe/opt-out mechanism, no `List-Unsubscribe` header, no suppression list, no sender postal address (CAN-SPAM / GDPR / CASL exposure). |
| D2 | P0 | **Superseded-by-rewrite** (DSNs parsed from headers, hard bounces suppressed and the lead closed in one transaction, soft bounces deferred instead. Tests: `test_mail_parse.py::TestBounces`, `test_reply_service.py::TestActions` (hard, soft and deferral cases)) | No bounce handling: `MAILER-DAEMON` mails are ignored; bad addresses keep being counted as "sent". |
| D3 | P1 | **Superseded-by-rewrite** | No send throttling, jitter, daily caps, warm-up, or per-domain limits. Gmail personal ≈500/day, Workspace ≈2000/day; exceeding it risks lockout. |
| D4 | P1 | **Superseded-by-rewrite** | No email verification beyond `'@' in email`: no syntax check, MX check, role-address (`info@`) or free-domain filtering, no dedupe-by-domain. (`excel_loader.py:45,80`) |
| D5 | P2 | **Partly** (headers, plain text and the footer done; SPF/DKIM/DMARC guidance is a Phase 9 runbook item) | No SPF/DKIM/DMARC guidance, no dedicated sending domain, no plain-text/HTML hygiene, no `Message-ID`/`Date`/`Reply-To` control on outbound mail. |
| D6 | P2 | **Confirmed** | Lead data and reply text go to a third-party LLM with no consent/retention policy; `PRIVACY.md` is 22 generic lines. No deletion path per lead — there is no `DELETE` route in the API at all. |

## E. Architecture & operations

| ID | Sev | Verdict | Issue |
|----|-----|---------|-------|
| E1 | P1 | **Superseded-by-rewrite** - the worker is a separate process (`python -m app.worker`) with its own five tasks. Test: `tests/integration/test_worker_scheduler.py::TestTheProcess::test_all_five_tasks_are_assembled` | **Workers run inside the API process** (`api_server.py:135`), so on a free PaaS tier that sleeps after ~15 min idle, reply monitoring stops exactly when you are waiting for replies. |
| E2 | P1 | **Superseded-by-rewrite** | SQLite on ephemeral disk (data wiped on redeploy); a new connection per call throughout `database.py`, no WAL, no `busy_timeout` → "database is locked" under parallel writes. |
| E3 | P1 | **Superseded-by-rewrite** | **State model is a single mutable row.** Insights/draft are overwritten; no event history, no record of what was *actually sent* (final subject/body/timestamp/Message-ID), no audit of who approved what. |
| E4 | P1 | **Superseded-by-rewrite** | No job queue / worker separation. Parallelism is `asyncio.gather` in one process; no job-level retries, no dead-letter, no resumability. |
| E5 | P1 | **Superseded-by-rewrite** | LLM: retry on *every* exception including auth/400 (`llm_client.py:85`), fixed 2-10 s backoff ignoring `Retry-After`, no token/cost accounting, no model fallback, `max_tokens=2048` fixed. |
| E6 | P2 | **Done** | ~~No tests, no CI, no type checking, unpinned deps, no lockfile, no `pyproject.toml`.~~ Fixed: `pyproject.toml` with exact pins plus `uv.lock`, ruff (lint and format), mypy strict over `app/` and `config/`, pytest, GitHub Actions CI, `pre-commit` with `gitleaks`, Dependabot. `requirements.txt` deleted. The Dockerfile is still outstanding and belongs to Phase 9. |
| E7 | P2 | **Confirmed** | Observability: Rich console logs only; no structured logs, run IDs, metrics, alerting, or per-lead timeline. |
| E8 | P2 | **Confirmed** | Dashboard: approvals one-by-one only (no bulk endpoint exists), no filters/search/pagination, no run/campaign concept, no per-step status view, no way to pause/cancel a run or a single lead. |

## F. Docs/hygiene

| ID | Sev | Verdict | Issue |
|----|-----|---------|-------|
| F1 | P3 | **Done** | `INSTALL.md` deleted — it documented a `requirements-full.txt`, a "lite/dummy mode", pandas and conda, none of which existed. `docs/SETUP.md` is now the single install guide. The "dummy graph" comment in `runner.py` and the "dummy LLM" warning in `llm_client.py` are gone. |
| F2 | P3 | **Done** | `DEPLOY.md` deleted — it described Render plus Neon, which both ADR-001 and the target architecture reject; Phase 9 writes `docs/DEPLOY.md`. `RUN.md` deleted along with its hardcoded local Windows path. `README.md` rewritten: it states plainly that this is a prototype under rewrite and that sending is off by default. |
| F3 | P3 | **Done** | Every module docstring now names its real path. `.gitignore` covers `uploads/`, `local/` and the tool caches. The root `ARCHITECTURE.md` is deleted: `docs/REPO_ANALYSIS.md` describes the current system accurately, and `docs/ARCHITECTURE_TARGET.md` is the target. |

---

## NEW — found during verification, not in the original audit

| ID | Sev | Verdict | Issue | Where |
|----|-----|---------|-------|-------|
| NEW-1 | P1 | **Done** (`runner` no longer stores an exception as a draft. Tests: `test_error_text_never_emailed.py::TestRunnerDoesNotStoreExceptionsAsDrafts`) | On workflow crash the raw exception string is written into the `email_draft` column — a second path, independent of B2, for error text to reach a field the UI renders and offers to send. | runner.py:88 |
| NEW-2 | P1 | **Done** (a lead is never marked sent when nothing was sent. Tests: `test_send_guard.py::TestLeadIsNotMarkedSentWhenNothingWasSent`) | `handle_positive` ignores `send_email`'s return value, so the lead is marked `MEETING_BOOKED` even when the send failed outright. | decision_engine.py:119 |
| NEW-3 | P2 | **Verified, not yet fixed** (the rewritten stats read the `events` audit trail, not the current status; lands with the UI) | `get_stats` counts `emails_sent` as *current* `status == 'EMAIL_SENT'`, so every lead that progresses to `REPLIED`/`MEETING_BOOKED` silently decrements the "sent" total. With B4 inflating meetings, both headline numbers on the dashboard are wrong. | database.py:145 |
| NEW-4 | P2 | **Superseded-by-rewrite** - the CSV export endpoint is deleted with `api_server.py` and is not reinstated. If it returns, the formula-injection guard is a precondition (noted in `PROGRESS.md`) | `/api/leads/export/csv` writes lead and LLM text into CSV with no formula-injection guard: a lead whose name begins `=`, `+`, `-` or `@` executes on open in Excel. The endpoint is unauthenticated (C1) and the rate limiter is POST-only, so it is not even throttled. | api_server.py:306-330 |
| NEW-5 | P2 | **Done** (`startup()` keeps the task handle, so the duplicate guard in `start_workers()` works - `api_server.py:146-149`. No test: the legacy server is being replaced, and the rewritten worker owns its own tasks) | ~~`startup()` spawned `_workers_loop()` without assigning `_workers_task`, so the duplicate guard inside `start_workers()` could not work and a second reply monitor could be spawned.~~ Fixed: the task handle is stored. Found independently by ruff (RUF006) while setting up CI. | api_server.py |
| NEW-6 | P3 | **Superseded-by-rewrite** - `os.chdir` is gone with the module. Nothing in `app/` mutates the process working directory | `os.chdir(_root)` runs at import time, mutating process-global working directory for anything else hosted in the same interpreter. | api_server.py:25 |
| NEW-7 | P3 | **Superseded-by-rewrite** - `database.py` is deleted; `app/db` uses SQLAlchemy bound parameters throughout | `update_lead` interpolates `fields.keys()` into the SQL string. Not exploitable today — every call site passes literals — but one user-controlled kwarg away from SQL injection. | database.py:86 |
| NEW-8 | P3 | **Partly** (`.xls` rejected with a clear message and `.csv` accepted; the `--skip-monitor` docstring went with the F1-F3 rewrite) | `/api/upload` accepts only `.xlsx`/`.xls` while `excel_loader` supports CSV, so the CSV path is dead from the API; and `main.py:22` documents a `--skip-monitor` flag that is really `--no-monitor`. | api_server.py:189, main.py:22,78 |

---

### Counts

Counted from the tables above rather than carried forward by hand, because they had drifted:
the "43 original / 51 total" figures in earlier revisions of this file and of `PROGRESS.md`
undercounted the lettered IDs, which are 51 on their own.

Original audit (A–F): P0: 12 · P1: 24 · P2: 11 · P3: 4 — **51, all confirmed**.
Found during verification (NEW-1…8): P1: 2 · P2: 3 · P3: 3 — **8 added**.
**Total tracked: 59.**

| Status | Count | IDs |
|---|---|---|
| **Superseded-by-rewrite** (each with a test proving the behaviour) | 29 | A1–A8, B5, B7–B17, C5, D1, D2, D3, D4, E2–E5 |
| **Done** | 14 | B1, B2, B3, B4, B6, C2, C8, E6, F1, F2, F3, NEW-1, NEW-2, NEW-5 |
| Partly | 2 | D5, NEW-8 |
| Verified, not yet fixed | 14 | B18, C1, C3, C4, C6, C7, D6, E1, E7, E8, NEW-3, NEW-4, NEW-6, NEW-7 |

### What is genuinely good (keep)

- Clean separation: the workflow ends at send; replies are handled by separate loops. The target
  architecture keeps this shape.
- Human-in-the-loop with editable drafts is a sound default, and the UI's approve/edit/reject flow
  is the right interaction — it is the *enforcement* that is missing, not the idea.
- Async structure, `LeadState` typing and the graph routing are straightforward and readable.
- `.env` is correctly gitignored and **no secrets appear anywhere in the 11-commit history**
  (verified by grep over `git log --all -p`).
- Dependency direction is acyclic; no module imports upward into an entry point.
