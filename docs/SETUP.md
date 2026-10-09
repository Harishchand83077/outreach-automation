# Local setup

Reproducible from a clean checkout. Windows paths are shown because that is the development machine;
the project directory contains a space, so **quote every path**.

## Prerequisites

| Tool | Required | This machine |
|---|---|---|
| Python | **3.12** (not 3.13+) | 3.14.3 is the system default — do **not** use it; 3.12.12 installed via `uv` |
| uv | any recent | 0.10.6 |
| Node | 20+ | 24.13.1 |
| git | 2.4+ | 2.44.0 |
| Docker | for Phase 9 and the Postgres/GreenMail test containers | 29.8.0 |
| gh | for PRs | authenticated as `Harishchand83077` |

## Python environment

The project is pinned to Python 3.12. Never run the system interpreter.

```bash
uv python install 3.12            # once
uv venv --python 3.12 .venv       # add --clear only if you intend to destroy the existing env
uv sync                           # from pyproject.toml + uv.lock (Phase 0 onward)
```

From then on, always call the interpreter explicitly — do not rely on an activated shell:

```bash
.venv/Scripts/python -m pytest          # Windows
.venv/bin/python -m pytest              # macOS/Linux
```

`.venv/` is gitignored. On this machine a 3.12.1 venv already existed with the prototype's
dependencies installed; it was kept as found rather than recreated.

> Until the Phase 0 CI PR lands, dependencies still come from the unpinned `requirements.txt`:
> `.venv/Scripts/python -m pip install -r requirements.txt`. That file is replaced by
> `pyproject.toml` + `uv.lock` in that PR.

## Configuration

```bash
cp .env.example .env     # then edit .env with real values
```

`.env` is gitignored and **Claude never reads it** — that is enforced by a `deny` rule in
`.claude/settings.json`. To check whether configuration is complete, run the env checker, which
prints only `OK`/`MISSING` against each variable *name* and never a value:

```bash
.venv/Scripts/python -m app.scripts.check_env      # Phase 0 onward
```

Startup **fails** if a required value is empty or is still an `.env.example` placeholder. This is
deliberate: the prototype accepted `your_groq_api_key_here` as a real key and reported itself
correctly configured (issue B3).

### Safety switch

`SEND_MODE` controls whether mail can leave:

| Value | Behaviour |
|---|---|
| `disabled` | **Default.** Nothing is sent; the send is logged and the draft is kept. |
| `allowlist` | Sends only to addresses in `SEND_ALLOWLIST`. Any other recipient ends `SKIPPED`. |
| `live` | Sends normally. **Set by the owner only, only in production.** |

Your own test sheet belongs at `local/test_leads.xlsx` — gitignored, never read by Claude, and never
committed. Committed fixtures use `@example.com` addresses only.

## Running

```bash
# API (Phase 1 onward; bound to localhost until auth lands in Phase 7)
.venv/Scripts/python -m uvicorn app.api.main:app --reload --host 127.0.0.1 --port 8000

# worker
.venv/Scripts/python -m app.worker

# frontend
cd frontend && npm install && npm run dev
```

Legacy prototype, still runnable through Phase 0 but not to be extended:

```bash
.venv/Scripts/python main.py leads.xlsx --no-monitor   # note: --no-monitor, not --skip-monitor
.venv/Scripts/python main.py --status
.venv/Scripts/python -m uvicorn api_server:app --reload --port 8000
```

## Checks

```bash
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .
.venv/Scripts/python -m mypy app
.venv/Scripts/python -m pytest                  # unit + integration
.venv/Scripts/python -m pytest -m e2e            # needs Docker (Postgres + GreenMail)
cd frontend && npm run lint && npm run build
```

No test touches the network. SMTP is faked with `aiosmtpd`, IMAP and end-to-end mail with GreenMail,
HTTP with `respx`, and the LLM with `FakeLLM`.

## Baseline

The pre-rewrite prototype is tagged `baseline-pre-rewrite` (commit `990fa9f`). Measured there:
`compileall` clean, all 15 modules import, no tests exist. See `REPO_ANALYSIS.md`.
