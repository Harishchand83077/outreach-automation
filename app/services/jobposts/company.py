"""Which company a posting is for, answered honestly or not at all (ADR-047).

One function, because three callers need the answer and each of them spends something on it: a
contact lookup spends a provider credit, an import spends a research fetch and a model call, and
a referral note spends the user's credibility with somebody they know. Three copies of this rule
would be this project's signature defect in the places where it costs most.

**It resolves or it refuses.** There is no fallback that invents a domain from a slug: `vercel`
is not `vercel.com` often enough to rely on, `ramp` is not `ramp.com` for every Ramp, and a
guess here is a credit spent on the wrong company and an email about them. Hard rule 6 forbids
inventing facts about the sender; this is the same rule pointed at the recipient, and ADR-011 is
what it costs when the key is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CompanyProfile, JobPost


@dataclass(frozen=True)
class CompanyRef:
    """What we can say about the employer behind a posting.

    `domain` is None whenever we do not know it. `label` is always something a person can read,
    which is what a screen needs -- but it is never passed off as a domain, and the two are
    separate fields precisely so a caller cannot use one as the other by accident.
    """

    domain: str | None
    label: str
    #: How the domain was established, in words, for a screen and for a reviewer: `posting` when
    #: the apply URL was the company's own site, `board slug` when `company_profiles` knew which
    #: domain uses that slug, and `unknown` when nobody does.
    via: str

    @property
    def known(self) -> bool:
        return self.domain is not None


async def resolve(session: AsyncSession, post: JobPost) -> CompanyRef:
    """The employer behind one posting.

    Three steps, cheapest first and none of them networked:

    1. **The posting's own domain**, which is set only when the apply URL was not a board's.
    2. **The shared public-company cache**, by `(ats_type, ats_slug)`. F19 records those per
       domain for exactly this reason, and it is the one place a slug becomes a domain -- a fact
       some workspace established from a careers page, not a guess.
    3. **Nothing**, with the slug as a readable label so a screen can name the posting and a
       person can supply the domain in one field.
    """
    if post.company_domain:
        return CompanyRef(domain=post.company_domain, label=post.company_domain, via="posting")

    slug = (post.board_slug or "").strip().lower()
    if slug and post.source in {"greenhouse", "lever", "ashby"}:
        # Shared across workspaces, which is safe here for ADR-038's reason: an ATS slug and a
        # domain are public facts about a company, and this reads them rather than writing any.
        profile = await session.scalar(
            select(CompanyProfile).where(
                CompanyProfile.ats_type == post.source, CompanyProfile.ats_slug == slug
            )
        )
        if profile is not None and profile.domain:
            return CompanyRef(
                domain=profile.domain,
                label=profile.name or profile.domain,
                via="board slug",
            )

    return CompanyRef(domain=None, label=slug or post.title, via="unknown")
