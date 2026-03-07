# Funding Outreach Automation System

Production-ready automated funding outreach using LangGraph, async Python, and parallel execution.

## Requirements

- Python 3.10+
- Excel file with columns: Name, Email, Company

## Setup

1. Clone or extract the project.

2. Create a virtual environment (recommended):

   ```
   python -m venv venv
   venv\Scripts\activate   # Windows
   source venv/bin/activate # Linux/macOS
   ```

3. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

4. Configure environment:

   - Copy `.env.example` to `.env`
   - Set `GROQ_API_KEY` (Groq API key for LLM)
   - Set `SMTP_EMAIL` and `SMTP_PASSWORD` (Gmail app password for sending)
   - Set `IMAP_EMAIL` and `IMAP_PASSWORD` (for reply monitoring)
   - Set `CALENDAR_LINK` (e.g. Calendly link)
   - Optional: `AUTO_APPROVE=1` to skip CLI approval prompts; `DRY_RUN_EMAILS=1` to simulate sends

## How to Run

**Full run (load leads, run workflows, then start background workers):**

```
python main.py leads.xlsx
```

**Skip reply monitoring (run workflows only, then exit):**

```
python main.py leads.xlsx --no-monitor
```

**Show lead status only:**

```
python main.py --status
```

**Run only background workers (reply monitor + decision engine):**

```
python main.py --workers-only
```

## Sample Excel Format

| Name | Email | Company |
|------|--------|---------|
| Jane Doe | jane@example.com | Acme Inc |
| John Smith | john@corp.com | Tech Co |

Save as `.xlsx` (or `.csv` with same column names).

## Project Structure

- `main.py` – Entry point, argument parsing, phase orchestration
- `runner.py` – Parallel execution (asyncio.gather + semaphore), runs one LangGraph workflow per lead
- `graph.py` – LangGraph workflow definition (generate_insights -> human_validate_insights -> generate_email -> human_validate_email -> send_email -> end_node)
- `state.py` – LeadState TypedDict
- `nodes.py` – Graph node implementations (LLM, CLI, SMTP)
- `database.py` – Async SQLite (aiosqlite) for leads table
- `excel_loader.py` – Load leads from Excel/CSV (openpyxl)
- `reply_monitor.py` – Background worker: IMAP inbox check, update DB on reply
- `decision_engine.py` – Background worker: classify reply (LLM), positive/question/no_interest actions
- `llm_client.py` – Groq/LangChain LLM with retry
- `email_utils.py` – SMTP send and IMAP check
- `logger.py` – Rich logging
- `config/config.py` – Config from .env

## Architecture

See `ARCHITECTURE.md` for flow diagrams and design rules (parallel execution, workflow ends after send_email, reply handling in separate workers).

## Optional: Remove Old Venvs

If you have multiple virtualenv folders (e.g. `.venv311`, `venv`) and want a clean tree, delete the ones you do not use. The app only needs one active venv with `pip install -r requirements.txt`. `.gitignore` already excludes `.venv/`, `.venv311/`, `venv/`.
