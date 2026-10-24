"""What may be attached to an outgoing email, decided without touching a database or a disk.

One campaign, one file, attached to every email that campaign sends unless somebody unchecks it
for a particular draft. That is the whole feature, and almost all of the thinking here is about
the ways a file attached to a cold email can hurt the person sending it.

**Three rules, and the reasons they are rules rather than suggestions.**

1. **The client's filename never reaches a filesystem.** It never reaches one anyway -- the
   bytes live in Postgres, so there is no path to traverse and the whole class of defect that
   `../../etc/passwd` belongs to simply has nowhere to happen here. The name is still sanitised,
   because it travels in a MIME header and in the recipient's download dialog, and a newline in
   it is header injection exactly as it is in a subject line.

2. **The type is decided by us, from an allowlist, not by what the uploader claims.** A browser
   sends a `Content-Type` it guessed from the extension; trusting it means a recipient's client
   is told `application/pdf` about something that is not a PDF. We ignore the claim, take the
   extension, and look it up. An extension we do not know is refused rather than sent as
   `application/octet-stream`: "I do not know what this is, here it is anyway" is not a position
   to take about a file one is mailing to strangers.

3. **Nothing executable, ever.** Not as a convenience, and not because an allowlist is tidier:
   `.exe`, `.js`, `.vbs`, `.scr` and friends in an unsolicited email are the oldest malware
   delivery mechanism there is, and a system that will send one on request is a system that will
   eventually be asked to. Office macro formats (`.docm`, `.xlsm`, `.pptm`) are refused for the
   same reason and are not merely "not on the list" -- `_REFUSED` names them so the error can say
   *why* rather than "unsupported file type", which reads like a gap rather than a decision.

**We do not scan for malware, and `PRIVACY.md` and the API error both say so rather than
implying a check that does not exist.** There is no scanner on this box and adding one is new
infrastructure. The operator is uploading their own deck to their own campaign, so the threat is
not really "the operator attacks their recipients" -- but it is worth being honest that the only
thing standing between a compromised laptop and a thousand mailed copies of something nasty is
the type allowlist above.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Five megabytes. Gmail and most providers accept 25 MB *after* base64 encoding inflates the
#: payload by a third, so this is nowhere near the protocol ceiling -- it is a deliverability
#: number. A cold email carrying a 20 MB file is a cold email that gets filed as spam, and the
#: attachment is also sent again on every retry and stored on our side for the life of the
#: campaign.
MAX_BYTES = 5 * 1024 * 1024

#: Extension -> the `Content-Type` we will state. Documents and images only: the things somebody
#: actually attaches to an outreach email (a deck, a one-pager, a case study, a CV).
#:
#: `.doc`/`.xls`/`.ppt` -- the pre-2007 binary formats -- are deliberately absent. They carry
#: macros in a container that cannot be told apart from a document without parsing it, which is
#: the same objection as `.docm`.
ALLOWED: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

#: Refused *by name*, so the message can say why. Everything not in `ALLOWED` is refused anyway;
#: these are the ones somebody will reasonably try, and deserve a reason rather than a shrug.
_REFUSED: dict[str, str] = {
    ".exe": "an executable",
    ".msi": "an installer",
    ".bat": "a script",
    ".cmd": "a script",
    ".sh": "a script",
    ".ps1": "a script",
    ".js": "a script",
    ".vbs": "a script",
    ".jar": "a program",
    ".scr": "an executable",
    ".docm": "a macro-enabled document",
    ".xlsm": "a macro-enabled document",
    ".pptm": "a macro-enabled document",
    ".doc": "an old binary Office format that can carry macros",
    ".xls": "an old binary Office format that can carry macros",
    ".ppt": "an old binary Office format that can carry macros",
    ".zip": "an archive, whose contents nothing here can inspect",
    ".rar": "an archive, whose contents nothing here can inspect",
    ".7z": "an archive, whose contents nothing here can inspect",
}

#: Anything that is not a plain, printable filename character. Collapsed to `_` rather than
#: stripped, so two different uploads cannot sanitise down to the same name silently.
_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]+")
_DOTS = re.compile(r"\.{2,}")


class AttachmentRejected(ValueError):
    """The file may not be attached. The message is shown to the operator verbatim."""


@dataclass(frozen=True)
class CheckedFile:
    """A file that has passed every rule and may be stored."""

    filename: str
    content_type: str
    content: bytes
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def safe_filename(raw: str) -> str:
    """A filename safe to put in a MIME header and in front of a recipient.

    Leading dots go: a name beginning `.` is hidden on Unix and is never what somebody meant to
    send. Directory separators go with the rest of the unsafe characters, so `../../x.pdf`
    becomes `.._.._x.pdf` and then loses its leading dots -- not because a path could be
    traversed here (nothing writes to a disk) but because the name is wrong and a wrong name
    should not be quietly mailed out.
    """
    name = (raw or "").strip().replace("\\", "/").rpartition("/")[2]
    name = _UNSAFE.sub("_", name)
    name = _DOTS.sub(".", name).lstrip(". ").strip()
    if len(name) > 120:
        stem, _, ext = name.rpartition(".")
        name = f"{stem[: 119 - len(ext)]}.{ext}" if ext else name[:120]
    return name


def extension_of(filename: str) -> str:
    _, dot, ext = filename.rpartition(".")
    return f".{ext.lower()}" if dot else ""


def check(raw_filename: str, content: bytes) -> CheckedFile:
    """Validate an upload, or raise `AttachmentRejected` saying what is wrong with it.

    Order matters: emptiness first (the commonest accident and the least interesting message),
    then size, then type. Reporting "unsupported type" about a 40 MB file whose real problem is
    that it is 40 MB would send somebody to rename it.
    """
    name = safe_filename(raw_filename)
    if not name:
        raise AttachmentRejected("the file has no usable name")
    if not content:
        raise AttachmentRejected(f"{name} is empty")
    if len(content) > MAX_BYTES:
        mb = len(content) / (1024 * 1024)
        raise AttachmentRejected(
            f"{name} is {mb:.1f} MB; the limit is {MAX_BYTES // (1024 * 1024)} MB. "
            "A large attachment on a cold email is a deliverability problem before it is a "
            "storage one — link to it instead."
        )

    ext = extension_of(name)
    if not ext:
        raise AttachmentRejected(f"{name} has no file extension, so its type cannot be decided")
    if reason := _REFUSED.get(ext):
        raise AttachmentRejected(
            f"{ext} is {reason} and is never attached to an outgoing email. "
            "Nothing here scans file contents, so the file type is the only control there is."
        )
    content_type = ALLOWED.get(ext)
    if content_type is None:
        raise AttachmentRejected(
            f"{ext} is not an allowed attachment type. Allowed: " + ", ".join(sorted(ALLOWED))
        )

    return CheckedFile(
        filename=name,
        content_type=content_type,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def should_attach(*, draft_override: bool | None, campaign_default: bool) -> bool:
    """Whether this particular draft carries the campaign's file.

    `None` on the draft means "nobody has expressed an opinion about this email", which is the
    state every draft starts in -- so the campaign's setting decides. An explicit `False` from a
    reviewer who unticked the box has to survive the draft being superseded by a later version,
    which is why this is a stored column rather than a decision made at send time from the
    campaign alone.
    """
    return campaign_default if draft_override is None else draft_override
