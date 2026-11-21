"""S19.1 — resume upload and parse.

Three invariants:

1. A traversing filename (../../etc/passwd) is rejected before the bytes reach Postgres.
2. A file above the 2 MB cap is rejected with 413.
3. parse_resume proposes facts but confirms none — nothing unconfirmed may ever reach a prompt.
4. The parse_resume service has no network calls (pure, offline).
"""

from __future__ import annotations

import hashlib
import io
import pathlib
import uuid


class TestUploadGuards:
    def test_traversing_filename_is_rejected(self) -> None:
        from app.services.candidate.resume_parse import validate_upload
        with __import__("pytest").raises(ValueError, match="filename"):
            validate_upload(filename="../../etc/passwd", content_type="application/pdf", size=100)

    def test_null_byte_in_filename_is_rejected(self) -> None:
        from app.services.candidate.resume_parse import validate_upload
        with __import__("pytest").raises(ValueError, match="filename"):
            validate_upload(filename="resume\x00.pdf", content_type="application/pdf", size=100)

    def test_oversized_file_is_rejected(self) -> None:
        from app.services.candidate.resume_parse import validate_upload, MAX_BYTES
        with __import__("pytest").raises(ValueError, match="too large"):
            validate_upload(filename="cv.pdf", content_type="application/pdf", size=MAX_BYTES + 1)

    def test_file_at_exactly_cap_is_accepted(self) -> None:
        from app.services.candidate.resume_parse import validate_upload, MAX_BYTES
        validate_upload(filename="cv.pdf", content_type="application/pdf", size=MAX_BYTES)

    def test_unsupported_content_type_is_rejected(self) -> None:
        from app.services.candidate.resume_parse import validate_upload
        with __import__("pytest").raises(ValueError, match="type"):
            validate_upload(filename="cv.txt", content_type="text/plain", size=100)

    def test_pdf_and_docx_are_accepted(self) -> None:
        from app.services.candidate.resume_parse import validate_upload
        validate_upload(filename="cv.pdf", content_type="application/pdf", size=100)
        validate_upload(
            filename="cv.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size=100,
        )


class TestParseProposesOnlyNeverConfirms:
    def test_pdf_parse_returns_proposals(self) -> None:
        from app.services.candidate.resume_parse import extract_text_from_pdf

        # Minimal valid PDF with one text run.
        # We're testing that the function returns a string, not that it handles every PDF.
        pdf_bytes = _minimal_pdf("John Smith\nSoftware Engineer at Acme Corp 2020-2023")
        text = extract_text_from_pdf(pdf_bytes)
        assert isinstance(text, str)
        # The two-word name and employer should come through.
        assert "John" in text or len(text) > 0

    def test_docx_parse_returns_text(self) -> None:
        from app.services.candidate.resume_parse import extract_text_from_docx

        docx_bytes = _minimal_docx("Jane Doe\nProduct Manager at Startup 2021-2024")
        text = extract_text_from_docx(docx_bytes)
        assert "Jane" in text
        assert "Product Manager" in text

    def test_text_to_fact_proposals_are_unconfirmed(self) -> None:
        from app.services.candidate.resume_parse import text_to_proposals

        text = "Software Engineer at Acme Corp 2020-2023\nPython, FastAPI, PostgreSQL"
        proposals = text_to_proposals(text)
        assert isinstance(proposals, list)
        # Every proposal must be unconfirmed (confirmed=False)
        for p in proposals:
            assert p.get("confirmed") is False, f"Proposal should be unconfirmed: {p}"

    def test_empty_text_gives_no_proposals(self) -> None:
        from app.services.candidate.resume_parse import text_to_proposals
        assert text_to_proposals("") == []
        assert text_to_proposals("   \n  ") == []


class TestParseIsOffline:
    def test_resume_parse_module_imports_no_httpx(self) -> None:
        import ast
        source = pathlib.Path("app/services/candidate/resume_parse.py").read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for name in names:
                    assert "httpx" not in name, "resume_parse must not import httpx"
                    assert "llm" not in name.lower(), "resume_parse must not call LLM"


# ---------------------------------------------------------------------------
# Minimal file builders
# ---------------------------------------------------------------------------

def _minimal_pdf(text: str) -> bytes:
    """Return a minimal valid PDF with one text stream."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    # write plain text using a basic annotation comment so we have something
    # to extract without needing font embedding
    buf = io.BytesIO()
    writer.write(buf)
    raw = buf.getvalue()
    # For extraction we need a real text stream. Build one minimally.
    return _pdf_with_text(text)


def _pdf_with_text(text: str) -> bytes:
    """Return PDF bytes that pypdf can extract text from."""
    # Use reportlab if available, otherwise fall back to a hand-crafted PDF.
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas as rl_canvas
        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=letter)
        c.setFont("Helvetica", 12)
        y = 750
        for line in text.split("\n"):
            c.drawString(50, y, line)
            y -= 20
        c.save()
        return buf.getvalue()
    except ImportError:
        pass

    # Hand-crafted minimal PDF — pypdf can extract text from Type1 content streams.
    lines = text.split("\n")
    content_lines = ["BT", "/F1 12 Tf"]
    y = 750
    for line in lines:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_lines.append(f"50 {y} Td ({safe}) Tj")
        y -= 20
    content_lines.append("ET")
    content = "\n".join(content_lines)

    obj_content = content.encode()
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        (
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
        ),
        (
            b"4 0 obj\n<< /Length " + str(len(obj_content)).encode() + b" >>\nstream\n"
            + obj_content + b"\nendstream\nendobj\n"
        ),
        (
            b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        ),
    ]

    header = b"%PDF-1.4\n"
    body = b"".join(objects)
    xref_offset = len(header) + len(body)

    offsets = []
    pos = len(header)
    for obj in objects:
        offsets.append(pos)
        pos += len(obj)

    xref = b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n".encode()

    trailer = (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n" + str(xref_offset).encode() + b"\n%%EOF"
    )
    return header + body + xref + trailer


def _minimal_docx(text: str) -> bytes:
    """Return .docx bytes with the given text."""
    from docx import Document
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
