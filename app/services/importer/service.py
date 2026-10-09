"""Import in two steps: preview, then commit.

The user sees the header mapping and every row's verdict *before* anything is written (§7A).
That separation is the point: an import decides who gets emailed, so a wrong column or a
misparsed address must be caught by a person, not discovered by a recipient.

`preview` is pure except for optional DNS. `commit` re-validates from scratch rather than
trusting a preview handed back to it — a preview is advisory, and anything that decides
recipients is re-checked against the database at the moment of writing.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo
from app.db.models import Lead
from app.db.repo import OrgContext
from app.services.importer import sheet
from app.services.importer.headers import HeaderMapping, build_full_name, map_headers
from app.services.importer.validation import (
    MxResolver,
    NullMxResolver,
    RowVerdict,
    ValidatedRow,
    check_company_url,
    check_email,
    check_linkedin_url,
)


@dataclass
class ImportPreview:
    """Everything the user needs to decide whether to go ahead."""

    mapping: HeaderMapping
    rows: list[ValidatedRow]
    file_sha256: str
    filename: str
    truncated: int = 0
    #: Domains appearing more than once, with their row numbers. Several people at one company
    #: is normal and useful to see, not an error.
    duplicate_domains: dict[str, list[int]] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            verdict.value: sum(1 for r in self.rows if r.verdict is verdict)
            for verdict in RowVerdict
        }

    @property
    def importable_rows(self) -> list[ValidatedRow]:
        return [r for r in self.rows if r.importable]

    @property
    def can_commit(self) -> bool:
        return self.mapping.is_importable and bool(self.importable_rows)


def _cell(row: list[object], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def preview(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    data: bytes,
    filename: str,
    campaign_id: uuid.UUID,
    mx: MxResolver | None = None,
) -> ImportPreview:
    """Parse and validate an upload without writing anything.

    Raises `sheet.SheetError` if the file cannot be read at all. Individual bad rows do not
    raise — they come back with a verdict and a reason, because the user needs to see them.
    """
    mx = mx or NullMxResolver()
    parsed = sheet.parse(data, filename)
    mapping = map_headers(parsed.headers)

    column = {field_name: index for index, field_name in mapping.mapped.items()}
    rows: list[ValidatedRow] = []
    #: Within-file duplicates. The database's UNIQUE(campaign_id, email_norm) is the real
    #: guarantee; catching them here means the user sees which rows collided.
    seen_emails: dict[str, int] = {}
    domains: dict[str, list[int]] = {}

    for offset, raw_row in enumerate(parsed.rows):
        # +2: row 1 is the header, and spreadsheet rows are 1-based, so this is the number the
        # user sees in Excel.
        row = ValidatedRow(row_number=offset + 2)

        check_email(row, _cell(raw_row, column.get("email")) or "", mx=mx)

        row.full_name = _cell(raw_row, column.get("full_name")) or build_full_name(mapping, raw_row)
        if not row.full_name:
            row.warn("no name: the greeting will have to be generic", flag="no_name")

        row.company_name = _cell(raw_row, column.get("company_name"))
        if not row.company_name:
            row.reject("company_name is empty")

        check_company_url(row, _cell(raw_row, column.get("company_url")))
        check_linkedin_url(row, _cell(raw_row, column.get("linkedin_url")))
        row.job_title = _cell(raw_row, column.get("job_title"))
        row.notes = _cell(raw_row, column.get("notes"))

        # Unrecognised columns are kept so research and drafting can use them (issue A5).
        row.extra = {
            mapping.headers[index]: text
            for index, _ in mapping.unmapped.items()
            if (text := _cell(raw_row, index)) is not None
        }

        if row.email_norm:
            if row.email_norm in seen_emails:
                row.reject(f"duplicate of row {seen_emails[row.email_norm]} in this file")
            else:
                seen_emails[row.email_norm] = row.row_number

            if await _already_in_campaign(session, ctx, campaign_id, row.email_norm):
                row.reject("already imported into this campaign")
            elif await _is_suppressed(session, ctx, row.email_norm):
                row.reject("on the suppression list — opted out or bounced previously")

            if row.domain:
                domains.setdefault(row.domain, []).append(row.row_number)

        rows.append(row)

    duplicate_domains = {d: numbers for d, numbers in domains.items() if len(numbers) > 1}
    for domain, numbers in duplicate_domains.items():
        for row in rows:
            if row.domain == domain and row.importable:
                row.warn(
                    f"{len(numbers)} people share the domain {domain}",
                    flag="duplicate_domain",
                )

    return ImportPreview(
        mapping=mapping,
        rows=rows,
        file_sha256=hashlib.sha256(data).hexdigest(),
        filename=filename,
        truncated=parsed.truncated,
        duplicate_domains=duplicate_domains,
    )


async def commit(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    data: bytes,
    filename: str,
    campaign_id: uuid.UUID,
    mx: MxResolver | None = None,
) -> tuple[list[Lead], ImportPreview]:
    """Re-validate and insert the importable rows. Returns the new leads and the final preview.

    Re-validating rather than accepting a preview from the caller is deliberate: a preview could
    be stale (someone may have opted out since) or forged. Anything that decides who receives
    email is checked against the database at the moment of writing.

    The caller commits the transaction, so an import lands atomically with whatever else it is
    paired with — the enrich jobs, in the API's case.
    """
    result = await preview(
        session, ctx, data=data, filename=filename, campaign_id=campaign_id, mx=mx
    )
    if not result.mapping.is_importable:
        missing = ", ".join(result.mapping.missing_required) or "none"
        raise sheet.SheetError(
            f"Cannot import: unmapped required column(s): {missing}. "
            "Confirm the column mapping first."
        )

    leads: list[Lead] = []
    for row in result.importable_rows:
        assert row.email_norm is not None  # guaranteed by `importable`
        lead = Lead(
            org_id=ctx.org_id,
            campaign_id=campaign_id,
            email_norm=row.email_norm,
            email_raw=row.email_raw or row.email_norm,
            full_name=row.full_name,
            company_name=row.company_name,
            company_url=row.company_url,
            domain=row.domain,
            job_title=row.job_title,
            linkedin_url=row.linkedin_url,
            notes=row.notes,
            extra=row.extra,
            state="NEW",
            flags=row.flags,
        )
        session.add(lead)
        leads.append(lead)

    await session.flush()
    return leads, result


async def _already_in_campaign(
    session: AsyncSession, ctx: OrgContext, campaign_id: uuid.UUID, email: str
) -> bool:
    from sqlalchemy import select

    found = await session.scalar(
        select(Lead.id)
        .where(
            Lead.org_id == ctx.org_id,
            Lead.campaign_id == campaign_id,
            Lead.email_norm == email,
        )
        .limit(1)
    )
    return found is not None


async def _is_suppressed(session: AsyncSession, ctx: OrgContext, email: str) -> bool:
    from app.db.outbox import is_suppressed

    return await is_suppressed(session, org_id=ctx.org_id, email=email)


__all__ = ["ImportPreview", "commit", "preview", "repo"]
