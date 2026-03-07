# How to Run the Automation (Step by Step)

## Prerequisites

- Python 3.10+ installed
- `.env` file in the project root with: `GROQ_API_KEY`, `SMTP_EMAIL`, `SMTP_PASSWORD` (and optionally `IMAP_EMAIL`, `IMAP_PASSWORD` for reply monitoring)

---

## Step 1: Open terminal in project folder

```powershell
cd c:\Users\Mi\Downloads\automation
```

---

## Step 2: Create a virtual environment (since you deleted venvs)

```powershell
python -m venv venv
```

---

## Step 3: Activate the virtual environment

**Windows (PowerShell):**
```powershell
.\venv\Scripts\Activate.ps1
```

**Windows (CMD):**
```cmd
venv\Scripts\activate.bat
```

You should see `(venv)` at the start of your prompt.

---

## Step 4: Install dependencies

```powershell
pip install -r requirements.txt
```

Wait until all packages install without errors.

---

## Step 5: Verify config and app

Check that `.env` is loaded and required keys are set:

```powershell
python -c "from config.config import config; config.validate(); print('Config OK')"
```

Then check that the app starts and the database works:

```powershell
python main.py --status
```

You should see the "FUNDING OUTREACH AUTOMATION SYSTEM" banner and either "No leads in database" or a leads table.

---

## Step 6: Run the full automation (first time: without reply monitor)

Use your leads file (e.g. `sample_leads.xlsx`) and skip the background reply monitor so the run finishes and you can see the full workflow:

```powershell
python main.py sample_leads.xlsx --no-monitor
```

What happens:

1. Loads leads from the Excel file (columns: Name, Email, Company)
2. Stores them in SQLite
3. Runs LangGraph workflows in parallel for each lead:
   - Generate insights (LLM)
   - Human approve insights (or auto if `AUTO_APPROVE=1` in `.env`)
   - Generate email draft (LLM)
   - Human approve email (or auto)
   - Send email via SMTP
4. Prints a summary and the final leads table

To avoid typing "y/n" for each lead, set in `.env`:

```
AUTO_APPROVE=1
```

To test without actually sending emails, set in `.env`:

```
DRY_RUN_EMAILS=1
```

---

## Step 7: Run with reply monitoring (optional)

To also run the reply monitor and decision engine (they run until you press Ctrl+C):

```powershell
python main.py sample_leads.xlsx
```

Do not use `--no-monitor`. The app will:

1. Do everything from Step 6
2. Then start two background loops:
   - Reply monitor: checks inbox every N minutes, sets status to REPLIED when a lead replies
   - Decision engine: classifies replies (positive / question / no_interest) and takes action

Press **Ctrl+C** to stop the background workers when you are done.

---

## Step 8: Run only background workers (later)

If you already ran the outreach and want to start only reply monitoring and decision engine:

```powershell
python main.py --workers-only
```

Press **Ctrl+C** to stop.

---

## Quick reference

| Command | What it does |
|---------|----------------|
| `python main.py --status` | Show leads in DB and exit |
| `python main.py sample_leads.xlsx --no-monitor` | Full workflow, no reply monitor (good first test) |
| `python main.py sample_leads.xlsx` | Full workflow + reply monitor (runs until Ctrl+C) |
| `python main.py --workers-only` | Only reply monitor + decision engine |

---

## If something fails

- **"Missing required environment variables"**  
  Check that `.env` exists in `c:\Users\Mi\Downloads\automation` and contains `GROQ_API_KEY`, `SMTP_EMAIL`, `SMTP_PASSWORD` (no quotes, no spaces around `=`).

- **"ModuleNotFoundError"**  
  Activate the venv and run `pip install -r requirements.txt` again.

- **"File not found"**  
  Use the correct path to your Excel file, e.g. `python main.py sample_leads.xlsx`.

- **SMTP / login errors**  
  For Gmail, use an App Password, not your normal password. Turn on 2FA and create an App Password in Google Account settings.

---

## Troubleshooting (from your run)

**1. Groq "Rate limit reached" (429, tokens per day)**  
Your Groq free tier has a daily token limit (e.g. 100,000 TPD). Once you hit it, LLM calls fail until the limit resets (next day).  
- **Options:** Wait until the limit resets, or upgrade at https://console.groq.com/settings/billing.  
- To use fewer tokens per run, set in `.env`: `MAX_CONCURRENT_LEADS=1` and run with a single lead.

**2. SMTP "semaphore timeout period has expired" (WinError 121)**  
The connection to Gmail SMTP timed out. Common causes: firewall, antivirus, or network blocking port 587.  
- **Try port 465 with SSL.** In `.env` add or set:
  ```
  SMTP_PORT=465
  SMTP_USE_TLS=1
  ```
- **Increase timeout.** In `.env` add: `SMTP_TIMEOUT=90` (or higher).  
- **Check network:** Try from another network (e.g. mobile hotspot) or disable VPN.  
- **Gmail:** Ensure you use an App Password (not your normal Gmail password).
