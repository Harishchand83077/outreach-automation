"""A one-font, plain-text PDF, written by hand (F20, ADR-043).

**Why there is no PDF library here.** Hard rule 5 forbids adding a paid service and the
conventions forbid a dependency without a stated reason; `reportlab` and `weasyprint` are both
large trees whose purpose is laying out things this document does not have -- images, tables,
flowing HTML, font embedding. What a tailored resume needs is a sequence of short lines in one
standard font, and PDF 1.4 has exactly that: a single page stream of `Tj` operators against
Helvetica, which is one of the fourteen fonts every reader is required to provide, so nothing is
embedded and nothing is licensed.

**Memory is bounded by numbers chosen here**, which is what makes this safe to run on a 2 GB
host beside Postgres and the worker (`IMPLEMENTATION_PLAN_V3.md` §4 risk 6). The text is capped,
the line count is capped, and the result is one `bytearray` built in one pass -- so the worst a
pathological version costs is `MAX_CHARS` bytes plus the page overhead, and the handler can
refuse before allocating anything.

**Every string is escaped.** `(`, `)` and `\\` are structural in a PDF string literal, and a
resume says "(remote)" and "C:\\" often enough that an unescaped writer produces a file that
opens as garbage or not at all -- the failure looking like our bug and being a quoting one.
"""

from __future__ import annotations

#: Beyond this the input is not a resume. Checked before anything is allocated.
MAX_CHARS = 20_000
MAX_LINES = 400

#: A4 in points, and typographic settings that make a plain resume readable rather than pretty.
PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN = 56
FONT_SIZE = 10.5
LEADING = 14.5
#: Characters per line before wrapping. Helvetica at 10.5pt averages ~5.3pt a character, so this
#: is the width that fits between the margins with a little slack for wide glyphs.
WRAP_AT = 92


class PdfTooLarge(Exception):
    """The text is past the cap this renderer will allocate for."""


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _latin1(text: str) -> str:
    """Helvetica's built-in encoding is Latin-1, so anything outside it is transliterated.

    A rupee sign or an em dash would otherwise raise at encode time, at the end of a job, after
    the work was done. Replaced with a readable stand-in rather than dropped, because a name
    silently missing a character is worse than one spelled with an ASCII approximation.
    """
    swaps = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "-",
        "\u20b9": "INR ",
        "\u2026": "...",
    }
    for bad, good in swaps.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", errors="replace").decode("latin-1")


def wrap(line: str, width: int = WRAP_AT) -> list[str]:
    """Break one logical line into rendered lines, on word boundaries where there are any."""
    if len(line) <= width:
        return [line]
    out: list[str] = []
    current = ""
    for word in line.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
        # A single word longer than the line (a URL) is cut rather than allowed to run off the
        # page: a link nobody can see the end of is no better than one that is wrapped.
        while len(word) > width:
            out.append(word[:width])
            word = word[width:]
        current = word
    if current:
        out.append(current)
    return out


def render_pdf(text: str, *, title: str = "Resume") -> bytes:
    """A PDF of this plain text. Raises `PdfTooLarge` past the caps."""
    if len(text or "") > MAX_CHARS:
        raise PdfTooLarge(f"a resume of {len(text)} characters is past the {MAX_CHARS} cap")

    lines: list[str] = []
    for raw in (text or "").splitlines():
        lines.extend(wrap(_latin1(raw.rstrip())))
        if len(lines) > MAX_LINES:
            raise PdfTooLarge(f"a resume of more than {MAX_LINES} lines is past the cap")

    per_page = int((PAGE_HEIGHT - 2 * MARGIN) // LEADING)
    pages = [lines[start : start + per_page] for start in range(0, len(lines), per_page)] or [[]]

    streams: list[bytes] = []
    for page in pages:
        parts = [
            "BT",
            f"/F1 {FONT_SIZE} Tf",
            f"{LEADING} TL",
            f"1 0 0 1 {MARGIN} {PAGE_HEIGHT - MARGIN} Tm",
        ]
        for line in page:
            # `Tj` then `T*`: write the line, then move down one leading. An empty line still
            # moves, which is what keeps the blank line before each heading.
            parts.append(f"({_escape(line)}) Tj")
            parts.append("T*")
        parts.append("ET")
        streams.append("\n".join(parts).encode("latin-1"))

    # Objects: 1 catalog, 2 pages, 3 font, then per page a page object and its content stream.
    objects: list[bytes] = []
    page_ids = [4 + index * 2 for index in range(len(pages))]
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Count {len(pages)} /Kids [{kids}] >>".encode("latin-1"))
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )
    for index, stream in enumerate(streams):
        content_id = page_ids[index] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("latin-1")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("latin-1")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("latin-1") + body + b"\nendobj\n"

    start_xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R "
        f"/Info << /Title ({_escape(_latin1(title))}) >> >>\n"
        f"startxref\n{start_xref}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)
