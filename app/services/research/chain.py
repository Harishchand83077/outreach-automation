"""An ordered chain of search providers, stopping at the first one that answers (F17, ADR-046).

The shape is deliberately `LLMRouter`'s, because the problem is the same one: several providers,
each with a free tier, any of which may be down or out of credit, tried in an order the operator
chose. Reusing that shape rather than inventing a second one means there is one answer to "what
happens when a provider fails?" in this system.

**The order is the feature.** A chain of three keys stops at the first provider that answers, so
whichever is cheapest or most generous goes first and the rest are never billed. Two properties
make that worth having rather than merely tidy:

- **A hit short-circuits.** The second member is not called, so it is not charged.
- **A cache hit calls nobody at all** -- that is `search_cache`, consulted before this.

**Every key is the workspace's own**, read from `provider_credentials` exactly as F9's contact
lookups are (ADR-041). The deployment's `TAVILY_API_KEY` is supported as the **last** member and
is the only entry that is not somebody's own: its free tier is shared by every workspace using
it, which is the reason to add your own key and is said in those words on the screen.

**A failure is not an empty answer**, which is the distinction the single-provider version could
not express and the reason `SearchProvider.search` now raises. "Nobody writes about this company"
stops the chain and is cached; "this provider is down" moves to the next member and is never
cached. One provider reporting an outage as zero results would poison a week of lookups for a
company that is simply quiet -- the job-post intake's lesson in a more expensive place.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import SEARCH_PROVIDERS, ProviderCredential
from app.db.repo import OrgContext
from app.services import credentials
from app.services.contacts import keys
from app.services.research.search import (
    ADAPTERS,
    SearchBudget,
    SearchHit,
    SearchKeyRefused,
    SearchProvider,
    SearchUnavailable,
)

logger = logging.getLogger(__name__)

#: The deployment-wide Tavily entry's name in a chain. It is the provider name with a suffix
#: rather than a bare `tavily`, because the two are different accounts and `search_usage` has to
#: be able to say which one a credit came out of: a workspace's own thousand credits and the
#: deployment's shared thousand are not one allowance.
DEPLOYMENT_MEMBER = "tavily:deployment"

#: What an attempt records when a member was skipped because its own monthly ceiling is spent.
#: A **named constant** because the research service compares against it to decide what to tell
#: a reviewer, and a literal in a producer matched by a literal in a consumer that cannot see it
#: change is this project's single most repeated defect.
CEILING_SPENT = "monthly ceiling spent"

#: The provider name a cached answer reports. Not one of `SEARCH_PROVIDERS`, deliberately: the
#: thing that answered was this system, and recording a provider that was never called would
#: make the usage ledger and the answer disagree about who was billed.
CACHE_MEMBER = "cache"


@dataclass(frozen=True)
class Attempt:
    """One member's turn, recorded whether it answered or not.

    Kept for every member tried, including the ones that failed, because "we searched and found
    nothing" and "every provider we have was down" are different sentences for a reviewer and
    the research result says which one happened.
    """

    provider: str
    hits: int = 0
    error: str | None = None

    @property
    def answered(self) -> bool:
        return self.error is None


@dataclass
class ChainAnswer:
    """What the chain produced, and what it cost to produce."""

    hits: list[SearchHit] = field(default_factory=list)
    #: The member that answered, or None when none of them could.
    provider: str | None = None
    attempts: list[Attempt] = field(default_factory=list)
    from_cache: bool = False

    @property
    def answered(self) -> bool:
        """Whether a provider actually answered, found something or not.

        **Not the same as having hits**, and this is the property the cache keys on: a provider
        that answered with nothing is an answer worth storing, while a chain that could not
        reach anybody is not.
        """
        return self.provider is not None

    @property
    def spent(self) -> int:
        """Credits this answer cost. Zero for a cache hit, and zero when nobody answered."""
        return sum(1 for attempt in self.attempts if attempt.answered)


@dataclass
class ChainMember:
    """One provider in the chain, with the credential it spends.

    `credential_id` is None for the deployment entry, which has no row to mark invalid -- its key
    is in the environment and a route cannot fix it.
    """

    provider: SearchProvider
    credential_id: uuid.UUID | None = None
    #: What `search_usage` records for this member, which for the deployment entry is not the
    #: bare provider name.
    usage_name: str = ""

    def __post_init__(self) -> None:
        if not self.usage_name:
            self.usage_name = self.provider.name


async def build_chain(
    session: AsyncSession,
    ctx: OrgContext,
    *,
    client: httpx.AsyncClient,
    settings: Settings | None = None,
) -> list[ChainMember]:
    """This workspace's chain, in order: its own keys first, the deployment entry last.

    The order within a workspace's own keys is `SEARCH_PROVIDERS`. A per-workspace order is a
    column and a screen and is deliberately not built yet: with a chain of at most three, the
    order that matters is "mine before the shared one", and that is the one implemented.

    A key the provider has already refused is **left out rather than tried**, the same refusal
    `lookup.run` makes before a contact lookup: calling with a key we recorded as dead spends a
    request to be told what we wrote down last time.
    """
    rows = {
        row.provider: row
        for row in (
            await session.scalars(
                select(ProviderCredential).where(
                    ProviderCredential.org_id == ctx.org_id,
                    ProviderCredential.provider.in_(SEARCH_PROVIDERS),
                    ProviderCredential.revoked_at.is_(None),
                )
            )
        ).all()
    }

    members: list[ChainMember] = []
    for name in SEARCH_PROVIDERS:
        row = rows.get(name)
        if row is None or row.invalid_reason is not None:
            continue
        adapter = ADAPTERS.get(name)
        if adapter is None:  # pragma: no cover - ADAPTERS covers SEARCH_PROVIDERS by test
            continue
        members.append(
            ChainMember(
                provider=adapter(credentials.unseal(row.secret, settings), client),
                credential_id=row.id,
            )
        )

    deployment_key = str(getattr(settings, "TAVILY_API_KEY", "") or "").strip()
    if deployment_key:
        members.append(
            ChainMember(
                provider=ADAPTERS["tavily"](deployment_key, client),
                credential_id=None,
                usage_name=DEPLOYMENT_MEMBER,
            )
        )
    return members


class SearchChain:
    """Tries members in order and stops at the first that answers.

    It satisfies `SearchProvider`, so the research service calls it exactly as it called a single
    provider. That is why F17 needed no change to the pipeline: the interface the chain hides
    behind is the one that was already there.
    """

    name = "chain"

    def __init__(
        self,
        members: list[ChainMember],
        *,
        session: AsyncSession,
        org_id: uuid.UUID,
        budget: SearchBudget | None = None,
    ) -> None:
        self._members = members
        self._session = session
        self._org_id = org_id
        self._budget = budget

    def __len__(self) -> int:
        return len(self._members)

    async def run(
        self,
        query: str,
        *,
        max_results: int = 3,
        domain: str | None = None,
        lead_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> ChainAnswer:
        """One question, asked of as few providers as possible.

        A member is skipped without being called when its own monthly ceiling is spent, so a
        workspace whose first key is exhausted falls through to its second rather than stopping.
        """
        moment = now or datetime.now(tz=UTC)
        answer = ChainAnswer()

        for member in self._members:
            name = member.usage_name
            if self._budget is not None and not await self._budget.can_spend(
                1, provider=name, now=moment
            ):
                answer.attempts.append(Attempt(provider=name, error=CEILING_SPENT))
                continue

            try:
                hits = await member.provider.search(query, max_results=max_results)
            except SearchKeyRefused as exc:
                # Recorded against the credential, so the next research job leaves this member
                # out instead of paying a request to be refused again. The deployment entry has
                # no row to mark, which is one of the reasons a workspace should bring its own.
                if member.credential_id is not None:
                    await keys.mark_invalid(
                        self._session, credential_id=member.credential_id, reason=str(exc)
                    )
                logger.warning("search provider refused the key", extra={"provider": name})
                answer.attempts.append(Attempt(provider=name, error=str(exc)))
                continue
            except SearchUnavailable as exc:
                logger.info("search provider unavailable", extra={"provider": name})
                answer.attempts.append(Attempt(provider=name, error=str(exc)))
                continue

            # It answered. The credit is spent whether or not it found anything, so it is
            # recorded either way -- a provider billing for a zero-result query and a ledger
            # that only counts hits would drift apart, and the ledger is what the screen shows.
            answer.attempts.append(Attempt(provider=name, hits=len(hits)))
            if self._budget is not None:
                await self._budget.record(provider=name, credits=1, domain=domain, lead_id=lead_id)
            if member.credential_id is not None:
                await keys.clear_invalid(self._session, credential_id=member.credential_id)

            answer.provider = name
            answer.hits = hits
            if hits:
                # **The short circuit.** Nothing after this member is called, so nothing after it
                # is charged.
                return answer

        return answer

    async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]:
        """The `SearchProvider` face of it, for a caller that only wants the hits."""
        return (await self.run(query, max_results=max_results)).hits
