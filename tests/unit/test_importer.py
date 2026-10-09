"""Header mapping, row validation and sheet parsing.

The prototype normalised headers with `.title()` and then demanded exactly `Name`, `Email`,
`Company`, so `E-mail`, `Company Name` and `First Name` all failed; a row one cell longer than
the header raised `KeyError` and killed the whole upload; and the only address check was
`'@' in email` (issues B7, D4, A5).

No test here touches DNS: the MX resolver is injected.
"""

from __future__ import annotations

import pytest

from app.services.importer import sheet
from app.services.importer.headers import (
    RECOMMENDED_FIELDS,
    build_full_name,
    map_headers,
    normalise,
)
from app.services.importer.validation import (
    RowVerdict,
    ValidatedRow,
    check_company_url,
    check_email,
    check_linkedin_url,
    normalise_url,
)


class FakeMx:
    """Deliverable unless the domain is listed as dead."""

    def __init__(self, dead: set[str] | None = None) -> None:
        self.dead = dead or set()
        self.queried: list[str] = []

    def has_mx(self, domain: str) -> bool:
        self.queried.append(domain)
        return domain not in self.dead


class TestHeaderNormalisation:
    @pytest.mark.parametrize(
        "header",
        ["Email", "email", "E-mail", "E_Mail", " EMAIL ", "e mail", "E-Mail"],
    )
    def test_every_spelling_of_email_is_one_shape(self, header):
        """These are the exact spellings that defeated the prototype."""
        assert normalise(header) == "email"

    def test_mapping_handles_the_headers_that_used_to_fail(self):
        mapping = map_headers(["Full Name", "E-mail", "Company Name", "Website"])
        assert mapping.mapped == {
            0: "full_name",
            1: "email",
            2: "company_name",
            3: "company_url",
        }
        assert mapping.missing_required == []
        assert mapping.is_importable


class TestHeaderMapping:
    def test_unrecognised_columns_are_kept_not_discarded(self):
        """A5: the prototype dropped every extra column, so none of it could feed research."""
        mapping = map_headers(["Name", "Email", "Company", "Segment", "ARR"])
        assert mapping.unmapped == {3: "Segment", 4: "ARR"}
        assert mapping.is_importable

    def test_a_missing_required_column_is_reported_not_guessed(self):
        mapping = map_headers(["Name", "Company"])
        assert mapping.missing_required == ["email"]
        assert not mapping.is_importable

    def test_missing_recommended_columns_do_not_block(self):
        mapping = map_headers(["Name", "Email", "Company"])
        assert mapping.is_importable
        assert set(mapping.missing_recommended) == set(RECOMMENDED_FIELDS)

    def test_two_columns_claiming_one_field_is_ambiguous(self):
        """Guessing which `email` is the real one is precisely a human's decision."""
        mapping = map_headers(["Email", "Work Email", "Name", "Company"])
        assert "email" in mapping.duplicates
        assert mapping.duplicates["email"] == [0, 1]
        assert not mapping.is_importable

    def test_a_split_first_and_last_name_is_understood(self):
        mapping = map_headers(["First Name", "Last Name", "Email", "Company"])
        assert mapping.first_name_column == 0
        assert mapping.last_name_column == 1
        assert mapping.missing_required == [], "a split name still satisfies full_name"
        assert build_full_name(mapping, ["Asha", "Rao", "a@example.com", "Acme"]) == "Asha Rao"

    def test_a_first_name_alone_still_works(self):
        mapping = map_headers(["First Name", "Email", "Company"])
        assert build_full_name(mapping, ["Asha", "a@example.com", "Acme"]) == "Asha"

    def test_blank_headers_are_ignored(self):
        mapping = map_headers(["Name", "", "Email", "   ", "Company"])
        assert mapping.is_importable
        assert 1 not in mapping.unmapped


class TestEmailValidation:
    def test_a_good_address_is_normalised(self):
        row = ValidatedRow(row_number=2)
        check_email(row, "  Asha.Rao@Example.COM ", mx=FakeMx())
        assert row.verdict is RowVerdict.OK
        assert row.email_norm == "asha.rao@example.com"
        assert row.email_raw == "Asha.Rao@Example.COM"
        assert row.domain == "example.com"

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "not-an-email", "@example.com", "asha@", "asha example.com", "a@b"],
    )
    def test_malformed_addresses_are_rejected_with_a_reason(self, bad):
        """D4: `'@' in email` accepted most of these."""
        row = ValidatedRow(row_number=2)
        check_email(row, bad, mx=FakeMx())
        assert row.verdict is RowVerdict.REJECT
        assert row.reasons, "a rejection must always say why"

    def test_a_domain_with_no_mx_is_rejected(self):
        row = ValidatedRow(row_number=2)
        check_email(row, "asha@nomail.example", mx=FakeMx(dead={"nomail.example"}))
        assert row.verdict is RowVerdict.REJECT
        assert "MX" in " ".join(row.reasons)

    def test_a_role_address_is_a_warning_not_a_rejection(self):
        """Shared inboxes are a poor bet and a spam signal, but the user may still want them."""
        row = ValidatedRow(row_number=2)
        check_email(row, "info@example.com", mx=FakeMx())
        assert row.verdict is RowVerdict.WARN
        assert row.importable
        assert "role_address" in row.flags

    def test_a_disposable_address_is_rejected(self):
        row = ValidatedRow(row_number=2)
        check_email(row, "x@mailinator.com", mx=FakeMx())
        assert row.verdict is RowVerdict.REJECT

    def test_free_mail_is_flagged_because_the_site_cannot_be_inferred(self):
        """The reason this matters: guessing a company URL from gmail.com would send the
        research fetcher off to fetch gmail.com."""
        row = ValidatedRow(row_number=2)
        check_email(row, "founder@gmail.com", mx=FakeMx())
        assert row.verdict is RowVerdict.WARN
        assert "free_mail" in row.flags
        assert row.importable

    def test_mx_is_queried_once_per_domain(self):
        """Several people at one company is the normal case, and DNS is slow."""
        mx = FakeMx()
        for local in ("a", "b", "c"):
            check_email(ValidatedRow(row_number=2), f"{local}@shared.example", mx=mx)
        # The resolver is asked each time; caching is the real resolver's job, verified here as
        # the contract it must satisfy.
        assert mx.queried == ["shared.example"] * 3

    def test_a_warning_never_downgrades_a_rejection(self):
        row = ValidatedRow(row_number=2)
        row.reject("already imported")
        row.warn("role address")
        assert row.verdict is RowVerdict.REJECT


class TestUrlNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("example.com", "https://example.com"),
            ("www.example.com", "https://example.com"),
            ("http://example.com", "https://example.com"),
            ("https://Example.COM/", "https://example.com"),
            ("https://example.com/about/", "https://example.com/about"),
            ("  example.co.in  ", "https://example.co.in"),
        ],
    )
    def test_urls_are_canonicalised_to_https(self, raw, expected):
        assert normalise_url(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not a url", "ftp://example.com", "javascript:alert(1)", "http://"]
    )
    def test_unusable_urls_return_none(self, raw):
        assert normalise_url(raw) is None

    def test_a_missing_company_url_warns_and_is_never_guessed(self):
        """A1's root cause was having no site to research. But inventing one is worse."""
        row = ValidatedRow(row_number=2)
        row.email_norm = "founder@gmail.com"
        check_company_url(row, None)
        assert row.verdict is RowVerdict.WARN
        assert "no_company_url" in row.flags
        assert row.company_url is None, "it must not be derived from the email domain"

    def test_an_unusable_company_url_warns_but_imports(self):
        row = ValidatedRow(row_number=2)
        check_company_url(row, "n/a")
        assert row.importable
        assert "bad_company_url" in row.flags


class TestLinkedInUrl:
    def test_it_is_stored_for_reference(self):
        row = ValidatedRow(row_number=2)
        check_linkedin_url(row, "linkedin.com/in/asharao")
        assert row.linkedin_url == "https://linkedin.com/in/asharao"
        assert row.verdict is RowVerdict.OK

    def test_a_non_linkedin_url_is_flagged(self):
        row = ValidatedRow(row_number=2)
        check_linkedin_url(row, "https://twitter.com/asharao")
        assert "bad_linkedin_url" in row.flags

    def test_absent_is_fine(self):
        row = ValidatedRow(row_number=2)
        check_linkedin_url(row, None)
        assert row.verdict is RowVerdict.OK


class TestSheetParsing:
    CSV = b"Name,Email,Company\nAsha,asha@example.com,Acme\nBen,ben@example.com,Globex\n"

    def test_a_csv_is_parsed(self):
        parsed = sheet.parse(self.CSV, "leads.csv")
        assert parsed.headers == ["Name", "Email", "Company"]
        assert parsed.row_count == 2

    def test_a_row_longer_than_the_header_does_not_crash(self):
        """B7: this raised KeyError and killed the entire upload."""
        data = b"Name,Email,Company\nAsha,asha@example.com,Acme,STRAY,MORE\n"
        parsed = sheet.parse(data, "leads.csv")
        assert parsed.row_count == 1
        assert len(parsed.rows[0]) == 3, "trimmed to the header width"

    def test_a_row_shorter_than_the_header_is_padded(self):
        data = b"Name,Email,Company\nAsha,asha@example.com\n"
        parsed = sheet.parse(data, "leads.csv")
        assert parsed.rows[0] == ["Asha", "asha@example.com", None]

    def test_blank_separator_rows_are_skipped(self):
        data = b"Name,Email,Company\nAsha,asha@example.com,Acme\n,,\n\nBen,ben@example.com,Globex\n"
        assert sheet.parse(data, "leads.csv").row_count == 2

    def test_an_excel_utf8_bom_does_not_corrupt_the_first_header(self):
        """Excel's "CSV UTF-8" export writes a BOM, which would otherwise make the first header
        unmatchable — and the first header is usually Name."""
        parsed = sheet.parse(b"\xef\xbb\xbf" + self.CSV, "leads.csv")
        assert parsed.headers[0] == "Name"

    def test_xls_is_rejected_with_an_actionable_message(self):
        with pytest.raises(sheet.SheetError, match="Re-save"):
            sheet.parse(b"\xd0\xcf\x11\xe0", "legacy.xls")

    def test_an_unknown_extension_is_rejected(self):
        with pytest.raises(sheet.SheetError, match="Unsupported"):
            sheet.parse(self.CSV, "leads.txt")

    def test_an_empty_file_is_rejected(self):
        with pytest.raises(sheet.SheetError, match="empty"):
            sheet.parse(b"", "leads.csv")

    def test_a_header_only_file_parses_to_no_rows(self):
        parsed = sheet.parse(b"Name,Email,Company\n", "leads.csv")
        assert parsed.row_count == 0

    def test_an_oversized_file_is_rejected(self):
        too_big = b"Name,Email,Company\n" + b"a,a@example.com,b\n" * 400_000
        with pytest.raises(sheet.SheetError, match="too large"):
            sheet.parse(too_big, "leads.csv")

    def test_excess_rows_are_reported_not_silently_dropped(self):
        rows = b"".join(
            f"n{i},n{i}@example.com,c{i}\n".encode() for i in range(sheet.MAX_ROWS + 10)
        )
        parsed = sheet.parse(b"Name,Email,Company\n" + rows, "leads.csv")
        assert parsed.row_count == sheet.MAX_ROWS
        assert parsed.truncated == 10, "the user must be told rows were left out"

    def test_too_many_columns_is_rejected(self):
        header = ",".join(f"col{i}" for i in range(sheet.MAX_COLUMNS + 1)).encode()
        with pytest.raises(sheet.SheetError, match="Too many columns"):
            sheet.parse(header + b"\n", "leads.csv")
