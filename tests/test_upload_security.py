"""Regression tests for C2 and NEW-8.

The prototype wrote the client-supplied filename straight into a path
(`_root/"uploads"/file.filename`), so `../../x.xlsx` escaped the directory, and uploads were never
cleaned up. The fix removes the disk write entirely: the sheet is parsed from memory, so there is
no path to traverse.
"""

from pathlib import Path

import pytest

import api_server
from conftest import executable_source
from excel_loader import load_leads_from_bytes

REPO_ROOT = Path(__file__).resolve().parent.parent

CSV_BYTES = (
    b"Name,Email,Company\n"
    b"Asha Rao,asha@example.com,Acme\n"
    b"Ben Shah,ben@example.com,Globex\n"
)


class TestNoDiskWrite:
    def test_upload_handler_never_builds_a_path_from_the_filename(self):
        src = executable_source(api_server)
        assert "write_bytes" not in src
        assert "uploads" not in src

    def test_no_uploads_directory_is_created(self):
        assert not (REPO_ROOT / "uploads").exists()


class TestParseFromBytes:
    def test_csv_bytes_parse(self):
        leads = load_leads_from_bytes(CSV_BYTES, "leads.csv")
        assert [lead["email"] for lead in leads] == ["asha@example.com", "ben@example.com"]

    @pytest.mark.parametrize("filename", [
        "../../pwned.csv",
        "..\\..\\pwned.csv",
        "/etc/passwd.csv",
        "C:\\Windows\\Temp\\pwned.csv",
    ])
    def test_traversal_filenames_are_only_used_for_extension_dispatch(self, filename):
        """A hostile name must not matter, because nothing is written to disk."""
        leads = load_leads_from_bytes(CSV_BYTES, filename)
        assert len(leads) == 2
        assert not Path("pwned.csv").exists()
        assert not (REPO_ROOT.parent.parent / "pwned.csv").exists()

    def test_xls_is_rejected_with_a_clear_message(self):
        """NEW-8 — the API advertised .xls but openpyxl cannot read it."""
        with pytest.raises(ValueError, match="xls"):
            load_leads_from_bytes(b"\xd0\xcf\x11\xe0", "legacy.xls")

    def test_unknown_extension_is_rejected(self):
        with pytest.raises(ValueError):
            load_leads_from_bytes(CSV_BYTES, "leads.txt")

    def test_oversized_upload_is_rejected(self):
        too_big = b"Name,Email,Company\n" + b"a,a@example.com,b\n" * 400_000
        with pytest.raises(ValueError, match="too large"):
            load_leads_from_bytes(too_big, "leads.csv")

    def test_too_many_rows_is_rejected(self):
        many = b"Name,Email,Company\n" + b"".join(
            f"n{i},n{i}@example.com,c{i}\n".encode() for i in range(5_001)
        )
        with pytest.raises(ValueError, match="rows"):
            load_leads_from_bytes(many, "leads.csv")


class TestRowLongerThanHeader:
    """B7 — `col_map[i]` raised KeyError and killed the whole upload."""

    def test_stray_extra_column_does_not_crash(self):
        data = (
            b"Name,Email,Company\n"
            b"Asha,asha@example.com,Acme,STRAY\n"
            b"Ben,ben@example.com,Globex\n"
        )
        leads = load_leads_from_bytes(data, "leads.csv")
        assert len(leads) == 2
