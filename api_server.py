"""
api_server.py — FastAPI backend for the Funding Outreach Automation dashboard.

Run with: uvicorn api_server:app --reload --host 0.0.0.0 --port 8000

When APPROVAL_MODE=api (set in .env for dashboard use), human-in-the-loop
approvals are done via these endpoints instead of CLI.
"""

import asyncio
import csv
import io
import os
import time
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

# Ensure project root is on path and load env
_root = Path(__file__).resolve().parent
os.chdir(_root)

from config.config import config
from database import (
    init_db,
    get_all_leads,
    get_lead,
    upsert_lead,
    get_leads_by_status,
    get_stats,
    reset_failed_leads_to_init,
)
from excel_loader import load_leads_from_excel
from runner import run_all_leads
from approval_store import (
    get_pending_async,
    approve_insights,
    reject_insights,
    approve_email,
    reject_email,
    approve_question_reply,
)
from reply_monitor import reply_monitor_loop
from decision_engine import decision_engine_loop
from logger import get_logger

logger = get_logger("api_server")

# Simple in-memory rate limit: IP -> list of request timestamps (last 60s)
_rate_limit: dict = {}
_RATE_LIMIT_REQUESTS = 100
_RATE_LIMIT_WINDOW = 60.0


def _rate_limit_check(ip: str) -> bool:
    now = time.time()
    if ip not in _rate_limit:
        _rate_limit[ip] = []
    times = _rate_limit[ip]
    times[:] = [t for t in times if now - t < _RATE_LIMIT_WINDOW]
    if len(times) >= _RATE_LIMIT_REQUESTS:
        return False
    times.append(now)
    return True


app = FastAPI(
    title="Funding Outreach Automation API",
    description="Backend for the outreach dashboard: upload leads, run workflows, approve/reject via API.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_and_rate_limit(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    if request.url.path.startswith("/api/") and request.method == "POST":
        if not _rate_limit_check(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again in a minute."},
            )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# Background task for current run + workers
_run_task: Optional[asyncio.Task] = None
_workers_task: Optional[asyncio.Task] = None


# ─── Pydantic models ───────────────────────────────────────────────────────

class ApproveRejectBody(BaseModel):
    feedback: Optional[str] = None


class ApproveInsightsBody(BaseModel):
    edited_insights: Optional[str] = None


class ApproveEmailBody(BaseModel):
    edited_email_draft: Optional[str] = None


class ApproveQuestionReplyBody(BaseModel):
    response_text: Optional[str] = None


# ─── Startup ───────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    await init_db()
    # Force API approval mode and human approval when using the server (no auto-approve)
    config.APPROVAL_MODE = "api"
    os.environ["AUTO_APPROVE"] = "0"
    # Start reply monitor + decision engine so replies are detected when using dashboard
    asyncio.create_task(_workers_loop())
    logger.info("API server started. APPROVAL_MODE=api, AUTO_APPROVE=0, background workers started.")


# ─── API routes ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    """Root path: point to API health and docs."""
    return {
        "service": "Funding Outreach Automation API",
        "status": "running",
        "health": "/api/health",
        "docs": "/docs",
        "message": "Use the dashboard (Vercel) or call /api/leads, /api/run, etc.",
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/leads")
async def list_leads():
    """Return all leads with current status (for dashboard table)."""
    leads = await get_all_leads()
    # Convert to JSON-serializable dicts (Row may have non-JSON types)
    out = []
    for r in leads:
        d = dict(r)
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        out.append(d)
    return {"leads": out}


@app.get("/api/leads/pending")
async def list_pending():
    """Return leads waiting for insights or email approval (for Approve/Reject buttons)."""
    pending = await get_pending_async()
    return {"pending": pending}


@app.post("/api/upload")
async def upload_leads(file: UploadFile = File(...)):
    """Upload Excel file; parse and store leads. Returns stored lead count."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "File must be Excel (.xlsx or .xls)")
    contents = await file.read()
    path = _root / "uploads" / (file.filename or "upload.xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    try:
        leads = load_leads_from_excel(str(path))
    except Exception as e:
        raise HTTPException(400, f"Failed to parse Excel: {e}")
    for lead in leads:
        await upsert_lead(
            name=lead["name"],
            email=lead["email"],
            company=lead["company"],
        )
    return {"uploaded": file.filename, "count": len(leads), "leads": leads}


@app.post("/api/run")
async def start_run(use_existing: bool = True):
    """
    Start the outreach workflow in the background.
    If use_existing=True (default), run for all INIT leads in DB.
    Otherwise you must have just uploaded leads (they are INIT).
    """
    global _run_task
    if _run_task and not _run_task.done():
        return {"status": "already_running", "message": "A run is already in progress."}
    leads = await get_leads_by_status("INIT")
    if not leads:
        # Run for all leads that are not yet EMAIL_SENT (e.g. re-run failed)
        all_leads = await get_all_leads()
        leads = [dict(r) for r in all_leads if dict(r).get("status") == "INIT"]
    if not leads:
        return {"status": "no_leads", "message": "No leads in INIT status. Upload an Excel file first."}
    _run_task = asyncio.create_task(_run_outreach([dict(r) for r in leads]))
    return {"status": "started", "lead_count": len(leads)}


async def _run_outreach(leads: List[dict]):
    """Run workflows for given leads (called in background)."""
    try:
        results = await run_all_leads(leads)
        logger.info("Background run finished: %d leads", len(results))
    except Exception as e:
        logger.error("Background run failed: %s", e, exc_info=True)


@app.post("/api/leads/{email}/approve-insights")
async def api_approve_insights(email: str, body: ApproveInsightsBody = None):
    """Approve insights for this lead (optionally with edited content). Unblocks workflow."""
    edited = (body and body.edited_insights) if body else None
    ok = approve_insights(email, edited_insights=edited)
    if not ok:
        raise HTTPException(404, "No pending insights approval for this lead")
    return {"status": "approved"}


@app.post("/api/leads/{email}/reject-insights")
async def api_reject_insights(email: str, body: ApproveRejectBody = None):
    """Reject insights and optionally provide feedback for regeneration."""
    ok = reject_insights(email, feedback=(body and body.feedback) or "")
    if not ok:
        raise HTTPException(404, "No pending insights approval for this lead")
    return {"status": "rejected"}


@app.post("/api/leads/{email}/approve-email")
async def api_approve_email(email: str, body: ApproveEmailBody = None):
    """Approve email draft for this lead (optionally with edited content). Unblocks workflow."""
    edited = (body and body.edited_email_draft) if body else None
    ok = approve_email(email, edited_email_draft=edited)
    if not ok:
        raise HTTPException(404, "No pending email approval for this lead")
    return {"status": "approved"}


@app.post("/api/leads/{email}/reject-email")
async def api_reject_email(email: str, body: ApproveRejectBody = None):
    """Reject email draft and optionally provide feedback."""
    ok = reject_email(email, feedback=(body and body.feedback) or "")
    if not ok:
        raise HTTPException(404, "No pending email approval for this lead")
    return {"status": "rejected"}


@app.post("/api/leads/{email}/approve-question-reply")
async def api_approve_question_reply(email: str, body: ApproveQuestionReplyBody = None):
    """Submit human response for a QUESTION-classified reply. Unblocks decision engine."""
    response_text = (body and body.response_text) if body else None
    ok = approve_question_reply(email, response_text=response_text or "")
    if not ok:
        raise HTTPException(404, "No pending question-reply for this lead")
    return {"status": "sent"}


@app.get("/api/run/status")
async def run_status():
    """Whether a run is currently in progress."""
    running = _run_task is not None and not _run_task.done()
    return {"running": running}


@app.get("/api/stats")
async def stats():
    """Dashboard stats: total leads, emails sent, meetings, last activity."""
    s = await get_stats()
    return s


@app.get("/api/leads/export/csv")
async def export_leads_csv():
    """Export all leads as CSV for backup or use in other tools."""
    leads = await get_all_leads()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Name", "Email", "Company", "Status", "Classification", "Meeting", "Insights", "Email draft"])
    for r in leads:
        row = dict(r)
        writer.writerow([
            row.get("name", ""),
            row.get("email", ""),
            row.get("company", ""),
            row.get("status", ""),
            row.get("classification") or "",
            "Yes" if row.get("meeting_booked") else "",
            (row.get("insights") or "")[:500],
            (row.get("email_draft") or "")[:500],
        ])
    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads_export.csv"},
    )


@app.post("/api/run/retry-failed")
async def retry_failed():
    """Reset all EMAIL_FAILED and ERROR leads to INIT so they can be re-run."""
    global _run_task
    if _run_task and not _run_task.done():
        return {"status": "already_running", "message": "A run is in progress. Wait for it to finish."}
    count = await reset_failed_leads_to_init()
    return {"status": "ok", "reset_count": count, "message": f"{count} lead(s) reset to INIT. Click Run outreach to retry."}


# ─── Optional: start background workers when server runs ─────────────────────

def start_workers():
    """Start reply monitor and decision engine in the background (optional)."""
    global _workers_task
    if _workers_task is None or _workers_task.done():
        _workers_task = asyncio.create_task(_workers_loop())
        logger.info("Background workers (reply monitor + decision engine) started.")


async def _workers_loop():
    try:
        await asyncio.gather(reply_monitor_loop(), decision_engine_loop())
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("Workers error: %s", e)


# Background workers are started in startup() when using the API server.
