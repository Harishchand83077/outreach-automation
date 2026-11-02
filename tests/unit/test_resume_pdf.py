"""The hand-written PDF writer (F20, ADR-043).

Two reasons these tests are worth more than they look. **The escaping one is a real defect, not a
hypothetical**: `(`, `)` and `\\` are structural in a PDF string literal, and a resume says
"(remote)" often enough that an unescaped writer produces a file that opens as garbage -- which
reads as our bug and is a quoting one. **The caps one is the memory limit** the implementation
plan asked for: this runs on a 2 GB host beside Postgres, and the refusal has to happen before
anything is allocated.
"""

from __future__ import annotations

import pytest

from app.services.resume.pdf import (
    LEADING,
    MARGIN,
    MAX_CHARS,
    MAX_LINES,
    PAGE_HEIGHT,
    PdfTooLarge,
    render_pdf,
    wrap,
)


class TestItIsAPdf:
    def test_the_header_and_trailer_are_there(self) -> None:
        out = render_pdf("Asha Rao\n\nExperience\n- Built the payments API.\n")

        assert out.startswith(b"%PDF-1.4")
        assert out.rstrip().endswith(b"%%EOF")
        assert b"/Type /Catalog" in out
        assert b"startxref" in out

    def test_the_text_is_in_the_page_stream(self) -> None:
        out = render_pdf("Built the payments API at Acme.")

        assert b"(Built the payments API at Acme.) Tj" in out

    def test_the_cross_reference_table_offsets_point_at_their_objects(self) -> None:
        """A readable PDF with a wrong xref is the failure that opens in one viewer and not in
        another, which is the worst kind to debug from a bug report."""
        out = render_pdf("Asha Rao\n\nSkills\n- Postgres.\n")
        table = out.split(b"xref\n", 1)[1]
        rows = [row for row in table.splitlines() if row.endswith(b" n ")]

        assert rows
        for number, row in enumerate(rows, start=1):
            offset = int(row.split()[0])
            assert out[offset:].startswith(f"{number} 0 obj".encode())


class TestEscaping:
    def test_parentheses_are_escaped(self) -> None:
        out = render_pdf("- Backend engineer (remote).")

        assert rb"\(remote\)" in out
        assert b"(remote)." not in out.split(b"stream", 1)[1].split(b"endstream", 1)[0]

    def test_a_backslash_is_escaped(self) -> None:
        out = render_pdf(r"- Shipped on C:\builds.")

        assert rb"C:\\builds" in out

    def test_a_character_outside_latin_1_is_transliterated(self) -> None:
        """A rupee sign or an em dash would otherwise raise at encode time, at the end of a job,
        after the work was done. Transliterated rather than dropped: a name silently missing a
        character is worse than one spelled with an approximation."""
        out = render_pdf("- Paid \u20b9 50,000 \u2014 and shipped it.")

        assert b"(- Paid INR  50,000 - and shipped it.) Tj" in out


class TestTheCaps:
    def test_text_past_the_character_cap_is_refused(self) -> None:
        with pytest.raises(PdfTooLarge):
            render_pdf("x" * (MAX_CHARS + 1))

    def test_too_many_lines_is_refused(self) -> None:
        with pytest.raises(PdfTooLarge):
            render_pdf("\n".join(["line"] * (MAX_LINES + 50)))

    def test_a_long_document_becomes_several_pages(self) -> None:
        """The page count is derived rather than guessed at, because an assertion that accepts
        three answers is an assertion that would pass against a writer putting everything on one
        page -- the same shape as the `tools/list` ordering check that compared a list with
        itself."""
        lines = 200
        per_page = int((PAGE_HEIGHT - 2 * MARGIN) // LEADING)
        expected = -(-lines // per_page)
        out = render_pdf("\n".join(f"- bullet {number}" for number in range(lines)))

        assert expected > 1
        assert f"/Count {expected}".encode() in out
        assert out.count(b"/Type /Page ") == expected


class TestWrapping:
    def test_a_long_line_breaks_on_a_word(self) -> None:
        lines = wrap("word " * 60)

        assert len(lines) > 1
        assert all(len(line) <= 92 for line in lines)
        assert not any(line.startswith(" ") for line in lines)

    def test_a_single_word_longer_than_the_line_is_cut(self) -> None:
        """A URL. Letting it run off the page is no better than wrapping it."""
        lines = wrap("https://example.com/" + "a" * 200)

        assert len(lines) > 1
        assert all(len(line) <= 92 for line in lines)

    def test_a_short_line_is_left_alone(self) -> None:
        assert wrap("- Postgres and Docker.") == ["- Postgres and Docker."]
