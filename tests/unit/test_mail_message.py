"""Message construction: headers, footer and header-injection safety.

D1 and D5: the prototype had no unsubscribe mechanism, no `List-Unsubscribe`, no `Message-ID`
or `Date` of its own, no `Reply-To`, and built HTML by replacing newlines with `<br>` without
escaping anything (B14).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.mail.message import (
    FooterIdentity,
    Mailbox,
    build_footer,
    build_message,
    build_references,
    sanitise_header,
    unsubscribe_mailto,
)

MAILBOX = Mailbox(
    from_name="Asha Rao",
    from_email="asha@outreach.example",
    reply_to="asha@outreach.example",
)

IDENTITY = FooterIdentity(
    sender_name="Asha Rao",
    agency_name="Acme Software",
    agency_url="https://acme.example",
    postal_address="12 MG Road, Bengaluru 560001, India",
)

BODY = "Hi Ben,\n\nI saw you are hiring backend engineers.\n\nWorth a short call?\n\nAsha"


def build(**kw):
    defaults = {
        "mailbox": MAILBOX,
        "to_email": "ben@globex.example",
        "subject": "Your pipeline hiring",
        "body": BODY,
        "identity": IDENTITY,
        "now": datetime(2026, 10, 12, 11, 0, tzinfo=UTC),
    }
    return build_message(**{**defaults, **kw})


class TestHeaderInjection:
    """A newline in a header lets everything after it be read as a new header — which is how a
    Bcc gets added to someone else's email."""

    @pytest.mark.parametrize(
        "hostile",
        [
            "Hello\r\nBcc: attacker@evil.example",
            "Hello\nBcc: attacker@evil.example",
            "Hello\x00Bcc: attacker@evil.example",
        ],
    )
    def test_newlines_are_stripped_from_a_subject(self, hostile):
        built = build(subject=hostile)
        assert "\n" not in built.subject
        assert "\r" not in built.subject
        assert built.message["Bcc"] is None

    def test_the_rendered_message_has_no_injected_header(self):
        """The property that matters is that no *new header* exists.

        The address surviving as inert text inside the subject is harmless — it is just a
        strange-looking subject line. What must not happen is a `Bcc:` header appearing, which
        is what the newline was there to create.
        """
        built = build(subject="Hi\r\nBcc: attacker@evil.example")
        rendered = built.as_bytes().decode()
        header_block = rendered.split("\n\n", 1)[0]

        header_names = {
            line.split(":", 1)[0].strip().lower()
            for line in header_block.splitlines()
            if ":" in line and not line.startswith((" ", "\t"))
        }
        assert "bcc" not in header_names
        assert built.message["Bcc"] is None
        # And the recipient list is exactly who we meant to write to.
        assert str(built.message["To"]) == "ben@globex.example"

    def test_sanitise_collapses_the_gap_a_stripped_newline_leaves(self):
        assert sanitise_header("Hello\r\n\r\nthere") == "Hello there"

    def test_an_empty_subject_is_refused(self):
        with pytest.raises(ValueError, match="subject"):
            build(subject="   ")

    def test_a_malformed_recipient_is_refused(self):
        with pytest.raises(ValueError, match="recipient"):
            build(to_email="not-an-address")


class TestRequiredHeaders:
    def test_our_own_date_and_message_id_are_set(self):
        """Without them a message threads badly, scores worse with filters, and cannot be found
        again — which the crash-recovery path depends on."""
        built = build()
        assert built.message["Date"] is not None
        assert built.message["Message-ID"] is not None
        assert built.message_id.startswith("<")

    def test_a_pre_generated_message_id_is_used_verbatim(self):
        """The outbox assigns it before the send so a crash can be resolved by searching the
        mailbox for that exact id. The same value must be on the wire and in the database."""
        assigned = "<fixed-id-123@outreach.example>"
        built = build(message_id=assigned)
        assert built.message_id == assigned
        assert built.message["Message-ID"] == assigned

    def test_the_from_header_carries_a_display_name(self):
        built = build()
        assert "Asha Rao" in str(built.message["From"])
        assert "asha@outreach.example" in str(built.message["From"])

    def test_reply_to_is_set_when_configured(self):
        assert build().message["Reply-To"] == "asha@outreach.example"

    def test_reply_to_is_omitted_when_not_configured(self):
        bare = Mailbox(from_name="Asha", from_email="asha@outreach.example")
        assert build(mailbox=bare).message["Reply-To"] is None

    def test_the_generated_message_id_uses_the_sending_domain(self):
        built = build(message_id=None)
        assert built.message_id.endswith("@outreach.example>")


class TestUnsubscribe:
    def test_the_list_unsubscribe_header_is_always_present(self):
        """A header the model cannot influence is a header that is always there."""
        assert build().message["List-Unsubscribe"] == (
            "<mailto:asha@outreach.example?subject=unsubscribe>"
        )

    def test_one_click_is_not_advertised_without_a_url_to_post_to(self):
        """**The bug this replaced, and the test that asserted it.**

        `List-Unsubscribe-Post: List-Unsubscribe=One-Click` is RFC 8058, and RFC 8058 is about an
        `https` URI a receiver can POST to. Sending that header beside a `mailto:` announces a
        capability we do not have -- and unlike most wrong headers, this one is *testable by the
        receiver*: Gmail can POST, get nothing, and conclude the sender misrepresents itself. A
        missing feature is better than a false claim about one.
        """
        assert build().message["List-Unsubscribe-Post"] is None

    def test_one_click_is_advertised_once_there_is_a_url(self):
        built = build(unsubscribe_url="https://api.example.com/u/abc123")

        assert built.message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_the_url_comes_first_and_the_mailto_stays(self):
        """Both targets, https first.

        RFC 8058 wants the https URI, and the mailto is kept because it needs no endpoint to be
        reachable and a reply to it lands in the inbox the IMAP poller already watches. Order
        matters: a receiver takes the first one it understands.
        """
        header = build(unsubscribe_url="https://api.example.com/u/abc123").message[
            "List-Unsubscribe"
        ]

        assert header == (
            "<https://api.example.com/u/abc123>, "
            "<mailto:asha@outreach.example?subject=unsubscribe>"
        )

    def test_a_non_https_unsubscribe_url_is_refused(self):
        """`http` would have a receiver POST an opt-out in clear, and RFC 8058 requires https."""
        with pytest.raises(ValueError):
            build(unsubscribe_url="http://api.example.com/u/abc123")

    def test_it_is_a_mailto_so_no_endpoint_has_to_exist(self):
        """A reply lands in the inbox the IMAP poller already watches, so an opt-out is caught
        by machinery that is already running."""
        target = unsubscribe_mailto(MAILBOX)
        assert target.startswith("<mailto:")
        assert "subject=unsubscribe" in target


class TestFooter:
    def test_it_states_who_sent_this_and_how_to_stop(self):
        footer = build_footer(IDENTITY)
        assert "Asha Rao" in footer
        assert "Acme Software" in footer
        assert "no thanks" in footer

    def test_the_postal_address_is_included_when_given(self):
        assert "MG Road" in build_footer(IDENTITY)

    def test_it_works_without_an_address_or_url(self):
        minimal = FooterIdentity(sender_name="Asha Rao", agency_name="Acme Software")
        footer = build_footer(minimal)
        assert "Asha Rao" in footer
        assert "no thanks" in footer

    def test_the_wording_is_overridable_without_a_code_change(self):
        """So a lawyer's revision does not need a deploy."""
        reviewed = FooterIdentity(
            sender_name="Asha",
            agency_name="Acme",
            opt_out_line="Reply STOP to be removed from this list.",
        )
        assert "Reply STOP" in build_footer(reviewed)

    def test_the_footer_is_appended_to_every_message(self):
        built = build()
        assert "no thanks" in built.body
        assert built.body.index("no thanks") > built.body.index("Worth a short call")

    def test_a_draft_cannot_omit_it(self):
        """The whole reason the footer is built here rather than by the model."""
        built = build(body="Hi Ben, call me.")
        assert "Acme Software" in built.body
        assert "no thanks" in built.body


class TestPlainText:
    def test_the_message_is_plain_text_only(self):
        """B14: no HTML path means nothing to escape, and deliverability improves."""
        built = build()
        assert built.message.get_content_type() == "text/plain"
        assert built.message.get_content_charset() == "utf-8"
        assert not built.message.is_multipart()

    def test_no_tracking_headers_are_added(self):
        """§9.7: no open pixel, no link rewriting. Privacy, and it hurts deliverability."""
        rendered = build().as_bytes().decode().lower()
        for marker in ("x-mailer-track", "list-id", "x-campaign"):
            assert marker not in rendered

    def test_unicode_survives_intact(self):
        built = build(body="Hi Ben,\n\nNamaste 🙏 — worth a call?\n\nAsha")
        assert "🙏" in built.message.get_content()

    def test_the_body_is_not_wrapped_in_markup(self):
        content = build().message.get_content()
        assert "<br>" not in content
        assert "<html" not in content.lower()


class TestThreading:
    def test_a_reply_carries_in_reply_to_and_references(self):
        """Without these a follow-up looks like a fresh cold email instead of continuing the
        conversation."""
        built = build(
            in_reply_to="<their-reply@globex.example>",
            references="<ours@outreach.example> <their-reply@globex.example>",
        )
        assert built.message["In-Reply-To"] == "<their-reply@globex.example>"
        assert "ours@outreach.example" in str(built.message["References"])

    def test_an_initial_email_carries_neither(self):
        built = build()
        assert built.message["In-Reply-To"] is None
        assert built.message["References"] is None

    def test_a_references_chain_starts_from_the_first_message(self):
        assert build_references(None, "<first@x.example>") == "<first@x.example>"

    def test_the_chain_is_extended(self):
        chain = build_references("<a@x.example>", "<b@x.example>")
        assert chain == "<a@x.example> <b@x.example>"

    def test_a_long_chain_keeps_the_first_and_the_last_few(self):
        """Clients thread on those; fifty ids waste header space for nothing."""
        existing = " ".join(f"<m{i}@x.example>" for i in range(20))
        chain = build_references(existing, "<new@x.example>").split()
        assert len(chain) == 6
        assert chain[0] == "<m0@x.example>"
        assert chain[-1] == "<new@x.example>"

    def test_header_injection_through_threading_headers_is_stripped(self):
        built = build(in_reply_to="<id@x.example>\r\nBcc: attacker@evil.example")
        assert "\n" not in str(built.message["In-Reply-To"])
