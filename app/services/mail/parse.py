"""Parsing inbound mail: what kind of message is this, and what did the human actually write?

`ARCHITECTURE_TARGET.md` §10.2. Pure functions over raw bytes, so every case can be tested from
a fixture rather than by provoking a real mail server.

Three jobs, in order of importance:

1. **Decide the message *type* before reading its content.** A bounce and an out-of-office are
   not replies, and treating them as such is how the prototype marked a dead address as engaged
   (issues A8, D2). The type comes from headers, which the sender cannot easily fake into
   looking human.
2. **Strip quoted text before classifying.** Otherwise the classifier reads our own email back
   and happily finds enthusiasm in it.
3. **Decode charsets properly.** A reply in Hindi or with a Windows-1252 apostrophe must survive;
   the prototype decoded with `errors="replace"` against whatever it guessed and produced
   mojibake it then fed to a model.
"""

from __future__ import annotations

import email
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from enum import StrEnum

#: Headers that mark a message as machine-generated. Checked before any content, because this is
#: the one signal a sender does not usually bother to forge.
AUTO_HEADERS: tuple[str, ...] = (
    "auto-submitted",
    "x-autoreply",
    "x-autorespond",
    "x-auto-response-suppress",
    "x-mailer-daemon",
)

#: Phrases that mean "I am not here", in the languages this audience actually uses.
OOO_PHRASES: tuple[str, ...] = (
    "out of office",
    "out of the office",
    "automatic reply",
    "auto reply",
    "autoreply",
    "away from my desk",
    "on leave",
    "on vacation",
    "on holiday",
    "annual leave",
    "maternity leave",
    "paternity leave",
    "currently travelling",
    "currently traveling",
    "limited access to email",
    "will respond when i return",
)

#: Envelope senders used by mail systems rather than people.
DAEMON_LOCALS: frozenset[str] = frozenset(
    {
        "mailer-daemon",
        "postmaster",
        "no-reply",
        "noreply",
        "donotreply",
        "do-not-reply",
        "bounce",
        "bounces",
        "mail-daemon",
    }
)

#: Quote markers. "On ... wrote:" is the common one; the rest are client-specific blocks.
_QUOTE_STARTERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*On\s.{0,120}\swrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*On\s.{0,120},\s.{0,80}\swrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE),
    re.compile(r"^\s*>+\s?.*$"),
)

#: A signature delimiter. RFC 3676 says "-- " exactly; clients are sloppy, so allow "--".
_SIGNATURE = re.compile(r"^\s*--\s*$")

_HTML_BLOCK = re.compile(r"<(script|style|head)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_BREAK = re.compile(r"<(br|/p|/div|/tr|/li)\b[^>]*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_WS_RUN = re.compile(r"[ \t]{2,}")
_BLANK_RUN = re.compile(r"\n{3,}")


class MessageType(StrEnum):
    """What kind of thing arrived. Decided from headers, before content."""

    HUMAN = "human"
    #: A delivery status notification. Hard means the address is dead.
    BOUNCE_HARD = "bounce_hard"
    #: A temporary failure: full mailbox, server down. Not a reason to suppress.
    BOUNCE_SOFT = "bounce_soft"
    AUTO_REPLY = "auto_reply"


@dataclass
class ParsedMessage:
    """One inbound message, parsed and ready to match against a lead."""

    message_type: MessageType
    from_addr: str = ""
    to_addr: str = ""
    subject: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: list[str] = field(default_factory=list)
    date: datetime | None = None
    #: The whole body, stored for the audit trail.
    body_full: str = ""
    #: Quoted text and signature removed. This is what gets classified.
    body_reply_only: str = ""
    #: For a bounce: the address that failed and the status code, if we could find them.
    failed_recipient: str = ""
    dsn_status: str = ""
    #: For an out-of-office: when they said they would be back.
    return_date: datetime | None = None

    @property
    def is_bounce(self) -> bool:
        return self.message_type in (MessageType.BOUNCE_HARD, MessageType.BOUNCE_SOFT)

    @property
    def threading_ids(self) -> list[str]:
        """Every id this message claims to be part of, newest first.

        Matching tries `In-Reply-To` before `References`, because the former names the message
        being answered while the latter is the whole thread.
        """
        ids = [self.in_reply_to] if self.in_reply_to else []
        return ids + [ref for ref in self.references if ref and ref != self.in_reply_to]


def decode_text(value: str | None) -> str:
    """Decode an RFC 2047 header into plain text.

    Subjects routinely arrive as `=?UTF-8?B?...?=`, and a classifier reading that literally has
    no idea what it is looking at.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def address_of(value: str | None) -> str:
    """The bare address from a header, lower-cased."""
    if not value:
        return ""
    _, addr = email.utils.parseaddr(decode_text(value))
    return addr.strip().lower()


def _header_ids(value: str | None) -> list[str]:
    """Extract `<...>` ids from a threading header."""
    if not value:
        return []
    return re.findall(r"<[^<>\s]+>", value)


def html_to_text(html: str) -> str:
    """Flatten HTML into readable text.

    Needed because plenty of people reply from a client that sends HTML only. Script and style
    contents are dropped first — otherwise a tracking blob ends up quoted as though a human had
    typed it.
    """
    without_blocks = _HTML_BLOCK.sub(" ", html or "")
    with_breaks = _HTML_BREAK.sub("\n", without_blocks)
    text = _HTML_TAG.sub(" ", with_breaks)
    # A handful of entities carry meaning; the rest are noise at this stage.
    for entity, char in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&apos;", "'"),
    ):
        text = text.replace(entity, char)
    return tidy(text)


def tidy(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _WS_RUN.sub(" ", cleaned)
    cleaned = "\n".join(line.rstrip() for line in cleaned.split("\n"))
    return _BLANK_RUN.sub("\n\n", cleaned).strip()


def strip_quoted(body: str) -> str:
    """Keep only what this person wrote, dropping the quoted thread and signature.

    This matters more than it looks. With the quote left in, the classifier reads our own
    enthusiastic pitch and classifies the reply as interested — the model is not wrong, it is
    just reading the wrong text.

    Conservative on purpose: if stripping would leave nothing, the original is returned. A
    one-line reply above an unrecognised quote block is better classified from the whole thing
    than from an empty string.
    """
    lines = (body or "").split("\n")
    kept: list[str] = []

    for line in lines:
        if any(pattern.match(line) for pattern in _QUOTE_STARTERS):
            break
        if _SIGNATURE.match(line):
            break
        kept.append(line)

    result = tidy("\n".join(kept))
    return result or tidy(body)


def extract_body(message: Message) -> tuple[str, str]:
    """Pull the best available text out of a message. Returns (full, reply-only).

    Prefers `text/plain`, falls back to flattened HTML. Attachments are ignored: a reply's
    meaning is in its text, and a PDF is not something to feed a model.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []

    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in disposition.lower():
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain":
                plain_parts.append(_part_text(part))
            elif content_type == "text/html":
                html_parts.append(_part_text(part))
    else:
        content_type = message.get_content_type()
        if content_type == "text/html":
            html_parts.append(_part_text(message))
        else:
            plain_parts.append(_part_text(message))

    if plain_parts:
        full = tidy("\n\n".join(p for p in plain_parts if p.strip()))
    else:
        full = html_to_text("\n".join(html_parts))

    return full, strip_quoted(full)


def _part_text(part: Message) -> str:
    """Decode one part, honouring its declared charset.

    `errors="replace"` only as a last resort: the prototype used it unconditionally against a
    guessed charset and produced mojibake, which it then sent to a model as though it were text.
    """
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        # A part with no decodable payload — a container, or already-decoded text.
        content = part.get_payload()
        return content if isinstance(content, str) else ""

    charsets = [part.get_content_charset(), "utf-8", "cp1252", "latin-1"]
    for charset in charsets:
        if not charset:
            continue
        try:
            return payload.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


def detect_bounce(message: Message, from_addr: str) -> tuple[MessageType | None, str, str]:
    """Is this a delivery failure? Returns (type, failed recipient, status code).

    Hard versus soft matters a great deal: a hard bounce means the address does not exist and
    must be suppressed forever, while a soft bounce is a full mailbox or a server having a bad
    afternoon, and suppressing on that would throw away a real lead.
    """
    content_type = (message.get_content_type() or "").lower()
    report_type = str(message.get_param("report-type") or "").lower()
    local = from_addr.partition("@")[0]

    is_dsn = content_type == "multipart/report" and report_type == "delivery-status"
    looks_like_daemon = local in DAEMON_LOCALS
    subject = decode_text(message.get("Subject")).lower()
    subject_suggests = any(
        marker in subject
        for marker in (
            "undeliverable",
            "delivery status notification",
            "returned mail",
            "delivery failure",
            "mail delivery failed",
            "failure notice",
        )
    )

    if not (is_dsn or (looks_like_daemon and subject_suggests)):
        return None, "", ""

    status, recipient = _read_dsn(message)

    # 5.x.x is permanent, 4.x.x is temporary (RFC 3463).
    if status.startswith("5"):
        return MessageType.BOUNCE_HARD, recipient, status
    if status.startswith("4"):
        return MessageType.BOUNCE_SOFT, recipient, status

    # No machine-readable status. Fall back to the wording, erring toward *soft*: a wrongly
    # soft-bounced lead is retried, while a wrongly hard-bounced one is suppressed forever.
    body, _ = extract_body(message)
    lowered = body.lower()
    if any(
        phrase in lowered
        for phrase in (
            "user unknown",
            "no such user",
            "does not exist",
            "address rejected",
            "recipient address rejected",
            "mailbox unavailable",
            "invalid recipient",
        )
    ):
        return MessageType.BOUNCE_HARD, recipient, status
    return MessageType.BOUNCE_SOFT, recipient, status


def _read_dsn(message: Message) -> tuple[str, str]:
    """Read `Status` and `Final-Recipient` out of the machine-readable DSN part."""
    status = ""
    recipient = ""
    for part in message.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        text = _part_text(part) or ""
        if not text:
            payload = part.get_payload()
            if isinstance(payload, list):
                text = "\n".join(str(sub) for sub in payload)
        for line in text.split("\n"):
            lowered = line.lower()
            if lowered.startswith("status:") and not status:
                status = line.partition(":")[2].strip()
            elif lowered.startswith("final-recipient:") and not recipient:
                recipient = line.partition(";")[2].strip().lower()
            elif lowered.startswith("original-recipient:") and not recipient:
                recipient = line.partition(";")[2].strip().lower()
    return status, recipient


def detect_auto_reply(message: Message, body: str) -> tuple[bool, datetime | None]:
    """Is this an out-of-office or similar? Returns (is auto, return date if stated).

    Treating one as a real reply is doubly wrong: it cancels the follow-up for a lead who never
    read the email, and it puts a robot's message in front of a human reviewer.
    """
    for header in AUTO_HEADERS:
        value = str(message.get(header) or "").strip().lower()
        if value and value not in ("no", "false"):
            return True, _find_return_date(body)

    precedence = str(message.get("Precedence") or "").strip().lower()
    if precedence in ("auto_reply", "bulk", "junk", "list"):
        return True, _find_return_date(body)

    subject = decode_text(message.get("Subject")).lower()
    haystack = f"{subject}\n{(body or '')[:1500].lower()}"
    if any(phrase in haystack for phrase in OOO_PHRASES):
        return True, _find_return_date(body)

    return False, None


def _find_return_date(body: str) -> datetime | None:
    """Best-effort "back on 14 October" extraction.

    Only used to push the follow-up out sensibly, so a miss costs a default delay rather than
    anything important.
    """
    if not body:
        return None

    patterns = (
        r"(?:back|return(?:ing)?|available)\s+(?:on|from)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)(?:\s+(\d{4}))?",
        r"(?:back|return(?:ing)?|available)\s+(?:on|from)\s+"
        r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
    )
    months = {
        m: i
        for i, name in enumerate(
            [
                "january",
                "february",
                "march",
                "april",
                "may",
                "june",
                "july",
                "august",
                "september",
                "october",
                "november",
                "december",
            ],
            start=1,
        )
        for m in (name, name[:3])
    }

    for pattern in patterns:
        match = re.search(pattern, body, re.IGNORECASE)
        if not match:
            continue
        a, b, year = match.group(1), match.group(2), match.group(3)
        try:
            if a.isdigit():
                day, month_name = int(a), b.lower()
            else:
                day, month_name = int(b), a.lower()
            month = months.get(month_name)
            if month is None:
                continue
            resolved_year = int(year) if year else datetime.now(tz=UTC).year
            return datetime(resolved_year, month, day, tzinfo=UTC)
        except (ValueError, TypeError):
            continue
    return None


def parse_message(raw: bytes) -> ParsedMessage:
    """Parse one raw message into something the reply engine can act on."""
    message = email.message_from_bytes(raw)

    from_addr = address_of(message.get("From"))
    to_addr = address_of(message.get("To"))
    subject = decode_text(message.get("Subject"))

    date: datetime | None = None
    if message.get("Date"):
        try:
            parsed = parsedate_to_datetime(str(message.get("Date")))
            date = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            date = None

    body_full, body_reply = extract_body(message)

    bounce_type, failed_recipient, status = detect_bounce(message, from_addr)
    if bounce_type is not None:
        message_type = bounce_type
        return_date = None
    else:
        is_auto, return_date = detect_auto_reply(message, body_full)
        message_type = MessageType.AUTO_REPLY if is_auto else MessageType.HUMAN
        failed_recipient, status = "", ""

    in_reply_to_ids = _header_ids(message.get("In-Reply-To"))

    return ParsedMessage(
        message_type=message_type,
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        message_id=(_header_ids(message.get("Message-ID")) or [""])[0],
        in_reply_to=in_reply_to_ids[0] if in_reply_to_ids else "",
        references=_header_ids(message.get("References")),
        date=date,
        body_full=body_full,
        body_reply_only=body_reply,
        failed_recipient=failed_recipient,
        dsn_status=status,
        return_date=return_date,
    )


def normalised_subject(subject: str) -> str:
    """Strip reply and forward prefixes, for the fallback match.

    Clients add these in many languages and sometimes stack them, so `Re: Re: Fwd: Hello` has to
    reduce to `hello` or the fallback never matches anything.
    """
    text = decode_text(subject).strip()
    prefix = re.compile(r"^\s*(re|fw|fwd|aw|sv|antw|res|rif|vs)\s*(\[\d+\])?\s*:\s*", re.IGNORECASE)
    while True:
        stripped = prefix.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return re.sub(r"\s+", " ", text).strip().lower()
