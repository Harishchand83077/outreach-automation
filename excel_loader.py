"""Load leads from an Excel or CSV sheet, in memory.

Parsing is driven by bytes, never by a path the client chose. The prototype's API wrote the
client-supplied filename straight into `uploads/<filename>`, so `../../x.xlsx` escaped the
directory (issue C2). Nothing here touches the filesystem on the upload path, so there is no
path to traverse: `filename` is used only to pick a parser.

Header aliasing (`E-mail`, `Company Name`, `First Name`…) and the per-row validation preview
belong to the import phase and are deliberately not attempted here; see ISSUES.md B7 and
ARCHITECTURE_TARGET.md §7A.
"""

import csv
import io
from pathlib import Path
from typing import Any, Dict, List

from openpyxl import load_workbook

from logger import get_logger

logger = get_logger("excel_loader")

#: Upload limits, per ARCHITECTURE_TARGET.md §7A.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5_000

REQUIRED_COLUMNS = ("Name", "Email", "Company")


def _normalize_header(h: str) -> str:
    return (h or "").strip().title()


def _row_to_lead(values: Dict[str, Any]) -> Dict[str, str] | None:
    """Return a lead dict, or None if the row is unusable."""
    name = str(values.get("Name") or "").strip()
    email = str(values.get("Email") or "").strip()
    company = str(values.get("Company") or "").strip()
    if not name or not email or not company:
        return None
    if "@" not in email:
        return None
    return {"name": name, "email": email.lower(), "company": company}


def _parse_csv(data: bytes) -> List[Dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("The file is empty — no header row found.")

    normalized = [_normalize_header(h) for h in header]
    _require_columns(normalized)

    leads: List[Dict[str, str]] = []
    for count, row in enumerate(reader, start=1):
        if count > MAX_ROWS:
            raise ValueError(f"Too many rows: the limit is {MAX_ROWS:,} rows per upload.")
        # zip() stops at the shorter sequence, so a row with a stray extra cell is tolerated
        # instead of raising KeyError and killing the whole upload (B7).
        lead = _row_to_lead(dict(zip(normalized, row)))
        if lead:
            leads.append(lead)
    return leads


def _parse_xlsx(data: bytes) -> List[Dict[str, str]]:
    wb = load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active

    rows = ws.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration:
        raise ValueError("The file is empty — no header row found.")
    if not header:
        raise ValueError("Missing header row.")

    normalized = [_normalize_header(str(h)) if h is not None else "" for h in header]
    _require_columns(normalized)

    leads: List[Dict[str, str]] = []
    for count, row in enumerate(rows, start=1):
        if count > MAX_ROWS:
            raise ValueError(f"Too many rows: the limit is {MAX_ROWS:,} rows per upload.")
        lead = _row_to_lead(dict(zip(normalized, row)))
        if lead:
            leads.append(lead)
    return leads


def _require_columns(normalized: List[str]) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in normalized]
    if missing:
        raise ValueError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Found: {', '.join(h for h in normalized if h) or '(none)'}"
        )


def load_leads_from_bytes(data: bytes, filename: str) -> List[Dict[str, Any]]:
    """Parse an uploaded sheet from memory.

    `filename` selects the parser by extension and is otherwise ignored — in particular it is
    never joined onto a path.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"File is too large: the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        leads = _parse_csv(data)
    elif suffix == ".xlsx":
        leads = _parse_xlsx(data)
    elif suffix == ".xls":
        raise ValueError(
            "Legacy .xls files are not supported — openpyxl cannot read them. "
            "Re-save the sheet as .xlsx or .csv."
        )
    else:
        raise ValueError(f"Unsupported file type '{suffix or filename}'. Use .xlsx or .csv.")

    if not leads:
        raise ValueError("No valid leads found in the file.")

    logger.info("Parsed %d valid lead(s) from an uploaded %s file", len(leads), suffix)
    return leads


def load_leads_from_excel(filepath: str) -> List[Dict[str, Any]]:
    """Read a sheet from disk (CLI use). Delegates to the in-memory parser."""
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    return load_leads_from_bytes(p.read_bytes(), p.name)
