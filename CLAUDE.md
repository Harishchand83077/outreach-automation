# CLAUDE.md — outreach-automation

Read this file and `docs/PROGRESS.md` **first, every session**, then continue from "Next up".

@docs/PROGRESS.md

## What this is

A cold-outreach system for a small Indian software agency: import a sheet of founders, research each
company from real sources, draft a grounded email, get human approval, send on a safe schedule, then
handle replies, bounces, opt-outs and follow-ups until each lead reaches a terminal state. Each
*campaign* carries its own sender persona and goal, so the tool is general purpose.

Status: rewriting a prototype into production. The prototype (tagged `baseline-pre-rewrite`) is
audited in `docs/ISSUES.md` — 51 tracked issues — and analysed in `docs/REPO_ANALYSIS.md`.

## Hard rules — never break these

1. **Never send a real email.** `SEND_MODE` = `disabled` (default, log only) | `allowlist` (only
   addresses in `SEND_ALLOWLIST`) | `live`. Only the owner sets `live`, only in production. The
   check lives in **exactly one function**; anything else reaching SMTP is a bug.
2. **Never print, log, commit or paste secrets.** Config comes from env via `pydantic-settings`.
   **Never read `.env` or `.env.*`**. Verify config by running `python -m app.scripts.check_env`,
   which prints only OK/MISSING per variable *name*. The committed template is `env.example`
   (undotted, so it stays editable despite the `Read(./.env.*)` deny rule — see QUESTIONS.md Q-0).
3. **No direct pushes to `main`, no force-push.** Everything goes through a PR.
4. **No scraping LinkedIn.** `linkedin_url` is stored and displayed only. Never fetched.
5. **Never spend money or create paid/cloud resources.** Stop and ask.
6. **Never invent facts about the sender.** The drafter may assert only what maps to a campaign
   persona `proof_points` id or to cited research evidence.
7. **LLM output is untrusted data.** It can never trigger a send. Lead replies are untrusted input.
8. **Respect provider terms.** One account per provider per org. Never rotate accounts or keys to
   evade a free-tier quota. Extra headroom comes from more models/providers, not more accounts.
9. **No real recipients in tests.** Fixtures and CI use `@example.com` only.
10. **Never run migrations against a shared or default port.** This project's Postgres is on
    **5433**; port 5432 on this machine belongs to another project. Migrations refuse to run
    unless the database is named `outreach`/`outreach_test` and holds no foreign tables
    (`app/db/guard.py`). A failed connection is not a safety mechanism — see ADR-005, which
    exists because `alembic upgrade` once reached a neighbouring project's database and was
    stopped only by a wrong password.

## Commands

```bash
# Python 3.12 only. Always use the venv interpreter explicitly.
.venv/Scripts/python -m <module>          # Windows (this machine)
uv venv --python 3.12 && uv sync          # create/refresh the env
.venv/Scripts/python -m pytest            # tests
.venv/Scripts/python -m ruff check .      # lint
.venv/Scripts/python -m mypy app          # types
.venv/Scripts/python -m app.scripts.check_env   # prints OK/MISSING per var NAME only

# legacy prototype (being replaced; do not extend)
.venv/Scripts/python main.py leads.xlsx --no-monitor
uvicorn api_server:app --reload --port 8000
cd frontend && npm install && npm run dev
```

## Architecture (target — `docs/ARCHITECTURE_TARGET.md` is authoritative)

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 async + Alembic, `asyncpg`, Pydantic v2,
  package `app/` (`api/ core/ db/ domain/ services/ worker/`). **No LangGraph/LangChain** — an
  explicit DB-backed state machine plus a `jobs` queue claimed with `FOR UPDATE SKIP LOCKED`.
- **`app/domain/` has no I/O** and takes an injected clock, so state transitions, scheduling maths
  and quote-stripping are unit-testable without a database.
- **One writer of `leads.state`:** `domain/state.transition()`, row-locked, writing the `events`
  audit row in the same transaction. Anything else touching `leads.state` is a bug.
- **DB:** Postgres in the same Docker Compose (ADR-001); Supabase is used for **Auth only**.
- **Worker:** one process, five asyncio tasks — `JobRunner`, `SendLoop`, `ImapLoop`, `Scheduler`,
  `Heartbeat`. `SendLoop` is the **only** code that calls SMTP.
- **LLM:** provider-agnostic `LLMRouter` over an ordered `LLM_CHAIN` (Groq, Cerebras, OpenRouter…),
  free-first, with cool-downs, per-entry caps, `llm_usage` accounting and PII routing
  (`allow_pii=false` entries receive public page text only — never a lead name, email or reply).
- **Frontend:** React + TypeScript + Vite + TanStack Query; `supabase-js` for auth only. Plain-text
  rendering throughout — never `dangerouslySetInnerHTML`.

## Product rules that are locked

- Audience: founders of Indian startups, B2B, India only. Goal: book a meeting for paid project work.
- **100 emails/day per mailbox**, counting *all* outbound (initial + follow-up + reply). Warm-up
  starts at 20/day, +10 every 2 business days.
- **No sends Saturday or Sunday.** Window Mon–Fri 10:00–17:30 **Asia/Kolkata**, jittered spacing.
- **One follow-up 3 calendar days after the first email**, rolled forward to the next business day
  inside the window. `max_followups` default 1. Any reply, bounce or opt-out cancels follow-ups in
  the same transaction that records it.
- `approval_mode = review_all` for first emails and every reply. Design for, but never enable,
  `auto_send_if_confidence`.
- `MEETING_BOOKED` is set **only** by a human click or a booking webhook — never inferred from a
  positive reply.
- Plain text only. No tracking pixels, no link rewriting, no HTML body.

## Conventions

- Conventional Commits. Branches `fix/<ID>-<slug>` or `feat/<ID>-<slug>`.
- **Write the failing test first**, especially for a bug fix. Fakes only: `FakeLLM`, `aiosmtpd` for
  SMTP, GreenMail for E2E, `respx` for HTTP. **Never the network in a test.**
- Every PR: issue IDs fixed, what/why, tests added, risk, rollback — and update `docs/PROGRESS.md`
  and `docs/DECISIONS.md` in the same PR.
- Timestamps are `timestamptz` in UTC. IST conversion happens only in `domain/scheduling`.
- Every table carries `org_id`; every repository call takes an `OrgContext` (ADR-004).
- An ADR in `docs/DECISIONS.md` is required to deviate from `ARCHITECTURE_TARGET.md`.

## Do not auto-merge (open the PR, label `needs-human`, summarise, move on)

Any change that alters or drops existing DB data, touches auth/security, changes sending behaviour
or defaults (`SEND_MODE`, caps, window, footer), or changes deploy config.

## Gotchas in this repo

- The working directory path contains a space (`Desktop\email automation`). **Quote every path.**
- The legacy flat modules are importable only from the repo root — there is no `pyproject.toml` yet.
- `config/config.py` loads `.env.example` as defaults *and copies its placeholders into the
  environment*, so the prototype's `validate()` passes with fake credentials (B3). The replacement
  must reject `your_`-prefixed values at startup.
- `DRY_RUN_EMAILS=0` means *dry run*, not "send real", because `bool("0")` is `True` (B1). This flag
  is being deleted in favour of `SEND_MODE`.
- A reply reading "not positive" currently classifies as **positive** (B17, substring matching) and,
  pre-patch, auto-sends (B4).
- `sample_leads.xlsx` is deleted; use `tests/fixtures/leads_example.csv`. The owner's real test
  sheet lives at `local/test_leads.xlsx` — gitignored, and **never read it**.
- The repo is **private**. Do not make it public.
