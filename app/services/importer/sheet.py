"""Reading a spreadsheet from memory.

Parsed from bytes, never from a path the client chose — there is no file on disk, so there is no
path to traverse (issue C2). `filename` only selects a parser.

`defusedxml` guards the XLSX path: an .xlsx is a zip of XML, so a hostile upload can attack the
parser itself (billion laughs, external entity expansion) long before we look at its contents.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

#: Upload limits, per ARCHITECTURE_TARGET §7A.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5_000
#: A sheet with hundreds of columns is a mistake, not a lead list.
MAX_COLUMNS = 100


class SheetError(ValueError):
    """The upload cannot be read at all. Distinct from a row that fails validation."""


@dataclass
class ParsedSheet:
    headers: list[str]
    #: Data rows only. Every row is padded to the header width, so a short or long row is data
    #: to validate rather than an IndexError (issue B7).
    rows: list[list[object]]
    #: Rows present in the file beyond MAX_ROWS, reported rather than silently truncated.
    truncated: int = 0

    @property
    def row_count(self) -> int:
        return len(self.rows)


def parse(data: bytes, filename: str) -> ParsedSheet:
    """Parse an uploaded sheet. Raises `SheetError` if the file itself is unusable."""
    if not data:
        raise SheetError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise SheetError(f"File is too large: the limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        return _parse_csv(data)
    if suffix == ".xlsx":
        return _parse_xlsx(data)
    if suffix == ".xls":
        raise SheetError(
            "Legacy .xls files are not supported — openpyxl cannot read them. "
            "Re-save the sheet as .xlsx or .csv."
        )
    raise SheetError(f"Unsupported file type {suffix or filename!r}. Use .xlsx or .csv.")


def _finish(headers: list[str], rows: list[list[object]], truncated: int) -> ParsedSheet:
    if not headers or not any(str(h or "").strip() for h in headers):
        raise SheetError("No header row found — the first row must name the columns.")
    if len(headers) > MAX_COLUMNS:
        raise SheetError(f"Too many columns: the limit is {MAX_COLUMNS}.")

    width = len(headers)
    # Pad and trim to the header width. A stray extra cell used to raise KeyError and kill the
    # whole upload; now it is simply data the validator can report on (issue B7).
    padded = [(row + [None] * width)[:width] for row in rows]
    return ParsedSheet(headers=headers, rows=padded, truncated=truncated)


def _parse_csv(data: bytes) -> ParsedSheet:
    # utf-8-sig: a BOM from Excel's "CSV UTF-8" export would otherwise corrupt the first header.
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        headers = [str(h) for h in next(reader)]
    except StopIteration:
        raise SheetError("No header row found — the first row must name the columns.") from None

    rows: list[list[object]] = []
    truncated = 0
    for row in reader:
        if not any(str(cell).strip() for cell in row):
            continue  # a blank separator line, not a lead
        if len(rows) >= MAX_ROWS:
            truncated += 1
            continue
        rows.append(list(row))
    return _finish(headers, rows, truncated)


def _parse_xlsx(data: bytes) -> ParsedSheet:
    try:
        workbook = load_workbook(filename=io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise SheetError(f"Could not read the .xlsx file: {exc}") from exc

    sheet = workbook.active
    if sheet is None:
        raise SheetError("The workbook has no sheets.")

    iterator = sheet.iter_rows(values_only=True)
    try:
        header_row = next(iterator)
    except StopIteration:
        raise SheetError("No header row found — the first row must name the columns.") from None

    headers = [("" if h is None else str(h)) for h in header_row]

    rows: list[list[object]] = []
    truncated = 0
    for row in iterator:
        if not any(cell is not None and str(cell).strip() for cell in row):
            continue
        if len(rows) >= MAX_ROWS:
            truncated += 1
            continue
        rows.append(list(row))

    workbook.close()
    return _finish(headers, rows, truncated)
