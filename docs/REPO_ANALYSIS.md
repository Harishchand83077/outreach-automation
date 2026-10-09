# Repo Analysis — outreach-automation @ `990fa9f`

Read-only analysis of every tracked file (33 files, 11 commits), performed before any code change.
Baseline tagged `baseline-pre-rewrite`. Companion documents: `ISSUES.md` (verdict per issue),
`ARCHITECTURE_TARGET.md` (where we are going), `DECISIONS.md` (ADRs).

---

## (a) What the system does today, in plain words

A single operator puts a three-column spreadsheet (`Name`, `Email`, `Company`) in front of the tool.
For each row it asks an LLM to "research" the company — using nothing but the company's *name* — then
asks the LLM to write a cold outreach email about funding or partnership, shows both to the operator
for approval, and sends the result from a Gmail account over SMTP. Two background loops then poll the
Gmail inbox over IMAP, label any reply as `positive`/`question`/`no_interest`, and act: a positive
reply triggers an automatic "book a meeting" email and marks the lead `MEETING_BOOKED`; a question
drops to a blocking terminal prompt; a no-interest reply closes the lead.

It is a working demo of the shape of the product. It is not safe to point at real recipients: the
"research" is ungrounded invention, the API has no authentication, and a lead's own reply can cause
the system to send mail without a human in the loop.

## (b) Module map

Flat layout at the repo root; every module's docstring claims a package path (`workflows/`,
`utils/`, `db/`, `workers/`) that does not exist (F3). Only `config/` is a real package.

| Module | Responsibility | Imports from |
|---|---|---|
| `main.py` | CLI entry point; arg parsing, 3 run modes | config, database, excel_loader, runner, reply_monitor, decision_engine, logger |
| `api_server.py` | FastAPI app: upload, run, approve/reject, stats, CSV export. Also hosts both background workers in-process | config, database, excel_loader, runner, approval_store, reply_monitor, decision_engine, logger |
| `config/config.py` | Class-attribute config read from `.env` **and `.env.example`** | dotenv |
| `excel_loader.py` | Parse `.xlsx` via openpyxl / `.csv` via stdlib into `{name,email,company}` | logger |
| `database.py` | Async SQLite (aiosqlite): one `leads` table, CRUD, stats | config, logger |
| `state.py` | `LeadState` TypedDict passed between graph nodes | — |
| `graph.py` | LangGraph `StateGraph` wiring the 6 nodes and 2 conditional loops | state, nodes, logger |
| `nodes.py` | The 6 node functions: insights, approve, draft, approve, send, end | state, llm_client, email_utils, database, config, logger |
| `runner.py` | Runs one graph per lead under an `asyncio.Semaphore(5)`, gathered | graph, state, database, config, logger |
| `llm_client.py` | Groq via LangChain, tenacity retry, soft-fails to a stub string | config, logger |
| `email_utils.py` | `send_email` (aiosmtplib) and `check_inbox` (imaplib, sync in executor) | config, logger |
| `reply_monitor.py` | Loop: poll inbox, match reply to lead by sender address, set `REPLIED` | database, email_utils, config, logger |
| `decision_engine.py` | Loop: classify `REPLIED` leads, then act (send / prompt / close) | database, llm_client, email_utils, config, logger |
| `approval_store.py` | **In-memory** dict of pending approvals + `asyncio.Event` per item | logger |
| `logger.py` | Rich console logging | rich |
| `frontend/src/App.jsx` | 622-line single-component React SPA; polls 5 endpoints every 2.5 s | — |

Dependency direction is clean (nothing imports upward into `main`/`api_server`), and the workflow
genuinely ends at send with replies handled separately. That separation is the part of the design
worth keeping.

## (c) End-to-end data flow, and the statuses actually implemented

```
sheet ──► excel_loader ──► upsert_lead (INSERT OR IGNORE) ──► leads table (status=INIT)
                                                                   │
  POST /api/run  or  main.py leads.xlsx                            ▼
           └──► runner.run_all_leads ──► per-lead LangGraph ──► generate_insights
                  (Semaphore(5))                                   ▼
                                                      human_validate_insights ◄─┐ reject
                                                                   ▼            │
                                                            generate_email ─────┘
                                                                   ▼
                                                       human_validate_email ◄──┐ reject
                                                                   ▼           │
                                                            send_email_node ───┘
                                                                   ▼
                                                               end_node
                            ┌──────────────────────────────────────┘
  reply_monitor loop  ──────┤ polls IMAP UNSEEN, matches by sender address only
  (every 5 min)             ▼
                        status=REPLIED
                            ▼
  decision_engine loop ──► classify_reply ──► positive → auto-send + MEETING_BOOKED
  (every 5 min, +30 s)                   ├──► question → blocking input() prompt
                                         └──► no_interest → CLOSED
```

**Statuses actually written to the DB** (string literals, no enum, no transition table):
`INIT`, `INSIGHTS_GENERATED`, `INSIGHTS_APPROVED`, `INSIGHTS_REJECTED`, `EMAIL_DRAFTED`,
`EMAIL_APPROVED`, `EMAIL_SENT`, `EMAIL_FAILED`, `ERROR`, `REPLIED`, `WAITING_REPLY`,
`MEETING_BOOKED`, `CLOSED`.

**What the docs claim versus what exists.** `README.md` calls the system "Production-ready".
`ARCHITECTURE.md` describes the same flow accurately but omits every gap below. There is no
follow-up sequence, no send window, no cap, no suppression, no bounce handling, no campaign or run
concept, no multi-user notion, and no audit history — the lead row is mutated in place, so the
system cannot answer "what exactly did we send to this person, and when?".

Transitions are implicit: any module may call `update_lead(email=..., status=...)` with any string.
`WAITING_REPLY` is a dead end — `reply_monitor` only polls `EMAIL_SENT`, so a lead that reaches
`WAITING_REPLY` after our reply is never watched again (A6).

## (d) Config actually read, and external services touched

Read via `os.getenv` in `config/config.py`: `GROQ_API_KEY`, `GROQ_MODEL`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_EMAIL`, `SMTP_PASSWORD`, `SMTP_TIMEOUT`, `SMTP_USE_TLS`, `IMAP_HOST`, `IMAP_PORT`,
`IMAP_EMAIL`, `IMAP_PASSWORD`, `CALENDAR_LINK`, `DB_PATH`, `MAX_CONCURRENT_LEADS`,
`REPLY_CHECK_INTERVAL_MINUTES`, `LOG_LEVEL`. Read elsewhere, bypassing `Config` entirely:
`DRY_RUN_EMAILS` (`email_utils.py:56`), `AUTO_APPROVE` (`nodes.py:32`). Monkey-patched onto the
config object at runtime and never declared: `APPROVAL_MODE` (`api_server.py:131`). Frontend:
`VITE_API_URL`.

Note `config/config.py:13` loads `.env.example` as a source of defaults, and lines 18–26 then copy
placeholder values out of it into `os.environ` for the three "required" keys. Consequence, measured
on this machine: all three required keys currently hold `.env.example` placeholders,
`config.validate()` passes, and `/api/config/check` reports `groq_configured: true`. The validation
gate is decorative (B3).

External services: **Groq** (chat completions via `langchain-groq`), **Gmail SMTP** on 587 and
**Gmail IMAP** on 993 using an app password, and a **Calendly** link embedded in prompts and in the
mail footer. Nothing else — in particular, nothing fetches the web, which is the root of A1.

## (e) How to run it locally

```
# deps (unpinned, no lockfile)
python -m pip install -r requirements.txt
# config
cp .env.example .env     # then fill in real values — see (d); placeholders silently "validate"
# CLI
python main.py leads.xlsx            # full run; add --no-monitor to stop after sending
python main.py --status              # print the leads table
python main.py --workers-only        # reply monitor + decision engine only
# API + dashboard
uvicorn api_server:app --reload --port 8000
cd frontend && npm install && npm run dev
```

Imports only resolve from the repo root (flat layout, no `pyproject.toml`, nothing installable).
`main.py`'s own usage string documents `--skip-monitor`; the real flag is `--no-monitor` (NEW-8).

**Baseline measured on this machine** (Python 3.12.1 venv, deps installed): `python -m compileall`
clean; all 15 modules import successfully; `langchain-groq`, `langgraph`, `langchain-core`,
`tenacity`, `aiosmtplib`, `openpyxl` and `groq` all present, so `llm_client` takes its real
LangChain path rather than the `"(simulated LLM response)"` stub. No tests to run.

## (f) Dead, duplicated and misleading code

- `excel_loader.py:34` computes normalised CSV headers into `cols` and never uses them; the CSV
  branch matches raw keys instead. The CSV path is also unreachable from the API, which accepts only
  `.xlsx`/`.xls` (`api_server.py:189`).
- `approval_store.approve_question_reply` + `POST /api/leads/{email}/approve-question-reply` +
  the `question_reply` branches in `App.jsx` form a complete feature whose producer was never
  written — nothing ever registers that pending key, so the endpoint always 404s (B5, measured).
- `api_server.start_workers()` is never called; `startup()` spawns the workers directly and does not
  record the task handle, so the guard inside `start_workers` would not prevent a duplicate pair
  (NEW-5).
- `api_server.py:218-222` re-filters for `status == "INIT"` a list already fetched by that status.
- `runner.py:76` comments on a "dummy graph" that no longer exists; `llm_client`'s no-op tenacity
  and no-LangChain fallbacks are dead now that both are installed (and dangerous if they revive).
- `INSTALL.md` documents `requirements-full.txt`, a "lite/dummy mode", pandas, numpy and conda —
  none of which exist (F1). `RUN.md:13,149` hardcode `c:\Users\Mi\Downloads\automation` (F2).
  `DEPLOY.md` devotes a step to Neon Postgres that `database.py` cannot use (F2). Every module
  docstring states a false file path (F3).
- `nodes.py:381` emits `</html></body>` — closing tags inverted.

## (g) Tests and CI

None, as expected. No `tests/`, no `.github/`, no `Dockerfile`, no `pyproject.toml`, no `Makefile`,
no type checking, no linter config. `requirements.txt` pins nothing (`>=` throughout) and there is
no lockfile, so two installs a week apart are different programs.

## (h) Secrets and personal data

- **No secrets found.** `git log --all -p` grepped for Groq/AWS key shapes, private-key headers and
  non-placeholder `SMTP_PASSWORD=`/`GROQ_API_KEY=` assignments: zero hits across all 11 commits.
  `.env` is correctly gitignored and was never committed. `.env.example` contains placeholders only.
- **Personal data is committed.** `sample_leads.xlsx` holds two real Gmail addresses
  (`sanketyadavdohar123@…`, `harishchand.iws@…`) paired with unrelated company names. The owner has
  confirmed both are his own, so there is no third-party exposure, and the repository has since been
  made **private** (verified). Remediation: delete the file, ship synthetic `@example.com` fixtures
  instead, and skip the history rewrite — the data is self-owned and the repo is no longer public,
  so a force-push is not worth its risk. A `git-filter-repo` recipe is recorded in `RUNBOOK.md`
  should that judgement ever change.
- `.gitignore` omits `uploads/` and `local/`, so files uploaded through the API — real lead sheets —
  sit untracked-but-visible in the working tree and are one `git add -A` from being committed (F3).

---

## Top risks, in priority order

1. **The API is an unauthenticated relay for the operator's mailbox.** No route checks any
   credential (C1), CORS is `*` with credentials allowed (C3), and the upload handler writes the
   client-supplied filename straight into a path (C2). Anyone who learns the URL can upload leads,
   start a run and send mail as the operator.
2. **A lead's reply can cause an outbound email with no human gate.** `handle_positive` sends and
   marks `MEETING_BOOKED` on a bare LLM label (B4), the reply text is pasted unfenced into the
   classifier prompt (C5), and the label is derived by substring test — measured: a reply reading
   *"not positive"* classifies as **positive** (B17). The UI asserts "Nothing is sent without your
   approval" in three places; that is untrue today.
3. **Nothing is durable or idempotent.** Approvals live in a process dict (B9), so a restart orphans
   every in-flight lead; sends write the DB only after SMTP returns, so a crash in between re-sends
   on the next run (B12); the single mutable lead row keeps no record of what was actually sent (E3).
4. **Deliverability is entirely unprotected.** A grep across every `.py` and `.jsx` for follow-up,
   unsubscribe, suppression, bounce, jitter, cap, warm-up, threading-header and retention terms
   returns **7 incidental hits in the whole codebase**. At volume this burns the sending domain.
5. **The "research" is invention.** The insights prompt instructs the model to work from the company
   name alone (`nodes.py:73`), and the drafting prompt asks for "our product/service and traction"
   while supplying no facts about the sender (`nodes.py:219`). Every concrete claim in a sent email
   is therefore unverifiable by construction (A1, A3).
