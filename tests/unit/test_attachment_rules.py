"""What may be attached to an outgoing email.

These are the rules that decide what this system will put in front of a stranger's virus
scanner, so they are tested as rules rather than through the API: the API can be changed, and
the point of `app/domain/attachment.py` is that the answer does not depend on who is asking.
"""

from __future__ import annotations

import hashlib

import pytest

from app.domain.attachment import (
    ALLOWED,
    MAX_BYTES,
    AttachmentRejected,
    check,
    extension_of,
    safe_filename,
    should_attach,
)

PDF = b"%PDF-1.7\nnot really a pdf but the bytes are never parsed\n"


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "name",
        ["payload.exe", "setup.msi", "run.bat", "a.ps1", "x.js", "macro.docm", "old.doc"],
    )
    def test_an_executable_or_macro_format_is_never_attached(self, name: str) -> None:
        """The oldest malware delivery mechanism there is. A system that will send one on
        request is a system that will eventually be asked to."""
        with pytest.raises(AttachmentRejected) as caught:
            check(name, b"MZ\x90\x00")

        assert "never attached" in str(caught.value)

    def test_an_archive_is_refused_because_nothing_can_look_inside_it(self) -> None:
        with pytest.raises(AttachmentRejected, match="archive"):
            check("bundle.zip", b"PK\x03\x04")

    def test_an_unknown_type_is_refused_rather_than_sent_as_octet_stream(self) -> None:
        """'I do not know what this is, here it is anyway' is not a position to take about a
        file one is mailing to strangers."""
        with pytest.raises(AttachmentRejected, match="not an allowed attachment type"):
            check("mystery.xyz", b"data")

    def test_a_file_with_no_extension_has_no_decidable_type(self) -> None:
        with pytest.raises(AttachmentRejected, match="no file extension"):
            check("README", b"data")

    def test_an_empty_file_says_so_rather_than_complaining_about_its_type(self) -> None:
        with pytest.raises(AttachmentRejected, match="empty"):
            check("deck.pdf", b"")

    def test_size_is_reported_before_type(self) -> None:
        """Reporting 'unsupported type' about a file whose real problem is that it is 40 MB
        would send somebody off to rename it."""
        with pytest.raises(AttachmentRejected, match="MB"):
            check("huge.xyz", b"x" * (MAX_BYTES + 1))

    def test_the_limit_itself_is_accepted(self) -> None:
        assert check("big.pdf", b"x" * MAX_BYTES).size_bytes == MAX_BYTES


class TestTheTypeIsOursNotTheUploaders:
    def test_the_content_type_comes_from_the_allowlist(self) -> None:
        """A browser sends a Content-Type it guessed from the extension. Trusting it means a
        recipient's client is told `application/pdf` about something that is not one."""
        assert check("deck.pdf", PDF).content_type == "application/pdf"
        assert check("sheet.csv", b"a,b\n1,2\n").content_type == "text/csv"

    @pytest.mark.parametrize("ext", sorted(ALLOWED))
    def test_every_allowed_extension_round_trips(self, ext: str) -> None:
        assert check(f"file{ext}", b"content").content_type == ALLOWED[ext]

    def test_the_extension_match_ignores_case(self) -> None:
        assert check("DECK.PDF", PDF).content_type == "application/pdf"

    def test_the_digest_is_of_the_bytes_stored(self) -> None:
        assert check("deck.pdf", PDF).sha256 == hashlib.sha256(PDF).hexdigest()


class TestTheFilename:
    def test_a_traversing_name_cannot_survive(self) -> None:
        """Nothing here writes to a disk — the bytes go to Postgres — but a wrong name should
        not be quietly mailed out either."""
        assert "/" not in safe_filename("../../etc/passwd.pdf")
        assert not safe_filename("../../etc/passwd.pdf").startswith(".")

    def test_a_newline_cannot_reach_a_mime_header(self) -> None:
        """Header injection in a filename is header injection exactly as it is in a subject."""
        cleaned = safe_filename("deck\r\nBcc: someone@example.com.pdf")
        assert "\r" not in cleaned
        assert "\n" not in cleaned

    def test_a_windows_path_keeps_only_its_last_part(self) -> None:
        assert safe_filename(r"C:\Users\Mi\Desktop\deck.pdf") == "deck.pdf"

    def test_a_very_long_name_is_truncated_but_keeps_its_extension(self) -> None:
        name = safe_filename("a" * 400 + ".pdf")
        assert len(name) <= 120
        assert name.endswith(".pdf")

    def test_a_name_of_only_punctuation_is_refused_rather_than_mailed(self) -> None:
        with pytest.raises(AttachmentRejected):
            check("...", b"data")

    def test_extension_of_is_case_folded(self) -> None:
        assert extension_of("Deck.PDF") == ".pdf"
        assert extension_of("noext") == ""


class TestWhoDecidesPerDraft:
    def test_nobody_having_said_means_the_campaign_decides(self) -> None:
        assert should_attach(draft_override=None, campaign_default=True) is True
        assert should_attach(draft_override=None, campaign_default=False) is False

    def test_a_reviewer_who_unticked_the_box_wins_over_the_campaign(self) -> None:
        assert should_attach(draft_override=False, campaign_default=True) is False

    def test_a_reviewer_may_also_turn_it_on_for_one_email(self) -> None:
        """The case this exists for on a reply: somebody asked for the deck, so send it, even
        though replies never carry it by default."""
        assert should_attach(draft_override=True, campaign_default=False) is True
