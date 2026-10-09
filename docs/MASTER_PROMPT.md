# MASTER PROMPT — paste this into Claude Code (VS Code) at the repo root

> Save this file as `docs/MASTER_PROMPT.md`, with `ISSUES.md` at `docs/ISSUES.md` and `ARCHITECTURE_TARGET.md` at `docs/ARCHITECTURE_TARGET.md`. Start Claude Code in **plan mode** and paste only the short kickoff prompt (`KICKOFF_PROMPT.md`), which points here.

---

You are the lead engineer for **outreach-automation**. Your mission: turn this prototype into a **production-grade, multi-user cold-outreach system** for a small Indian software agency, and fix **every issue** in `docs/ISSUES.md`. You work autonomously, issue by issue, and only stop to ask me when the rules in §11 say so.

**Do §00 (bootstrap & permissions) and then §0 (repo analysis) first, before any planning or coding.** `docs/ISSUES.md` is my audit of the repo; you must verify it, not trust it.

## 00. BOOTSTRAP & PERMISSIONS (do this before §0, ask me once, batched)

Before touching the repo, check the environment and ask me for what you need **in one batched message** (use AskUserQuestion). Do not ask piecemeal later for things you could have foreseen.

1. **Tooling check (read-only):** `python --version` (need 3.12), `node --version`, `git --version`, `gh auth status`, `docker --version`, `uv --version` (optional). Report what is missing and the exact install command; do not install system packages without asking.
2. **Python env:** create `.venv` with Python 3.12, activate it, upgrade pip, and from now on use only `.venv/bin/python -m ...` (Windows: `.venv\\Scripts\\python`). Dependencies go in `pyproject.toml` with a lock (`uv.lock` or `requirements.lock`); the old `requirements.txt` is replaced in the Phase 0/1 PRs, not before. `.venv/` stays gitignored.
3. **Permissions proposal:** show me the exact `.claude/settings.json` you want to commit (project-level, no secrets) and wait for my OK:
   - `allow`: `Bash(git status|diff|log|add|commit|checkout|switch|branch|fetch|pull *)`, `Bash(python -m pytest *)`, `Bash(python -m ruff *)`, `Bash(python -m mypy *)`, `Bash(pip install *)` / `Bash(uv *)` (inside `.venv` only), `Bash(npm run *)`, `Bash(npm install *)`, `Bash(gh pr view|checks|status *)`, `Bash(docker compose config|build *)`.
   - `ask` (always prompt me, even in auto mode): `Bash(git push *)`, `Bash(gh pr create *)`, `Bash(gh pr merge *)`, `Bash(alembic upgrade *)`, anything that touches a real database URL, anything run with `SEND_MODE=live`.
   - `deny`: `Read(./.env)`, `Read(./.env.*)` (except `.env.example`), `Bash(rm -rf *)`, `Bash(git push --force*)`, `Bash(git reset --hard origin/*)`, `Bash(curl * | sh)`, `Bash(* SEND_MODE=live *)`.
   Tell me which of these you think are too broad or too narrow.
4. **Accounts/approvals you need from me** (ask only for the ones needed for the *next* phase, and tell me when each will be needed): `gh auth login` done? branch protection on `main` enabled? repo is private? Supabase project URL + pooler `DATABASE_URL` placed in my local `.env` (you never read it; you verify by running `python -m app.scripts.check_env`, which prints only OK/MISSING per variable name)? `GROQ_API_KEY` set? Mailbox provider (SMTP/IMAP host)? Hosting target decided? Answers to Q1–Q7 in `docs/ARCHITECTURE_TARGET.md`.
5. **Standing approvals I give now (confirm each in the same message):** (a) you may create branches and commit locally without asking; (b) you may push branches and open PRs only after the `ask` prompt, and I will approve those quickly; (c) you may squash-merge a PR yourself only if CI is green and it is **not** labelled `needs-human` (§9); (d) you may install Python/npm dev dependencies in the project; (e) you may NOT create cloud resources, spend money, or send email.
6. Write the result of this step to `docs/SETUP.md` (a "Local setup" section: venv commands, how to run tests, how to start the app) so I can reproduce it.

## 0. PHASE −1: UNDERSTAND THE REPO FIRST (read-only, no code changes)

Before planning anything, build your own understanding of what exists:

1. Read **every** tracked file (`git ls-files`): all Python, the frontend, config, docs, sample data. Do not rely on the README; it is partly wrong. Also read `docs/ARCHITECTURE_TARGET.md` and tell me, in your report, anything in it you think is wrong, risky or over-engineered.
2. Write `docs/REPO_ANALYSIS.md` containing: (a) what the system does today, in plain words; (b) a module map with each module's responsibility and who imports whom; (c) the end-to-end data flow and the lead statuses **actually implemented** (vs. what the docs claim); (d) every env var / config value actually read, and external services touched; (e) how to run it locally; (f) dead, duplicated or misleading code and docs; (g) tests/CI that exist (expect none); (h) anything in the files or git history that looks like a secret or personal data.
3. **Verify `docs/ISSUES.md` item by item.** Mark each ID `Confirmed`, `Partly`, or `Not reproducible`, with evidence (file:line, or a tiny repro script that touches no network and no SMTP). Add `NEW-n` entries for anything my audit missed. Update `docs/ISSUES.md`.
4. Establish a baseline: create a venv, install deps, `python -m compileall`, import every module, and record what works or fails. No real keys, no network calls to LLM/SMTP/IMAP. Tag the commit `baseline-pre-rewrite`.
5. Report to me in ≤ 40 lines (what you understood, what you got wrong in my audit, top risks), then continue to the plan. Do not start coding until I approve the plan.

## 1. HARD RULES (never break these)

1. **Never send a real email** while developing or testing. Introduce `SEND_MODE` = `disabled` (default; log only) | `allowlist` (send only to addresses in `SEND_ALLOWLIST`) | `live`. Only I set `live`, only in the production environment. CI and your local runs never hold real SMTP/IMAP credentials.
2. **Never print, log, commit, or paste secrets.** Config comes from environment via `pydantic-settings`. `.env` is gitignored and you do not read it. `.env.example` has placeholders only, and startup **fails** if a required value is empty or still a placeholder.
3. **No direct pushes to `main`. No force-push.** Everything goes through a PR (see §9).
4. **No scraping LinkedIn.** The `linkedin_url` column is stored and shown as a reference only. Never fetch it.
5. **Do not spend money or create paid resources.** If something truly needs paying for, stop and ask (§11).
6. **Do not invent facts about the sender.** The email generator may only use facts from the campaign persona and from cited research evidence (§6).
7. LLM output is **untrusted data**. It can never trigger a send by itself, and replies from leads are untrusted input (prompt-injection safe, see §7).
8. **Respect provider terms.** One account per provider per organisation. Never build rotation across multiple accounts or keys created to get around a provider's free-tier quota (Groq's Acceptable Use Policy explicitly forbids registering multiple accounts or orchestrating usage across multiple organizations to circumvent limits; treat every other provider the same unless its terms clearly allow it). Extra headroom comes from **multiple models and multiple providers** (see the LLM router in §3), not from multiple accounts.
9. **No real recipients in tests.** When `SEND_MODE != live`, the sender must refuse any recipient not in `SEND_ALLOWLIST`; enforce this in exactly one place and cover it with a test. CI and committed fixtures use `@example.com` addresses only.

## 2. PRODUCT (decisions are locked; log any change in `docs/DECISIONS.md`)

**Who/why:** An agency (my team + freelancers) emails **founders of Indian startups** to book a meeting and win **paid project work** (fixed-scope projects, paid on completion, delivered with deadlines). The tool is **general purpose**: each *campaign* defines its own sender persona, goal, and rules. Funding outreach is just one possible campaign.

**Users:** one sending identity today (a business-domain mailbox over **SMTP/IMAP**), but **built multi-user / multi-org from day one** (`org_id` everywhere).

**Flow per lead:** import sheet → enrich+research → draft → **human review** → scheduled send → wait → reply handling / follow-up.

**Volume & rules:** cap **100 emails/day** per mailbox (with a configurable warm-up ramp, default start 20/day, +10 every 2 business days up to the cap). **No sends on Saturday/Sunday.** Send window in **Asia/Kolkata** (default Mon–Fri 10:00–17:30 IST), randomised spacing (jitter) between sends. **A sequence of up to 3 follow-ups** (default: one, 3 days after the first email; each step has its own `delay_days`, counted in calendar days from the previous send, then rolled to the next business day inside the window; hard ceiling 3 follow-ups). Follow-ups go in the same thread. Stop all follow-ups the moment any reply, bounce, or opt-out is detected.

**Approval:** default `approval_mode = review_all` for first emails and for every reply. Bulk review UI. Design for a future `auto_send_if_confidence >= X` mode but do not enable it.

**Audience:** India only for now. B2B business contacts. Every email has an honest sender identity and an opt-out. (Not legal advice; keep a `docs/COMPLIANCE.md` of what the system does so a lawyer can review it later.)

### Apollo-style campaign builder (my reference product is Apollo's email sequences; we copy the workflow and improve it with research and grounded AI)
The user experience must be: **upload sheet → map columns → see dedupe preview → write the brief and/or template, add links, choose sequence steps/delays → choose start time, days and window → start; then watch every lead's status in a table.** Details are in `docs/ARCHITECTURE_TARGET.md` §5.3, §5.4, §7A, §15, §26. Key rules:
1. **Column mapping** with saved presets; extra columns kept in `extra`; never crash (B7).
2. **Dedupe**: skip (never duplicate) rows already in the sheet, in this campaign, suppressed, or contacted in another campaign within 90 days; show counts and let me download the skipped list. Re-uploading an updated sheet adds only the new rows.
3. **Content modes per step**: `template` (merge fields only), `template_ai` (my text plus AI-written slots), `ai_personalized` (whole email from persona + brief + cited research). Defaults: step 1 `ai_personalized`, follow-ups `template_ai`. Missing merge fields block the draft; a raw `{{placeholder}}` must never reach a sent email.
4. **Links yes, tracking no.** No pixels or click-tracking redirects. Attachments are optional, off by default (one PDF ≤ 2 MB, deliverability warning).
5. **Status table**: every lead shows its state, step ("Follow-up 2 of 3"), first/last sent, next send time (IST), replied-at; filters; bulk actions; export CSV/XLSX with statuses.
6. **Schedule**: start now / start at / draft; days, window, daily cap, per-step window optional. Pre-decided follow-up timing is enforced by the scheduler, not by the UI.
7. **Replies**: AI classifies and drafts a response; **I approve before it is sent** (no auto-send of replies; a per-label autopilot is a future ADR).
8. Capacity honesty: with 3 follow-ups each lead can cost 4 sends, so 100/day is about 25–35 new leads/day. Show this estimate in the wizard.

### Campaign input
- **Sheet (.xlsx/.csv)**. Required: `full_name`, `email`, `company_name`, `company_url`. Recommended: `job_title`, `linkedin_url`. Optional: `notes` (anything I know about them), plus any extra columns, which are kept in `extra` JSON and passed to research/drafting. Accept header aliases (e.g. `Name`, `E-mail`, `Website`, `Company`) and show a **column-mapping + validation preview** before import. Never crash on extra columns (B7). Validate syntax + MX record, drop role addresses (`info@`, `sales@`…) with a warning, dedupe by email, flag duplicate domains.
- **Persona (per campaign):** sender name, role, agency name, website, what the agency does (services), ideal customer, offer / value proposition, **proof points / case studies (the only claims the LLM may state as ours)**, goal (e.g. "book a 20-min call"), CTA + booking link, tone, language, words/phrases to avoid, signature, max length, follow-up angle.

### Test data & rehearsal (my sheet)

I will provide my own test sheet at `local/test_leads.xlsx` (gitignored; the repo is **private**). It may lack some required columns. Its rows pair **my own email addresses** with **real public company websites** of random companies. Therefore:
1. The importer must cope with missing or renamed columns through the mapping preview (e.g. no `company_url` → ask me; never guess a company site from a free-mail domain).
2. Research will fetch those public sites (robots-aware, low volume): that is fine.
3. **Mail goes only to my addresses.** Test runs use `SEND_MODE=allowlist` with `SEND_ALLOWLIST` = my addresses. Rows whose email is not allow-listed are imported but end as `SKIPPED (not in allowlist)` at send time, never sent.
4. Commit only synthetic fixtures (`tests/fixtures/leads_example.csv` with `@example.com`). Delete the committed `sample_leads.xlsx` (it contains real personal emails) and tell me whether git history should be scrubbed.
5. I will extend the sheet as we go. At each phase gate, tell me exactly which columns/rows you want added to exercise the next phase.

## 3. TARGET ARCHITECTURE (decisions I made for you; challenge in an ADR if you have a better one)

**The full, detailed design is `docs/ARCHITECTURE_TARGET.md` and it is authoritative** (data model, state machines, queue, send engine, reply engine, LLM router, research, security, deployment, failure modes). The bullets below are only the headline. If you disagree with something there, write an ADR proposal in your plan; do not silently deviate.


- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 async + Alembic, `asyncpg`, Pydantic v2. Restructure into a package `app/` (api, core, db, services, workers). **Remove LangGraph/LangChain**: replace with an explicit, DB-backed **state machine + job queue** (simpler, resumable, testable). Use the official `groq` SDK directly with JSON-schema/structured outputs.
- **DB:** **Postgres on Supabase (free)**. Job queue = a `jobs` table claimed with `SELECT … FOR UPDATE SKIP LOCKED`. No Redis/Celery. Use a **dedicated Supabase project for this app** (never share my other project's). The free DB limit is 500 MB, so keep rows lean: store research as compact JSON, never whole HTML, truncate raw fetched text to what is needed and purge it after 30 days, add a storage-usage readout (`/api/admin/storage`) and warn at 70%. Connect through the Supabase **session pooler** string (IPv4-compatible; the direct connection is IPv6-only on the free plan), keep pools small, put all tables in a private schema `app`, enable RLS deny-all, and have me turn the Data API off.
- **Auth:** **Supabase Auth** (email+password, optional Google). FastAPI verifies the Supabase JWT on every route except `/health`. Roles `owner|admin|member` via a `members` table. All queries scoped by `org_id`. Restricted CORS (env list). Per-user rate limits.
- **Frontend:** keep **React + Vite** (add TypeScript, React Router, TanStack Query; use `@supabase/supabase-js` for login). Host on **Cloudflare Pages** (Vercel Hobby forbids commercial use).
- **Hosting (backend + worker):** **Docker** images + `docker compose` (api, worker, Caddy for HTTPS). **Render, Upstash and Vercel are not available for this app** (my other project already uses my free capacity there, and Render's free tier also blocks outbound SMTP and sleeps after 15 min). Do not add config for them. Target an **always-on VM**, in this order: Oracle Cloud Always Free (test outbound port 587 from the VM first), Google Cloud Always Free e2-micro (port 587 open, port 25 blocked; we only use 587), or a ~US$5/month VPS. The same images must run on any of them. Do not deploy anything yourself; produce `docs/DEPLOY.md` + scripts and ask me to run the deploy.
- **Worker:** a separate process (`python -m app.worker`) running: scheduler/sender, IMAP poller, research/draft job runners. All idempotent and crash-safe. Also expose `POST /internal/tick` (secret header) running one cycle, so any external trigger could drive it.
- **LLM = provider-agnostic router, free-first.** One `LLMRouter` over OpenAI-compatible chat endpoints (the `openai` SDK with a per-provider `base_url`, or `httpx`). Providers/models come from an ordered **`LLM_CHAIN`** config (env JSON or YAML, never hard-coded). Each entry: `provider, base_url, model, api_key_env, tier (fast|primary), allow_pii (bool), daily_token_cap, tokens_per_min_cap, rpm_cap, cost_per_mtok (0 for free)`. Entries whose key is missing are skipped. Implement entries for: **Groq** (several models: its limits are published *per model*, so different models give separate headroom inside one account), **Cerebras**, **OpenRouter** (`:free` models), and optionally **Google AI Studio**.
  - **Behaviour:** pick the first healthy entry for the task tier. On 429 / quota / 5xx / timeout / invalid JSON: put that entry in cool-down (use `Retry-After` and reset headers; else exponential backoff; if a daily quota is hit, cool down until its reset) and fall through to the next. Never retry non-retryable 4xx (auth, bad request): disable that entry and alert. Validate structured outputs with Pydantic; a schema failure counts as a failure for that model. Track per-entry usage in `llm_usage` (entry, task, tokens in/out, latency, outcome, lead_id) with per-lead and per-day token budgets. Use fast models for extraction/classification and primary models for drafting.
  - **When every free entry is exhausted:** pause LLM jobs and show "LLM capacity exhausted, resets at …" in the UI. Never silently degrade to a poor model or skip validation. An optional last `paid` entry exists but is **disabled by default** (`PAID_LLM_ENABLED=true` and `PAID_LLM_DAILY_USD_CAP` required).
  - **Data sensitivity:** some free tiers (Google AI Studio outside EEA/UK; Mistral's Experiment tier) may train on prompts. Mark those `allow_pii=false`: they may receive only **public website text with no lead name, email or reply content**. Drafting, classification and anything containing lead PII or reply text goes only to `allow_pii=true` providers.
  - **Quality gate:** a golden set in `tests/golden/` (about 20 cases, including prompt-injection replies) and a `make eval-llm` script so I can compare models before adding them to the chain.
  - **Capacity honesty:** measure and display tokens per lead (research + draft + validation, expected ~8–10k). Use token frugality: truncate pages, cache research per domain for 30 days, keep prompts small. Report real numbers so I can decide when to pay.
- **Research tools:** (a) direct fetch of `company_url` + a few same-domain pages (about/product/blog/careers) with `httpx` + `trafilatura`, robots-aware, polite per-host delay, short timeouts, size caps, ≤ ~5 pages per lead, and **SSRF protection** (block private/loopback/link-local IPs, limit redirects); (b) web search behind a `SearchProvider` interface, with **Tavily** as the first implementation (free 1,000 credits/month; basic search = 1 credit): at most 1 search per lead (recent news/person), cached per domain, tracked in `search_usage` against `TAVILY_MONTHLY_CREDIT_BUDGET`, with credits-left shown in the UI. One Tavily key per org (rule 8: no multi-account rotation). If the key is absent or the budget is exhausted, degrade gracefully to (a) only. More search providers can be added later; ask me first.

## 4. DATA MODEL (minimum; refine in an ADR)

`orgs`, `members`, `mailboxes` (org, smtp/imap settings, secret refs, daily_cap, warmup state), `campaigns` (org, name, goal, `persona jsonb`, `brief`, `template jsonb`, `sequence jsonb` (≤ 4 steps), `settings jsonb`: tz, window, caps, dedupe_window_days, approval_mode, `start_at`, status), `mapping_presets`, `assets` (links/attachments), `leads` (org, campaign, normalized email, name, company, company_url, linkedin_url, title, notes, `extra jsonb`, `state`, `unique(campaign_id, email)`), `research` (lead, sources jsonb with url+fetched_at, structured findings jsonb, confidence, model, tokens), `drafts` (lead, kind `initial|followup|reply`, subject, body, evidence refs, status, version, created_by), `messages` (lead, direction, RFC `message_id`, `in_reply_to`, `references`, subject, body_text, `sent_at`/`received_at`, `imap_uid`, idempotency_key, send_status), `jobs`, `events` (**append-only audit log of every state change, send, approval, who/when**), `suppressions` (org, email or domain, reason), `imap_cursor` (mailbox, uidvalidity, last_uid), `llm_usage`.

## 5. STATE MACHINE (single entry point, row-locked, event written in the same transaction)

`NEW → ENRICHING → RESEARCHED → DRAFTED → AWAITING_APPROVAL → APPROVED → SENT → { FOLLOWUP_SENT | REPLIED ⇄ IN_CONVERSATION }` (the authoritative state list and transition table is `docs/ARCHITECTURE_TARGET.md` §6; `SCHEDULED` is not a stored state, `IN_CONVERSATION` and `NO_RESPONSE` were added).
Terminals: `MEETING_BOOKED` (only via a human action or a booking webhook, **never inferred from a positive reply**), `NOT_INTERESTED`, `OPTED_OUT`, `BOUNCED`, `NO_RESPONSE`, `FAILED`, `SKIPPED` (`FAILED`/`SKIPPED` are human-recoverable). Pausing is a flag, not a state.
Any process can crash and restart; leads resume from DB state. No approval or send lives only in memory (B9). Approvals have no semaphore slot (B8): concurrency limits apply to LLM/HTTP work only.

## 6. RESEARCH, GROUNDING, DRAFT QUALITY

- Research output is structured JSON: what the company does, stage/size signals, product/tech needs relevant to the persona's offer, 1–3 **hooks**, each with `source_url` and a short supporting snippet, plus `confidence`. No evidence → low confidence → the draft must be honestly generic, or the lead goes to `AWAITING_APPROVAL` with a "thin research" flag (or `SKIPPED` if I configure that).
- Draft rules: plain text, ≤ persona max (default 120 words), one CTA, subject ≤ 6 words, no fake "Re:", no false familiarity, no unverifiable numbers. A concrete claim may only come from evidence or `persona.proof_points`. Add a **validation pass** (deterministic checks + a cheap LLM check) that rejects drafts containing claims not traceable to evidence/persona, spam-trigger phrases, or markdown. If generation or validation fails, **the lead goes to `FAILED`/needs-review; error text is never used as email content** (B2).
- The review UI shows the draft next to its evidence (links) so I can verify quickly. Edits are saved as a new draft version; the exact final subject/body that was sent is stored in `messages`.
- Cap regeneration attempts (default 3) (B15). Subject parsing uses structured output, not string-splitting (B13). Escape HTML (B14).

## 7. SENDING & REPLY RULES

**Sending:** outbox pattern with an idempotency key; a message is claimed, sent, then marked sent; a crash between send and mark must never produce a duplicate (B12). Before every send re-check: lead state, suppression list, `SEND_MODE`, weekday/window, daily cap, warm-up cap. Footer: sender identity, and an opt-out line; add the `List-Unsubscribe` (mailto) header, a proper `Message-ID`, `Date`, and `Reply-To`. Throttle with jitter. Stop and alert (not retry-spam) on repeated SMTP auth/limit errors. Daily counters use IST day boundaries.

**Reply monitoring:** IMAP with **`BODY.PEEK[]`** (never mark mail read), track `UIDVALIDITY` + last UID per mailbox (B10). Match replies to leads by `In-Reply-To`/`References` against our stored Message-IDs, falling back to sender + normalised subject (B11). Detect and handle **bounces** (`multipart/report`, `MAILER-DAEMON`: mark `BOUNCED` + suppress), **auto-replies** (`Auto-Submitted`, out-of-office headers/patterns: do not treat as a real reply; reschedule follow-up sensibly), and **opt-outs** (keyword rules *and* LLM class: suppress immediately, no reply email). Strip quoted text before classifying. Decode charsets correctly; handle HTML-only mail.

**Classification** (structured JSON): `interested | question | objection | not_now | not_interested | opt_out | out_of_office | bounce | wrong_person_referral | other`. The reply text is wrapped as untrusted data, and the model's only power is to choose a label and propose a draft reply for **human approval**. A label never sends anything (C5, B4). Persist classification before acting (B17). Replies the human approves are sent threaded (`In-Reply-To`, `References`), through the same outbox. Multi-turn conversations must work: monitoring continues until a terminal state (A6).

## 8. UI (must exist by the end)

Login; campaign list; **new-campaign wizard** (persona form → sheet upload with mapping/validation preview → settings → start); campaign dashboard (funnel counts, today's cap usage, token/credit usage, per-lead table with full timeline from `events`); **review queue** (bulk approve / edit / reject-with-feedback, evidence side-panel); **replies queue** (classified replies with proposed response drafts, approve/edit/send, "mark meeting booked" button); suppression list; mailbox settings with a "test connection" and "send test to myself" button; **global and per-campaign pause (kill switch)**; no endpoint returns whole-table payloads (pagination, no 2.5 s polling of everything: C7, E8).

## 9. HOW TO WORK, PER ISSUE

Use `gh` (I'll have run `gh auth login`). For each issue or tightly related group of issues:

1. `git checkout main && git pull`, then branch `fix/<ID>-<slug>` or `feat/<ID>-<slug>`.
2. Write the failing test first (regression test for bugs). Use fakes: FakeLLM, an in-process fake SMTP (`aiosmtpd`), a fake IMAP server/fixture, recorded HTTP (`respx`). Never the network.
3. Implement. Keep PRs small and reviewable.
4. Run locally: `ruff`, `mypy`, `pytest`, frontend lint/build. All green.
5. Self-review `git diff`; remove dead code and the old modules you've superseded.
6. Commit with Conventional Commits; open a PR with `gh pr create` including: issue IDs fixed, what/why, tests added, risk, rollback. Update `docs/PROGRESS.md` and `docs/DECISIONS.md` in the same PR.
7. Wait for CI (`gh pr checks --watch`). If red, fix; do not merge red.
8. When green, `gh pr merge --squash --delete-branch` (use `--auto` if branch protection is on). Pull `main` and continue.
9. **Do NOT auto-merge** (open the PR, label `needs-human`, summarise in chat, continue with independent work) when the change: alters or drops existing DB data, touches auth/security, changes sending behaviour or defaults (`SEND_MODE`, caps, window, footer), or changes deploy config.

**Foundations you build first (Phase 0):** GitHub Actions CI (lint, types, tests with a Postgres service container, `alembic upgrade head` on an empty DB, frontend build), `pre-commit` incl. `gitleaks`, Dependabot, pinned dependencies with a lockfile, `.env.example` v2, `pydantic-settings` validation (B3, B6, B18), `SEND_MODE` (B1), and a **small safety patch to the legacy code first**: fix B1, B2, B3, C2 and disable the auto-send in `handle_positive` (B4) so the current code can't hurt anyone while you rebuild.

**Order of work** (issue IDs from `docs/ISSUES.md`; each must end `Done` / `Superseded-by-rewrite (with test proving the behaviour)` / `Won't fix (reason)`):
- **P0 Foundations & safety patch:** B1, B2, B3, B4, B6, B18, C2, E6, F1–F3 (docs updated at the end too)
- **P1 Data layer & state machine:** B9, B16, E2, E3, E4, B12 (outbox)
- **P2 Auth & multi-tenancy:** C1, C3, C4, C7
- **P3 Campaigns, persona, import:** A4, A5, B7, D4, (persona/column design in §2), column-mapping presets, dedupe policy, status table skeleton and CSV/XLSX export
- **P3b Sequence builder:** sequence jsonb + validation (≤ 3 follow-ups), content modes, merge-field validation, links/optional attachment, sample-lead preview, schedule (start now/at)
- **P4 LLM router + research engine:** E5, A1, A2 (+ SSRF safety, caching, budgets, multi-provider failover, PII routing)
- **P5 Drafting, grounding, review queue:** A3, B13, B14, B15, B8
- **P6 Sending engine & compliance:** D1, D3, D5, E1 (worker architecture), B12
- **P7 Reply engine & follow-ups:** A6, A7, A8, B5, B10, B11, B17, C5, D2
- **P8 UI completion:** E8 and §8
- **P9 Ops:** Docker/compose/Caddy, `docs/DEPLOY.md`, structured JSON logs + run/lead IDs (E7), `/health` + `/ready`, error alerting hook (Sentry optional), nightly DB export script, `docs/RUNBOOK.md`, C6/C8/D6 mitigations (data retention + per-lead deletion endpoint, remove personal emails from `sample_leads.xlsx`)
- **P10 Rehearsal:** end-to-end test + dry-run rehearsal checklist (below)

## 10. MEMORY (so you survive context resets and new sessions)

Create and keep current: `CLAUDE.md` (≤200 lines: commands, architecture summary, hard rules from §1, conventions, gotchas), `docs/PROGRESS.md` (every issue ID: status, PR link, notes; a "next up" list; a **handoff note** you rewrite at the end of every session), `docs/DECISIONS.md` (ADRs: date, decision, why, alternatives), `docs/QUESTIONS.md` (open questions for me), `docs/SETUP.md`, `docs/RUNBOOK.md`, `docs/COMPLIANCE.md`, `docs/REPO_ANALYSIS.md`. **At the start of every session read `CLAUDE.md` and `docs/PROGRESS.md` first** and continue from "next up". Prefer a fresh session per phase.

## 11. WHEN TO ASK ME vs DECIDE

**Decide yourself** (log in `docs/DECISIONS.md`): library choices, schema details, naming, UI layout, test strategy, anything inside this spec.
**Ask me** (batch questions, state your recommendation + the default you'll use if I don't answer, and keep working on independent items): spending money or creating paid resources; any credential/account/DNS step only I can do; irreversible or data-destructive actions; compliance-sensitive copy (opt-out wording, footer); a product trade-off that changes behaviour I specified; deploys; flipping `SEND_MODE=live`. Never ask me to paste secrets into chat; tell me which env var to set and where.

## 12. DEFINITION OF DONE (go-live gate)

An automated E2E test (all fakes) proves: 10-lead sheet imports with a mapping preview → research (cached, token-budgeted) → grounded drafts → bulk approval → sends **only** inside Mon–Fri IST window, respecting the daily cap + warm-up and jitter → inbound reply (fake IMAP) matched by `In-Reply-To` → classified → human-approved threaded response → **follow-up cancelled on reply**; a no-reply lead gets exactly the configured follow-ups (test with 1 and with 3) at their delays, rolled off weekends and holidays, in the same thread; re-uploading the same sheet with 2 new rows adds only those 2; a duplicate of a suppressed or recently contacted address is skipped with the right reason; a `template` step with a missing merge field is blocked, never sent; opt-out and bounce suppress forever; **killing the worker mid-send never double-sends**; restarting mid-pipeline resumes; auth rejects unauthenticated and cross-org access; prompt-injection reply cannot trigger a send; the LLM router fails over on 429, never routes PII to `allow_pii=false` entries, and pauses cleanly when every entry is exhausted; sending to a non-allow-listed recipient is impossible while `SEND_MODE != live`. Then produce `docs/GO_LIVE.md`: a rehearsal using `SEND_MODE=allowlist` with my own two test mailboxes, DNS (SPF/DKIM/DMARC) checks, warm-up plan, and rollback. Every issue in `docs/ISSUES.md` is `Done`, `Superseded-by-rewrite` (with test), or `Won't fix` (with reason).

## FIRST ACTIONS

1. Execute §0 (read-only): read every file, write `docs/REPO_ANALYSIS.md`, verify and update `docs/ISSUES.md`, record the baseline.
2. In plan mode, present: your ≤ 40-line understanding report, the ADR list, the phase plan with PR breakdown, and any questions batched per §11.
3. After I approve, create `CLAUDE.md` + `docs/*`, then start Phase 0 with the safety patch PR.
