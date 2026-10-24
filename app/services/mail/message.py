"""Building the email itself.

`ARCHITECTURE_TARGET.md` §9.7. Plain text, no tracking, correct headers, and a footer the sender
adds rather than the model.

The footer is built here on purpose. An opt-out line the model might paraphrase, forget or
reword is not a compliance control — it has to be identical on every message, and it has to be
impossible for a draft to omit. Same for `List-Unsubscribe`: a header the model cannot influence
is a header that is always present.

The prototype had none of this (issues D1, D5): no unsubscribe mechanism, no `List-Unsubscribe`,
no `Message-ID` or `Date` of its own, no `Reply-To`, and an HTML body built by replacing
newlines with `<br>` without escaping anything (B14).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

#: Characters that would let a header value inject another header. Stripped, never escaped:
#: there is no legitimate reason for a newline inside a subject or an address.
_CRLF = re.compile(r"[\r\n\x00]+")
#: Collapse the whitespace a stripped newline leaves behind.
_SPACES = re.compile(r"[ \t]{2,}")


def sanitise_header(value: str) -> str:
    """Make a string safe to put in a header.

    Header injection is the attack: a newline in a subject lets everything after it be read as
    a new header by the receiving server, which is how a `Bcc` gets added to someone else's
    email.
    """
    cleaned = _CRLF.sub(" ", value or "")
    return _SPACES.sub(" ", cleaned).strip()


@dataclass
class Mailbox:
    """The sending identity, as the message builder needs it."""

    from_name: str
    from_email: str
    reply_to: str | None = None


@dataclass(frozen=True)
class Attachment:
    """A file to carry, already checked and typed by `app/domain/attachment.py`.

    Declared here rather than imported from the database layer so that `build_message` stays a
    pure function over plain values -- it is the most heavily tested thing in this package and
    nothing it needs should require a session to construct.
    """

    filename: str
    content_type: str
    content: bytes


@dataclass
class FooterIdentity:
    """What the footer must say about who we are and how to stop.

    Not legal advice — `docs/COMPLIANCE.md` records what the system does so a lawyer can review
    it. But an honest sender identity and a working opt-out are the two things every regime
    agrees on.
    """

    sender_name: str
    agency_name: str
    agency_url: str | None = None
    postal_address: str | None = None
    #: Overridable so the wording can be reviewed without touching code.
    opt_out_line: str = 'Not relevant? Reply with "no thanks" and I won\'t email you again.'


def build_footer(identity: FooterIdentity) -> str:
    """The block appended to every outbound email.

    Deliberately plain. A recipient should be able to tell in one glance who sent this and how
    to make it stop, without hunting for a pale grey link.
    """
    lines = ["--", identity.sender_name]
    if identity.agency_name:
        lines.append(identity.agency_name)
    if identity.agency_url:
        lines.append(identity.agency_url)
    if identity.postal_address:
        lines.append(identity.postal_address)
    lines.append("")
    lines.append(identity.opt_out_line)
    return "\n".join(lines)


def unsubscribe_mailto(mailbox: Mailbox) -> str:
    """The `List-Unsubscribe` target.

    A mailto rather than a URL: it needs no endpoint to exist, it works in every client, and a
    reply lands in the inbox the IMAP poller already watches, so an opt-out is detected by
    machinery that is already there. §9.7 notes a signed one-click URL as a later option.
    """
    return f"<mailto:{mailbox.from_email}?subject=unsubscribe>"


@dataclass
class BuiltMessage:
    """A message ready to hand to SMTP, with the identifiers needed to track it."""

    message: EmailMessage
    message_id: str
    subject: str
    body: str

    def as_bytes(self) -> bytes:
        return self.message.as_bytes()


def build_message(
    *,
    mailbox: Mailbox,
    to_email: str,
    subject: str,
    body: str,
    identity: FooterIdentity,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: Sequence[Attachment] = (),
    now: datetime | None = None,
) -> BuiltMessage:
    """Assemble one plain-text email, optionally carrying files.

    `message_id` is passed in rather than generated here, because the outbox assigns it *before*
    the send so a crash can be resolved by searching the mailbox for that exact id (§9.5). The
    same identifier has to appear on the wire and in the database.

    **An attachment changes the shape of the message and nothing else about it.** With no
    attachment this is `text/plain`, byte for byte what it has always been; with one it becomes
    `multipart/mixed` whose *first* part is that same `text/plain`. The locked product rule is
    plain text only, no HTML body, and that still holds -- there is no `text/html` alternative
    here, and a client that cannot read MIME still shows the text. What changes is only that
    something is carried alongside it.
    """
    now = now or datetime.now(tz=UTC)
    clean_subject = sanitise_header(subject)
    if not clean_subject:
        raise ValueError("a message must have a subject")

    clean_to = sanitise_header(to_email)
    if "@" not in clean_to:
        raise ValueError(f"{to_email!r} is not a usable recipient address")

    message = EmailMessage()
    message["Subject"] = clean_subject
    message["From"] = Address(
        display_name=sanitise_header(mailbox.from_name), addr_spec=mailbox.from_email
    )
    message["To"] = clean_to
    # Our own Date and Message-ID rather than whatever the relay invents: without them a
    # message is harder to thread, scores worse with filters, and cannot be found again.
    message["Date"] = format_datetime(now)
    message["Message-ID"] = message_id or make_msgid(
        domain=mailbox.from_email.rpartition("@")[2] or None
    )

    if mailbox.reply_to:
        message["Reply-To"] = sanitise_header(mailbox.reply_to)

    # Threading. A follow-up or a reply that carries these lands in the same conversation
    # instead of looking like a fresh cold email.
    if in_reply_to:
        message["In-Reply-To"] = sanitise_header(in_reply_to)
    if references:
        message["References"] = sanitise_header(references)

    message["List-Unsubscribe"] = unsubscribe_mailto(mailbox)
    # Tells a client the mailto is safe to trigger without a confirmation step.
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # Marks this as solicited-style bulk mail rather than a transactional message.
    message["Auto-Submitted"] = "no"

    full_body = f"{body.rstrip()}\n\n{build_footer(identity)}\n"
    message.set_content(full_body, subtype="plain", charset="utf-8")

    for item in attachments:
        maintype, _, subtype = item.content_type.partition("/")
        if not maintype or not subtype:
            raise ValueError(f"{item.content_type!r} is not a usable MIME type")
        # `set_content` above made this a text/plain message; the first `add_attachment` turns
        # it into multipart/mixed and keeps that text as part one. The filename is sanitised
        # again here rather than trusted from the caller: it goes into a header and into the
        # recipient's download dialog, and being sure costs one function call.
        message.add_attachment(
            item.content,
            maintype=maintype,
            subtype=subtype,
            filename=sanitise_header(item.filename),
        )

    return BuiltMessage(
        message=message,
        message_id=str(message["Message-ID"]),
        subject=clean_subject,
        body=full_body,
    )


def build_references(previous_references: str | None, replying_to: str) -> str:
    """Extend a `References` chain without letting it grow without limit.

    Keeping the first and the last few is the conventional compromise: clients thread on them,
    and a chain of fifty ids wastes header space for nothing.
    """
    existing = (previous_references or "").split()
    chain = [*existing, replying_to]
    if len(chain) <= 6:
        return " ".join(chain)
    return " ".join([chain[0], *chain[-5:]])
