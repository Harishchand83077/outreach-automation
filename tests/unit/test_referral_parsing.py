"""Reading a connections export, and matching a company name (F10, ADR-042).

The export's two awkward properties are what these tests are mostly about: it **begins with a
preamble rather than with headers**, and the company name in it is whatever the connection typed
into their own profile. Both are places where the obvious implementation returns a confidently
wrong answer rather than an error -- zero connections from a perfectly good file, or no referral
at a company the user has four friends at.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.referrals.parsing import ExportError, normalise_company, parse_export

HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"
PREAMBLE = 'Notes:\n"When exporting your connection data, you may notice..."\n\n'


def _export(*rows: str, preamble: bool = True) -> bytes:
    body = "\n".join([HEADER, *rows]) + "\n"
    return ((PREAMBLE if preamble else "") + body).encode("utf-8")


class TestThePreamble:
    def test_a_real_export_is_read_despite_its_notes_block(self) -> None:
        """The defect this test exists for: taking line 1 as the header reads the whole file as
        one unnamed column and reports **zero connections**, which is indistinguishable from an
        empty network -- the same silent wrong answer as the ATS feed that returned no postings.
        """
        parsed = parse_export(
            _export(
                "Asha,Rao,https://example.com/in/asha,,Acme Inc,Head of Engineering,21 Mar 2024"
            )
        )

        assert len(parsed.connections) == 1
        assert parsed.connections[0].full_name == "Asha Rao"

    def test_a_file_with_no_preamble_is_read_too(self) -> None:
        """Older exports and anything the user has opened and re-saved in a spreadsheet."""
        parsed = parse_export(
            _export(
                "Asha,Rao,https://example.com/in/asha,,Acme,Engineer,21 Mar 2024", preamble=False
            )
        )

        assert len(parsed.connections) == 1

    def test_a_file_that_is_not_an_export_is_an_error_with_a_sentence(self) -> None:
        """Never an empty result: "we read your file and you know nobody" is the one answer this
        must not give to somebody who uploaded the wrong CSV."""
        with pytest.raises(ExportError) as caught:
            parse_export(b"name,amount\nInvoice,100\n")

        assert "Connections.csv" in str(caught.value)

    def test_an_empty_file_is_an_error(self) -> None:
        with pytest.raises(ExportError):
            parse_export(b"")


class TestWhatIsRead:
    def test_the_fields_a_referral_needs(self) -> None:
        parsed = parse_export(
            _export(
                "Asha,Rao,https://example.com/in/asha,,Acme Inc,Head of Engineering,21 Mar 2024"
            )
        )
        one = parsed.connections[0]

        assert one.profile_url == "https://example.com/in/asha"
        assert one.company == "Acme Inc"
        assert one.position == "Head of Engineering"
        assert one.connected_on == date(2024, 3, 21)

    def test_an_unreadable_date_does_not_lose_the_connection(self) -> None:
        """When somebody connected is colour; who they are is the feature."""
        parsed = parse_export(_export("Asha,Rao,https://example.com/in/asha,,Acme,Engineer,soon"))

        assert parsed.connections[0].connected_on is None
        assert parsed.connections[0].full_name == "Asha Rao"

    def test_a_row_with_no_name_is_counted_rather_than_silently_dropped(self) -> None:
        """ "4,312 of 4,400" is the only thing that tells somebody the file was partly unread."""
        parsed = parse_export(
            _export(
                "Asha,Rao,https://example.com/in/asha,,Acme,Engineer,21 Mar 2024",
                ",,,,Acme,Engineer,21 Mar 2024",
            )
        )

        assert len(parsed.connections) == 1
        assert parsed.skipped == 1

    def test_a_connection_with_no_company_is_still_read(self) -> None:
        """They match nothing today and they are in the network; dropping them would make a
        re-export look like people had left."""
        parsed = parse_export(_export("Asha,Rao,https://example.com/in/asha,,,,21 Mar 2024"))

        assert len(parsed.connections) == 1
        assert parsed.connections[0].company is None


class TestNormalisingACompanyName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Acme", "acme"),
            ("ACME", "acme"),
            ("Acme, Inc.", "acme"),
            ("Acme Inc", "acme"),
            ("Acme Pvt Ltd", "acme"),
            ("Acme Software Solutions Pvt. Ltd.", "acme"),
            ("Acme India", "acme india"),
            ("  Acme   Labs  ", "acme"),
        ],
    )
    def test_the_same_company_written_four_ways_is_one_key(self, raw: str, expected: str) -> None:
        assert normalise_company(raw) == expected

    def test_stripping_stops_before_it_empties_the_name(self) -> None:
        """ "Technology Group Ltd" is somebody's actual company.

        Reduced to nothing it would match every other all-suffix name, which is the worst kind
        of match: confident, wrong, and about a person the user is about to message.
        """
        assert normalise_company("Technology Group Ltd") == "technology"

    def test_nothing_in_means_nothing_out(self) -> None:
        assert normalise_company(None) is None
        assert normalise_company("   ") is None
        assert normalise_company("...") is None
