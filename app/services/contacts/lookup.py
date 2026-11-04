"""Running one contact lookup, on a button press, and writing what it proposed (F9, ADR-041).

**This function is reached from exactly one place: a route a person called.** There is no job
kind for it, the `Scheduler` has no pass that calls it, and
`tests/unit/test_contacts_button_press_only.py` asserts both over the source. That is not
fastidiousness about layout -- a provider lookup spends the user's own money, and the difference
between a feature that costs what somebody chose to spend and one that quietly bills them in the
background is whether anything other than a click can start it.

**Three refusals before any network call**, in the order that costs least:

1. No key for that provider, or a key the provider already refused. Calling with a key we know is
   dead spends a request to be told what we recorded last time.
2. This workspace is over its daily lookup budget.
3. No domain to look up. A lookup on an empty domain is one credit for nothing.

**A proposal is not a lead.** Nothing written here is in `leads`, so nothing here can be sent.
The import path is a separate, explicit action that goes through the ordinary suppression check.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import CONTACT_PROVIDERS, ContactCandidate, Event, JobPost
from app.db.repo import OrgContext
from app.db.state import record_event
from app.services import credentials
from app.services.contacts import keys, providers
from app.services.jobposts import boards, company

logger = logging.getLogger(__name__)

#: Lookups one workspace may run per day. A ceiling on *our* side of the user's own quota: a
#: runaway loop in a script of theirs would otherwise spend a month's credits in an afternoon,
#: and the provider's bill does not care that it was an accident. Same reasoning as the company
#: cache's fetch budget.
DAILY_LOOKUP_BUDGET = 25


class LookupRefused(Exception):
    """A lookup did not run, with a sentence saying why and what to do about it."""


@dataclass(frozen=True)
class LookupResult:
    provider: str
    domain: str
    proposed: list[ContactCandidate]
    #: How many the provider returned that were already known, so the screen can say "nothing
    #: new" rather than showing an empty list that looks like a failure.
    already_known: int


def _norm_domain(raw: str | None) -> str:
    domain = (raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        domain = domain.removeprefix(prefix)
    domain = domain.split("/")[0].removeprefix("www.")
    return domain


#: The audit type for one lookup. **It is also the counter**, which is the decision worth
#: recording: `events` is append-only with a trigger refusing UPDATE and DELETE, so a count of
#: these rows cannot drift. A counter column incremented at call time can, and the mailbox
#: health rollup already chose a recomputation over a counter for exactly that reason -- a crash
#: between the call and the increment leaves a counter wrong for ever with nothing noticing.
#:
#: It also means the budget and the trail are the same fact. A lookup that spent a credit and
#: left no trace would be both an accounting hole and an audit one.
LOOKUP_EVENT = "contact.lookup"


async def used_today(
    session: AsyncSession, *, org_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Lookups this workspace has run in the last 24 hours.

    A rolling window rather than a calendar day, and deliberately: a midnight reset in one
    timezone is a free second allowance for anybody who waits for it, and this budget exists to
    stop a loop rather than to apportion a day.
    """
    moment = now or datetime.now(tz=UTC)
    since = moment - timedelta(days=1)
    used = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.org_id == org_id, Event.type == LOOKUP_EVENT, Event.at >= since)
    )
    return int(used or 0)


async def run(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    provider: str,
    domain: str | None = None,
    job_post_id: uuid.UUID | None = None,
    role_hint: str | None = None,
    client: httpx.AsyncClient | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> LookupResult:
    """One lookup. Raises `LookupRefused` for anything a person can fix."""
    moment = now or datetime.now(tz=UTC)

    post: JobPost | None = None
    if job_post_id is not None:
        post = await session.scalar(
            select(JobPost).where(JobPost.id == job_post_id, JobPost.org_id == ctx.org_id)
        )
        if post is None:
            # Scoped rather than 403: an id from another workspace resolves to nothing instead
            # of confirming it exists (ADR-004).
            raise LookupRefused("that job posting does not exist in this workspace.")

    target = _norm_domain(domain)
    if not target and post is not None:
        # **Resolved, never taken from the posting's host.** A feed's apply URL names the
        # applicant tracking system, so this used to search for people who work at Greenhouse,
        # on the user's own credit (ADR-047).
        target = _norm_domain((await company.resolve(session, post)).domain)
    if not target or "." not in target:
        raise LookupRefused(
            "a lookup needs a company domain, and this posting does not record one: its apply "
            "link points at the job board rather than the company. Type the company's domain."
        )
    if boards.is_board_url(f"https://{target}"):
        # Reachable when somebody types it, or pastes it from the posting's own link. Refused
        # rather than run, because the lookup would succeed and bill them for contacts at an
        # applicant tracking system.
        raise LookupRefused(
            f"{target} is a job board rather than a company, so a lookup there would spend a "
            "credit on the wrong people. Type the company's own domain."
        )

    credential = await keys.live(session, ctx, provider=provider, allowed=CONTACT_PROVIDERS)
    if credential is None:
        raise LookupRefused(
            f"this workspace has no {provider} key. Add your own under Settings; there is no "
            "shared key, by design."
        )
    if credential.invalid_reason is not None:
        # The stored sentence is deliberately *not* quoted: it can carry a provider's reply.
        raise LookupRefused(
            f"the {provider} key was refused the last time it was used. Save it again, or check "
            "the plan and credit on your account."
        )

    if await used_today(session, org_id=ctx.org_id, now=moment) >= DAILY_LOOKUP_BUDGET:
        raise LookupRefused(
            f"this workspace has used its {DAILY_LOOKUP_BUDGET} lookups for today. The limit is "
            "ours, not the provider's: it exists so a loop cannot spend your credits."
        )

    adapter = providers.build(provider, credentials.unseal(credential.secret, settings))

    owned = client is None
    http = client or httpx.AsyncClient(timeout=providers.TIMEOUT_SECONDS, follow_redirects=True)
    try:
        found = await adapter.find(domain=target, role_hint=role_hint, client=http)
    except providers.ProviderUnauthorised as exc:
        await keys.mark_invalid(session, credential_id=credential.id, reason=str(exc))
        # Committed, or the refusal is rolled back with the exception and the next press calls
        # the provider again with the key it just told us was dead.
        await session.commit()
        raise LookupRefused(str(exc)) from exc
    except providers.ProviderError as exc:
        logger.warning("contact lookup failed", extra={"provider": provider, "domain": target})
        raise LookupRefused(str(exc)) from exc
    finally:
        if owned:
            await http.aclose()

    credential.last_used_at = moment
    await keys.clear_invalid(session, credential_id=credential.id)

    # **Written before the candidates, and not wrapped.** Everywhere else in this system an
    # audit write is wrapped and never fatal, because the work is the point and the record of it
    # is not. Here the record *is* the budget: a lookup that spent one of the user's credits and
    # failed to say so would let the next one spend another, and the ledger would understate
    # what the key has been used for. So this one is allowed to fail the request.
    await record_event(
        session,
        org_id=ctx.org_id,
        type=LOOKUP_EVENT,
        actor="user",
        actor_id=ctx.actor_id,
        data={"provider": adapter.name, "domain": target, "returned": len(found)},
    )

    proposed: list[ContactCandidate] = []
    known = 0
    for contact in found:
        email_norm = (contact.email or "").strip().lower() or None
        if email_norm is not None:
            existing = await session.scalar(
                select(ContactCandidate).where(
                    ContactCandidate.org_id == ctx.org_id,
                    ContactCandidate.email_norm == email_norm,
                )
            )
            if existing is not None:
                # Already proposed, imported or dismissed. A second lookup must not resurrect
                # somebody the person already said no to.
                known += 1
                continue
        row = ContactCandidate(
            org_id=ctx.org_id,
            job_post_id=post.id if post else None,
            company_domain=target,
            provider=adapter.name,
            full_name=contact.full_name,
            title=contact.title,
            email=contact.email,
            email_norm=email_norm,
            email_status=contact.email_status,
            confidence=contact.confidence,
            profile_url=contact.profile_url,
            state="proposed",
        )
        session.add(row)
        proposed.append(row)

    await session.flush()
    return LookupResult(
        provider=adapter.name, domain=target, proposed=proposed, already_known=known
    )


async def dismiss(session: AsyncSession, ctx: OrgContext, *, candidate_id: uuid.UUID) -> bool:
    """Say no to a proposal. It stays, so the same person is not proposed again next week."""
    row = await session.scalar(
        select(ContactCandidate).where(
            ContactCandidate.id == candidate_id, ContactCandidate.org_id == ctx.org_id
        )
    )
    if row is None:
        return False
    if row.state == "imported":
        raise LookupRefused("that contact is already a lead. Delete the lead instead.")
    row.state = "dismissed"
    await session.flush()
    return True
