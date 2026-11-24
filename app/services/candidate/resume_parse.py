"""Resume upload validation and text extraction (S19.1, ADR-068).

**Offline only.** No network calls here. The service:

1. Validates the upload (filename safety, size cap, content type).
2. Extracts text from PDF or DOCX bytes.
3. Converts raw text to unconfirmed fact proposals.

Nothing confirmed exits this module. ``text_to_proposals`` marks every proposal
``confirmed=False``; the consuming route stores them that way. The existing
``profile.py`` already enforces that only confirmed facts reach a prompt.
"""

from __future__ import annotations

import hashlib
import re

#: 2 MB, following ADR-016 (capped bytes in Postgres).
MAX_BYTES = 2 * 1024 * 1024

ALLOWED_CONTENT_TYPES: set[str] = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------


def validate_upload(
    *,
    filename: str,
    content_type: str,
    size: int,
) -> None:
    """Raise ValueError if the upload should be rejected before touching Postgres.

    Args:
        filename: The original filename from the upload.
        content_type: The MIME type declared by the client.
        size: File size in bytes.
    """
    _check_filename(filename)
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError(
            f"Unsupported file type: {content_type!r}. Only PDF and DOCX files are accepted."
        )
    if size > MAX_BYTES:
        raise ValueError(
            f"File too large: {size} bytes exceeds the {MAX_BYTES // 1024 // 1024} MB limit."
        )


def _check_filename(filename: str) -> None:
    """Reject filenames that could cause path traversal or other injection."""
    if not filename:
        raise ValueError("filename must not be empty")
    # Null bytes
    if "\x00" in filename:
        raise ValueError("filename must not contain null bytes")
    # Path separators — no directory components allowed
    if "/" in filename or "\\" in filename:
        raise ValueError("filename must not contain path separators")
    # Leading dots that could become hidden files or traversal (.., ../foo)
    # Strip the extension first; the stem must not start with ..
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    if stem.startswith(".."):
        raise ValueError("filename looks like a path traversal attempt")


# ---------------------------------------------------------------------------
# Text extraction — offline, no network
# ---------------------------------------------------------------------------


def extract_text_from_pdf(data: bytes) -> str:
    """Extract plain text from PDF bytes using pypdf."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


def extract_text_from_docx(data: bytes) -> str:
    """Extract plain text from DOCX bytes using python-docx."""
    import io

    from docx import Document

    doc = Document(io.BytesIO(data))
    lines: list[str] = []
    for para in doc.paragraphs:
        if para.text.strip():
            lines.append(para.text)
    # Also walk tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    lines.append(cell.text)
    return "\n".join(lines)


def extract_text(data: bytes, content_type: str) -> str:
    """Dispatch to the right extractor based on content type."""
    if content_type == "application/pdf":
        return extract_text_from_pdf(data)
    if content_type == ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
        return extract_text_from_docx(data)
    raise ValueError(f"Cannot extract text from {content_type!r}")


# ---------------------------------------------------------------------------
# Proposal generation — rule-based, no model
# ---------------------------------------------------------------------------

#: Patterns that look like dated experience lines (e.g. "Engineer at Acme 2020-2023").
_EXPERIENCE_RE = re.compile(
    r"(.+?)\s+at\s+(.+?)\s+(\d{4})\s*[-]\s*(\d{4}|present)",
    re.IGNORECASE,
)

#: Simple skill tokeniser — comma or semicolon separated, or lines starting with bullet markers.
_SKILL_LINE_RE = re.compile(r"^[\-•*·▪▸]\s*(.+)$", re.MULTILINE)


def text_to_proposals(text: str) -> list[dict[str, object]]:
    """Convert extracted resume text to unconfirmed fact proposals.

    Rules-based, never LLM. Output is ``confirmed=False``; the caller stores
    these as proposals and the owner confirms them in the UI.

    Returns an empty list for blank input.
    """
    text = text.strip()
    if not text:
        return []

    proposals: list[dict[str, object]] = []

    for match in _EXPERIENCE_RE.finditer(text):
        role = match.group(1).strip()
        employer = match.group(2).strip()
        start = match.group(3)
        end = match.group(4)
        proposals.append(
            {
                "kind": "experience",
                "statement": f"{role} at {employer} ({start}-{end})",
                "confirmed": False,
                "source": "resume",
            }
        )

    for match in _SKILL_LINE_RE.finditer(text):
        skill = match.group(1).strip()
        if skill:
            proposals.append(
                {
                    "kind": "skill",
                    "statement": skill,
                    "confirmed": False,
                    "source": "resume",
                }
            )

    return proposals


# ---------------------------------------------------------------------------
# Sha-256 for deduplication
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
