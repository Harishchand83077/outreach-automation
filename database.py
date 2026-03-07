"""
db/database.py — Async SQLite database layer for leads management.

Uses aiosqlite for non-blocking DB operations.
All operations are safe for concurrent async access.
"""

import asyncio
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict, Any

from config.config import config
from logger import get_logger

logger = get_logger("database")

# Global lock to prevent concurrent schema creation races
_init_lock = asyncio.Lock()


async def init_db() -> None:
    """
    Initialize the SQLite database and create the leads table if it doesn't exist.
    Called once at startup.
    """
    async with _init_lock:
        async with aiosqlite.connect(config.DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    company TEXT NOT NULL,
                    insights TEXT,
                    email_draft TEXT,
                    status TEXT DEFAULT 'INIT',
                    reply TEXT,
                    classification TEXT,
                    meeting_booked INTEGER DEFAULT 0,
                    pre_meeting_doc TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status)
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email)
            """)
            await db.commit()
    logger.info("Database initialized at: %s", config.DB_PATH)


async def upsert_lead(name: str, email: str, company: str) -> int:
    """
    Insert a new lead or ignore if email already exists.
    Returns the row id.
    """
    async with aiosqlite.connect(config.DB_PATH) as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO leads (name, email, company, status)
            VALUES (?, ?, ?, 'INIT')
        """, (name, email, company))
        await db.commit()

        # Fetch the id whether inserted or pre-existing
        row = await db.execute("SELECT id FROM leads WHERE email = ?", (email,))
        result = await row.fetchone()
        return result[0] if result else -1


async def update_lead(email: str, **fields) -> None:
    """
    Update arbitrary fields for a lead identified by email.
    Automatically sets updated_at.

    Example:
        await update_lead("user@x.com", status="EMAIL_SENT", insights="...")
    """
    if not fields:
        return

    fields["updated_at"] = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields.keys())
    values = list(fields.values()) + [email]

    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(
            f"UPDATE leads SET {set_clause} WHERE email = ?",
            values
        )
        await db.commit()
    logger.debug("Updated lead [%s]: %s", email, list(fields.keys()))


async def get_lead(email: str) -> Optional[Dict[str, Any]]:
    """Fetch a single lead by email. Returns dict or None."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM leads WHERE email = ?", (email,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_leads_by_status(status: str) -> List[Dict[str, Any]]:
    """Fetch all leads with a given status."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM leads WHERE status = ? ORDER BY updated_at ASC",
            (status,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_all_leads() -> List[Dict[str, Any]]:
    """Fetch all leads."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM leads ORDER BY id ASC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def print_leads_summary() -> None:
    """Print a summary table of all leads and their statuses."""
    leads = await get_all_leads()
    if not leads:
        logger.info("No leads in database.")
        return

    from rich.table import Table
    from logger import console

    table = Table(title="Leads Status Summary", show_lines=True)
    table.add_column("ID", style="cyan", width=4)
    table.add_column("Name", style="white")
    table.add_column("Email", style="blue")
    table.add_column("Company", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Classification", style="yellow")
    table.add_column("Meeting", style="red")

    for lead in leads:
        table.add_row(
            str(lead.get("id", "")),
            lead.get("name", ""),
            lead.get("email", ""),
            lead.get("company", ""),
            lead.get("status", ""),
            lead.get("classification", "") or "-",
            "Y" if lead.get("meeting_booked") else "N",
        )

    console.print(table)
