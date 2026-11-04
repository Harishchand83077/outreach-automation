"""A job board's host is not a company's domain (ADR-047).

Revision ID: 8d1a4f60c3b7
Revises: 4c7e2a9b50d3
Create Date: 2026-11-04

One new column and **one correction to existing data**, which makes this the first migration in
this project that rewrites rows rather than only adding to them. It is worth saying why that is
the safe direction here.

`job_posts.company_domain` was filled with `urlsplit(apply_url).hostname`, and for every posting
that arrived through a feed that host is the applicant tracking system's --
`job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`. Three callers read it and all
three acted on it: a contact lookup searched the ATS vendor on the user's own credit, an imported
contact became a lead whose `company_url` was the vendor and which the research pipeline then
fetched and cited, and the referral matcher looked for connections working at "boards".

So those values are **not data that happens to be stale**; they are wrong, and every use of them
spends money or says something untrue about a company. Leaving them would mean the fix applied to
postings fetched after the deploy and not to the ones already in the table, which is the worst of
both: the same screen would behave two ways depending on when a row arrived.

**Upgrade.**

1. `board_slug` is added, nullable, and backfilled from the first path segment of `apply_url` for
   the rows whose host is one of the three boards we poll. That segment is the company's own
   identifier on the board -- `vercel`, `leverdemo`, `ramp` -- checked against the real trimmed
   feed responses in `tests/fixtures/feeds/` rather than taken from documentation.
2. `company_domain` is set to NULL wherever it holds a board host. NULL makes a caller ask; a
   vendor host makes three callers confident and wrong.

The host list here is deliberately **narrower than `boards.BOARD_HOSTS`**: only the hosts that
could actually have been written by the code as it stood, which is the three feed sources plus the
pasted postings a user may have supplied. Matching the full application list in SQL would risk
blanking a domain that is genuinely a company's, for a vendor nothing ever stored.

`contact_candidates.company_domain` is deliberately **not** rewritten. Those rows are proposals a
person may already have read and dismissed, and some were created from a domain somebody typed
rather than from a posting -- so there is no way in SQL to tell a wrong one from a right one. The
import route refuses a board host instead, which fails loudly at the one moment it matters.

**Downgrade** drops `board_slug` and does not restore the vendor hosts into `company_domain`.
That is a deliberate one-way door: putting them back would re-arm a lookup that bills the user
for contacts at Greenhouse and a research run that drafts an email about a job board. A revert
leaves those rows with no domain, which every caller already handles by asking.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.services.jobposts.boards import NOT_A_SLUG

revision = "8d1a4f60c3b7"
down_revision = "4c7e2a9b50d3"
branch_labels = None
depends_on = None

SCHEMA = "app"

#: Only the hosts the intake could have written. See the module docstring.
STORED_BOARD_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
)

# `NOT_A_SLUG` is imported from `boards`, so the backfill and the parser cannot disagree
# about what a slug is -- the same device the suppression reasons and the mailbox weights use.


def upgrade() -> None:
    op.add_column(
        "job_posts", sa.Column("board_slug", sa.String(length=200)), schema=SCHEMA
    )

    hosts = ", ".join(f"'{host}'" for host in STORED_BOARD_HOSTS)
    not_a_slug = ", ".join(f"'{segment}'" for segment in sorted(NOT_A_SLUG))

    # `split_part(path, '/', 2)` is the first segment, because a path beginning with `/` makes
    # the first part empty. Computed from `apply_url` rather than from `company_domain`, since
    # the slug lives in the path and not in the host.
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.job_posts
               SET board_slug = lower(
                       split_part(
                           split_part(
                               regexp_replace(apply_url, '^https?://[^/]+', ''),
                               '?', 1
                           ),
                           '/', 2
                       )
                   )
             WHERE company_domain IN ({hosts})
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.job_posts
               SET board_slug = NULL
             WHERE board_slug = '' OR board_slug IN ({not_a_slug})
            """
        )
    )
    op.execute(
        sa.text(
            f"UPDATE {SCHEMA}.job_posts SET company_domain = NULL "
            f"WHERE company_domain IN ({hosts})"
        )
    )


def downgrade() -> None:
    # The vendor hosts are deliberately not restored. See the module docstring.
    op.drop_column("job_posts", "board_slug", schema=SCHEMA)
