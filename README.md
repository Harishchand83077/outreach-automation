# outreach-automation

A cold-outreach system for a small software agency: import a sheet of founders, research each
company from real sources, draft a grounded email, get human approval, send on a safe schedule,
then handle replies, bounces, opt-outs and follow-ups until every lead reaches a terminal state.
Each *campaign* carries its own sender persona and goal, so the tool is general purpose.

**Status: a prototype being rewritten into production.** Nothing here is production-ready yet, and
by default nothing can send mail at all. Start with the documents below rather than the code.

| Document | What it is |
|---|---|
| [`docs/ARCHITECTURE_TARGET.md`](docs/ARCHITECTURE_TARGET.md) | The design we are building. **Authoritative** — deviating needs an ADR. |
| [`docs/REPO_ANALYSIS.md`](docs/REPO_ANALYSIS.md) | What the code actually does today, module by module, and the top risks. |
| [`docs/ISSUES.md`](docs/ISSUES.md) | 51 tracked issues, each with a verdict and file:line evidence. |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | Phase status, what is done, and what is next. |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | ADRs: where and why we deviate from the target design. |
| [`docs/SETUP.md`](docs/SETUP.md) | How to install and run it locally. |
| [`docs/QUESTIONS.md`](docs/QUESTIONS.md) | Open questions, each with a default so nothing blocks. |
| [`docs/SETUP_CHECKLIST.md`](docs/SETUP_CHECKLIST.md) | Accounts and keys to provision, by phase. |
| [`CLAUDE.md`](CLAUDE.md) | Hard rules, conventions and gotchas. Read this first when working on the repo. |

## Sending is off by default

`SEND_MODE` is the only switch that decides whether mail can leave, and it is enforced in exactly
one function (`email_utils.is_send_allowed`).

| Value | Behaviour |
|---|---|
| `disabled` | **Default.** Nothing is sent, to anyone. Drafts are still generated and saved. |
| `allowlist` | Sends only to addresses in `SEND_ALLOWLIST`. Everything else ends `SKIPPED_NOT_ALLOWLISTED`. |
| `live` | Sends normally. Set by the operator only, deliberately, in production. |

Startup fails if a required value is missing or still holds a placeholder, so the system cannot run
half-configured and report itself healthy.

## Quick start

Requires **Python 3.12** (not 3.13+), Node 20+, and [uv](https://docs.astral.sh/uv/).

```bash
uv python install 3.12
uv venv --python 3.12 .venv
uv sync --all-groups

cp env.example .env          # then fill in GROQ_API_KEY, SMTP_EMAIL, SMTP_PASSWORD
.venv/Scripts/python -m app.scripts.check_env    # prints OK/MISSING per NAME, never a value
```

Then run the legacy prototype — which still works, but is being replaced module by module:

```bash
.venv/Scripts/python main.py leads.xlsx --no-monitor
.venv/Scripts/python -m uvicorn api_server:app --reload --port 8000
cd frontend && npm install && npm run dev
```

Full detail, including the checks below, is in [`docs/SETUP.md`](docs/SETUP.md).

## Checks

```bash
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest
cd frontend && npm run build
```

No test touches the network: SMTP is faked with `aiosmtpd`, the LLM is monkeypatched, and every
address in a fixture is `@example.com`.

## Where the code is going

The target is a FastAPI application in `app/` — an explicit, DB-backed state machine plus a
Postgres job queue, replacing the current LangGraph workflow, with a separate worker process whose
`SendLoop` is the only code that touches SMTP. The `app/` tree exists and each package's
`__init__.py` states what belongs in it; Phase 1 begins filling them. The flat modules at the
repository root are the prototype and will be deleted as `app/` supersedes each one.
