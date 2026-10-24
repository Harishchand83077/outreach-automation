"""An attachment changes the shape of the message and nothing else about it."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.mail.message import (
    Attachment,
    FooterIdentity,
    Mailbox,
    build_message,
)

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
MAILBOX = Mailbox(
    from_name="Asha Rao", from_email="asha@outreach.example", reply_to="asha@outreach.example"
)
IDENTITY = FooterIdentity(sender_name="Asha Rao", agency_name="Arkvane")
DECK = Attachment(filename="arkvane.pdf", content_type="application/pdf", content=b"%PDF-1.7\n..")


def build(**kwargs: object) -> object:
    return build_message(
        mailbox=MAILBOX,
        to_email="founder@example.com",
        subject="Quick question",
        body="Hi there,\n\nA short note about your pipeline.\n\nAsha",
        identity=IDENTITY,
        message_id="<fixed@outreach.example>",
        now=NOW,
        **kwargs,  # type: ignore[arg-type]
    )


class TestWithoutAnAttachment:
    def test_the_message_is_still_exactly_plain_text(self) -> None:
        """The locked product rule is plain text only. Adding this feature must not have moved
        every ordinary email to multipart."""
        built = build()

        assert built.message.get_content_type() == "text/plain"
        assert not built.message.is_multipart()

    def test_an_empty_sequence_is_the_same_as_none(self) -> None:
        assert build().message.as_bytes() == build(attachments=[]).message.as_bytes()


class TestWithOne:
    def test_the_message_becomes_multipart_mixed(self) -> None:
        assert build(attachments=[DECK]).message.get_content_type() == "multipart/mixed"

    def test_the_first_part_is_the_same_plain_text_body(self) -> None:
        """A client that cannot read MIME still shows the text, and there is no `text/html`
        alternative anywhere in here."""
        built = build(attachments=[DECK])
        parts = list(built.message.iter_parts())  # type: ignore[attr-defined]

        assert parts[0].get_content_type() == "text/plain"
        assert "A short note about your pipeline." in parts[0].get_content()
        assert not any(part.get_content_type() == "text/html" for part in parts)

    def test_the_footer_still_reaches_the_recipient(self) -> None:
        """The opt-out line is a compliance control, and it has to survive a change in how the
        message is assembled."""
        built = build(attachments=[DECK])
        parts = list(built.message.iter_parts())  # type: ignore[attr-defined]
        text = parts[0].get_content()

        # The sender's name, and the opt-out sentence the model never gets to reword.
        assert "Asha Rao" in text
        assert "no thanks" in text.lower()

    def test_the_file_is_carried_with_our_type_and_name(self) -> None:
        built = build(attachments=[DECK])
        attached = [
            part
            for part in built.message.iter_parts()  # type: ignore[attr-defined]
            if part.get_filename()
        ]

        assert len(attached) == 1
        assert attached[0].get_filename() == "arkvane.pdf"
        assert attached[0].get_content_type() == "application/pdf"
        assert attached[0].get_payload(decode=True) == DECK.content

    def test_every_header_that_matters_is_untouched(self) -> None:
        """Threading, unsubscribe and identity all live in headers, and a change of body
        structure must not disturb any of them."""
        built = build(
            attachments=[DECK],
            in_reply_to="<parent@outreach.example>",
            references="<parent@outreach.example>",
        )

        assert built.message["Message-ID"] == "<fixed@outreach.example>"
        assert built.message["In-Reply-To"] == "<parent@outreach.example>"
        assert built.message["Reply-To"] == "asha@outreach.example"
        assert "mailto:" in built.message["List-Unsubscribe"]
        assert built.message["Auto-Submitted"] == "no"

    def test_a_newline_in_the_filename_cannot_inject_a_header(self) -> None:
        """`build_message` sanitises again rather than trusting the caller: this value is
        written into a header, and the cost of being sure is one function call."""
        built = build(
            attachments=[
                Attachment(
                    filename="deck\r\nBcc: victim@example.com.pdf",
                    content_type="application/pdf",
                    content=b"x",
                )
            ]
        )

        assert "victim@example.com" not in str(built.message["To"])
        assert built.message["Bcc"] is None
        raw = built.message.as_bytes().decode("utf-8", "replace")
        assert "\nBcc:" not in raw

    def test_a_nonsense_mime_type_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(ValueError, match="not a usable MIME type"):
            build(
                attachments=[
                    Attachment(filename="x.pdf", content_type="application-pdf", content=b"x")
                ]
            )
