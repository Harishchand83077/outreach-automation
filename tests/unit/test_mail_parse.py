"""Inbound mail parsing: message type, quote stripping, charsets and threading.

A8 and D2: the prototype had three classification buckets and no concept of a bounce or an
out-of-office, so a `MAILER-DAEMON` message was treated as a real reply — cancelling the
follow-up and marking a dead address as engaged.

Every fixture here is a real message shape rather than a contrived string, because the bugs in
this area come from the shapes real clients produce.
"""

from __future__ import annotations

import pytest

from app.services.mail.parse import (
    MessageType,
    html_to_text,
    normalised_subject,
    parse_message,
    strip_quoted,
)


def raw(headers: dict[str, str], body: str = "", *, content_type: str = "text/plain") -> bytes:
    lines = [f"{k}: {v}" for k, v in headers.items()]
    if not any(k.lower() == "content-type" for k in headers):
        lines.append(f'Content-Type: {content_type}; charset="utf-8"')
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode("utf-8")


HUMAN = raw(
    {
        "From": "Ben Shah <ben@globex.example>",
        "To": "asha@outreach.example",
        "Subject": "Re: Your pipeline hiring",
        "Message-ID": "<reply-1@globex.example>",
        "In-Reply-To": "<ours-1@outreach.example>",
        "References": "<ours-1@outreach.example>",
        "Date": "Mon, 12 Oct 2026 14:30:00 +0530",
    },
    "Thanks for reaching out. Can you send pricing?\n",
)


class TestHumanReply:
    def test_a_plain_reply_is_parsed(self):
        parsed = parse_message(HUMAN)
        assert parsed.message_type is MessageType.HUMAN
        assert parsed.from_addr == "ben@globex.example"
        assert "send pricing" in parsed.body_reply_only
        assert parsed.message_id == "<reply-1@globex.example>"

    def test_threading_ids_are_captured_in_order(self):
        """In-Reply-To names the message being answered; References is the whole thread. The
        first is tried first, because it is the more precise claim."""
        parsed = parse_message(HUMAN)
        assert parsed.in_reply_to == "<ours-1@outreach.example>"
        assert parsed.threading_ids[0] == "<ours-1@outreach.example>"

    def test_the_date_is_timezone_aware(self):
        parsed = parse_message(HUMAN)
        assert parsed.date is not None
        assert parsed.date.tzinfo is not None

    def test_an_rfc2047_encoded_subject_is_decoded(self):
        """Subjects routinely arrive encoded, and a classifier reading `=?UTF-8?B?...` has no
        idea what it is looking at."""
        encoded = raw(
            {
                "From": "ben@globex.example",
                "Subject": "=?UTF-8?B?UmU6IEhlbGxvIHdvcmxk?=",
                "Message-ID": "<x@y.example>",
            },
            "Hi there\n",
        )
        assert parse_message(encoded).subject == "Re: Hello world"


class TestQuoteStripping:
    """With the quote left in, the classifier reads our own pitch back and finds enthusiasm in
    it. The model is not wrong; it is reading the wrong text."""

    def test_an_on_wrote_block_is_removed(self):
        body = (
            "Not interested, thanks.\n\n"
            "On Mon, 12 Oct 2026 at 10:00, Asha Rao <asha@outreach.example> wrote:\n"
            "> We build fixed-scope data pipeline projects and would love to help!\n"
            "> Worth a short call?\n"
        )
        stripped = strip_quoted(body)
        assert "Not interested" in stripped
        assert "would love to help" not in stripped

    def test_quoted_lines_alone_are_removed(self):
        body = "No thanks.\n\n> Original pitch here\n> More pitch\n"
        assert "Original pitch" not in strip_quoted(body)

    def test_an_original_message_divider_is_removed(self):
        body = "Please remove me.\n\n-----Original Message-----\nFrom: Asha\nGreat offer!\n"
        stripped = strip_quoted(body)
        assert "Please remove me" in stripped
        assert "Great offer" not in stripped

    def test_an_outlook_from_block_is_removed(self):
        body = "Sure, Tuesday works.\n\nFrom: Asha Rao <asha@outreach.example>\nSent: Monday\n"
        stripped = strip_quoted(body)
        assert "Tuesday works" in stripped
        assert "Sent: Monday" not in stripped

    def test_a_signature_delimiter_truncates(self):
        body = "Yes please.\n\n--\nBen Shah\nCTO, Globex\n+91 00000 00000\n"
        stripped = strip_quoted(body)
        assert "Yes please" in stripped
        assert "+91" not in stripped

    def test_a_mobile_signature_is_removed(self):
        body = "Sounds good.\n\nSent from my iPhone\n"
        assert "iPhone" not in strip_quoted(body)

    def test_stripping_never_returns_nothing(self):
        """A one-line reply above an unrecognised quote block is better classified from the whole
        message than from an empty string."""
        body = "> only quoted text here\n> and more\n"
        assert strip_quoted(body).strip() != ""

    def test_the_full_body_is_kept_for_the_audit_trail(self):
        message = raw(
            {"From": "ben@globex.example", "Message-ID": "<x@y.example>"},
            "No thanks.\n\nOn Mon, Asha Rao wrote:\n> the pitch\n",
        )
        parsed = parse_message(message)
        assert "the pitch" in parsed.body_full, "stored in full"
        assert "the pitch" not in parsed.body_reply_only, "but not classified"


class TestCharsets:
    def test_a_declared_charset_is_honoured(self):
        """The prototype decoded with errors='replace' against a guess and produced mojibake,
        which it then fed to a model as though it were text."""
        body = "Je suis intéressé par votre offre".encode("cp1252")
        message = (
            b"From: ben@globex.example\r\n"
            b"Subject: Re: offer\r\n"
            b'Content-Type: text/plain; charset="windows-1252"\r\n\r\n' + body
        )
        assert "intéressé" in parse_message(message).body_full

    def test_utf8_devanagari_survives(self):
        message = raw(
            {"From": "ben@globex.example", "Subject": "Re: hello"},
            "धन्यवाद, मुझे दिलचस्पी है\n",
        )
        assert "धन्यवाद" in parse_message(message).body_full

    def test_an_undeclared_charset_still_decodes(self):
        message = (
            b"From: ben@globex.example\r\nSubject: Re: hi\r\n"
            b"Content-Type: text/plain\r\n\r\n" + "café".encode("cp1252")
        )
        assert parse_message(message).body_full  # does not raise, produces something


class TestHtmlOnly:
    def test_html_is_flattened(self):
        """Plenty of people reply from a client that sends HTML only."""
        html = "<html><body><p>Thanks!</p><p>Please send pricing.</p></body></html>"
        message = raw(
            {"From": "ben@globex.example", "Subject": "Re: hi"},
            html,
            content_type="text/html",
        )
        parsed = parse_message(message)
        assert "Thanks!" in parsed.body_full
        assert "send pricing" in parsed.body_full
        assert "<p>" not in parsed.body_full

    def test_script_contents_never_become_quotable_text(self):
        html = "<html><body><script>var t='tracker';</script><p>Real reply.</p></body></html>"
        assert "tracker" not in html_to_text(html)

    def test_plain_text_is_preferred_over_html_in_a_multipart(self):
        message = (
            b"From: ben@globex.example\r\n"
            b"Subject: Re: hi\r\n"
            b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"the plain version\r\n"
            b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b"<p>the html version</p>\r\n"
            b"--b--\r\n"
        )
        parsed = parse_message(message)
        assert "plain version" in parsed.body_full
        assert "html version" not in parsed.body_full

    def test_attachments_are_ignored(self):
        message = (
            b"From: ben@globex.example\r\n"
            b"Subject: Re: hi\r\n"
            b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
            b"--b\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="deck.pdf"\r\n\r\n'
            b"%PDF-1.4 binary junk\r\n"
            b"--b--\r\n"
        )
        parsed = parse_message(message)
        assert "See attached" in parsed.body_full
        assert "%PDF" not in parsed.body_full


class TestBounces:
    """D2: a MAILER-DAEMON message was treated as a real reply, so a dead address was marked
    engaged and its follow-up cancelled."""

    def _dsn(self, status: str, recipient: str = "ben@globex.example") -> bytes:
        return (
            b"From: MAILER-DAEMON@globex.example\r\n"
            b"To: asha@outreach.example\r\n"
            b"Subject: Undeliverable: Your pipeline hiring\r\n"
            b'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\r\n'
            b"\r\n--b\r\nContent-Type: text/plain\r\n\r\n"
            b"Delivery to the following recipient failed permanently.\r\n"
            b"--b\r\nContent-Type: message/delivery-status\r\n\r\n"
            + f"Final-Recipient: rfc822; {recipient}\r\nStatus: {status}\r\n".encode()
            + b"--b--\r\n"
        )

    def test_a_hard_bounce_is_recognised(self):
        parsed = parse_message(self._dsn("5.1.1"))
        assert parsed.message_type is MessageType.BOUNCE_HARD
        assert parsed.is_bounce
        assert parsed.failed_recipient == "ben@globex.example"
        assert parsed.dsn_status == "5.1.1"

    def test_a_soft_bounce_is_distinguished_from_a_hard_one(self):
        """Suppressing on a full mailbox would throw away a real lead."""
        parsed = parse_message(self._dsn("4.2.2"))
        assert parsed.message_type is MessageType.BOUNCE_SOFT
        assert parsed.is_bounce

    def test_a_daemon_message_without_a_dsn_part_is_still_caught(self):
        message = raw(
            {
                "From": "postmaster@globex.example",
                "Subject": "Delivery Status Notification (Failure)",
                "Message-ID": "<dsn@globex.example>",
            },
            "Your message could not be delivered: user unknown.\n",
        )
        parsed = parse_message(message)
        assert parsed.message_type is MessageType.BOUNCE_HARD

    def test_an_ambiguous_daemon_message_errs_soft(self):
        """A wrongly soft-bounced lead is retried; a wrongly hard-bounced one is suppressed
        forever. The asymmetry decides the default."""
        message = raw(
            {
                "From": "mailer-daemon@globex.example",
                "Subject": "Returned mail: see transcript for details",
                "Message-ID": "<dsn2@globex.example>",
            },
            "Something went wrong, no further detail.\n",
        )
        assert parse_message(message).message_type is MessageType.BOUNCE_SOFT

    def test_a_human_who_happens_to_mention_undeliverable_is_not_a_bounce(self):
        """The type comes from headers, not from words in the body."""
        message = raw(
            {
                "From": "ben@globex.example",
                "Subject": "Re: Your pipeline hiring",
                "Message-ID": "<human@globex.example>",
            },
            "Your last email was undeliverable to my colleague, try bob@globex.example\n",
        )
        assert parse_message(message).message_type is MessageType.HUMAN


class TestAutoReplies:
    """Treating one as a real reply cancels the follow-up for someone who never read the email,
    and puts a robot's message in front of a human reviewer."""

    @pytest.mark.parametrize(
        "header,value",
        [
            ("Auto-Submitted", "auto-replied"),
            ("X-Autoreply", "yes"),
            ("X-Autorespond", "auto"),
            ("Precedence", "auto_reply"),
        ],
    )
    def test_an_auto_header_is_enough(self, header, value):
        message = raw(
            {"From": "ben@globex.example", "Subject": "Re: hi", header: value},
            "I am away until Monday.\n",
        )
        assert parse_message(message).message_type is MessageType.AUTO_REPLY

    def test_auto_submitted_no_is_not_an_auto_reply(self):
        """Our own outbound mail sets `Auto-Submitted: no`; it must not match itself."""
        message = raw(
            {
                "From": "ben@globex.example",
                "Subject": "Re: hi",
                "Auto-Submitted": "no",
            },
            "Genuinely interested, let us talk.\n",
        )
        assert parse_message(message).message_type is MessageType.HUMAN

    @pytest.mark.parametrize(
        "phrase",
        [
            "I am out of office until further notice",
            "This is an automatic reply to your message",
            "I am currently on leave and will respond when I return",
            "Thanks, I'm on vacation with limited access to email",
        ],
    )
    def test_the_wording_is_recognised_without_a_header(self, phrase):
        message = raw({"From": "ben@globex.example", "Subject": "Re: hi"}, phrase + "\n")
        assert parse_message(message).message_type is MessageType.AUTO_REPLY

    def test_an_out_of_office_subject_is_recognised(self):
        message = raw(
            {"From": "ben@globex.example", "Subject": "Out of Office: Re: hi"},
            "Back soon.\n",
        )
        assert parse_message(message).message_type is MessageType.AUTO_REPLY

    def test_a_stated_return_date_is_extracted(self):
        """Used to push the follow-up out sensibly, so a miss costs a default delay only."""
        message = raw(
            {"From": "ben@globex.example", "Subject": "Automatic reply"},
            "I am away and will be back on 14 October. For urgent matters contact Bob.\n",
        )
        parsed = parse_message(message)
        assert parsed.return_date is not None
        assert parsed.return_date.month == 10
        assert parsed.return_date.day == 14

    def test_a_month_first_return_date_is_extracted(self):
        message = raw(
            {"From": "ben@globex.example", "Subject": "Automatic reply"},
            "On leave, returning on October 14, 2026.\n",
        )
        parsed = parse_message(message)
        assert parsed.return_date is not None
        assert parsed.return_date.day == 14

    def test_no_stated_date_is_not_an_error(self):
        message = raw(
            {"From": "ben@globex.example", "Subject": "Automatic reply"},
            "I am away from my desk.\n",
        )
        parsed = parse_message(message)
        assert parsed.message_type is MessageType.AUTO_REPLY
        assert parsed.return_date is None


class TestNormalisedSubject:
    """Used by the fallback match when threading headers are missing."""

    @pytest.mark.parametrize(
        "subject",
        [
            "Re: Your pipeline hiring",
            "RE: Your pipeline hiring",
            "Fwd: Your pipeline hiring",
            "Re: Re: Fwd: Your pipeline hiring",
            "AW: Your pipeline hiring",
            "  re:   Your   pipeline   hiring  ",
        ],
    )
    def test_prefixes_and_spacing_are_normalised_away(self, subject):
        """Clients add these in several languages and stack them, so without this the fallback
        would never match."""
        assert normalised_subject(subject) == "your pipeline hiring"

    def test_a_subject_with_no_prefix_is_unchanged(self):
        assert normalised_subject("Quick question") == "quick question"

    def test_an_empty_subject_is_safe(self):
        assert normalised_subject("") == ""
