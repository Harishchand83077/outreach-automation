"""The deployment, seen from above the workspaces (ADR-024).

**Why this cannot be an admin route.** `SIGNUP_MODE=open` gives every signup its own workspace
(ADR-017), and that is the product -- one person must not see another's campaigns. So the person
who *runs* the deployment has no screen at all: every admin route answers for exactly one org,
correctly, and "how many workspaces are there, and are their mailboxes still authenticating?" is
not a question any of them can be asked.

**What it may show, and what it may never show.** Counts, health and configuration. Not a lead,
not a draft, not a reply, not a campaign name. The distinction is not squeamishness: a workspace
owner consented to this system sending their mail, not to its operator reading their pipeline, and
an aggregate view that shipped contents would be a tenancy failure with a nicer heading.

Three things keep that honest, and none of them is good intentions:

1. `CurrentOperator` has **no `org_id` and no `ctx`**, so nothing here can construct the
   `OrgContext` that every repository call demands. The repository is not imported.
2. `tests/unit/test_platform_scope_structure.py` greps this file for the column names that would
   carry somebody's content, and fails if one appears.
3. `tests/integration/test_platform_scope.py` writes a lead, a draft and a campaign with
   distinctive strings and asserts none of them appears anywhere in the response text -- which
   catches a field added later in a place nobody thought to look.

Everything here is read-only. There is no write route in this module and no route that takes an
org from its caller: the whole point of the scope is that it reports, and the things worth
changing about a workspace belong to the people in it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from sqlalchemy import Select, func, select

from app.api.deps import OperatorDep, SessionDep
from app.core.settings import config
from app.db.models import (
    Alert,
    Campaign,
    Job,
    Lead,
    Mailbox,
    MailCredential,
    Member,
    Message,
    Org,
    SearchUsage,
    WorkerHeartbeat,
)
from app.db.models import LlmUsage as Llm
from app.services.llm.errors import Outcome

router = APIRouter(tags=["platform"])

#: Tavily's free plan. Reported so a month that is about to run out is visible before it does.
SEARCH_BUDGET = 1_000


def _counts_by_org(column: Any) -> Select[tuple[Any, int]]:
    """`SELECT org_id, count(*) ... GROUP BY org_id` over one table.

    One statement per table rather than a pile of correlated subqueries on `orgs`: at this size
    either is instant, and a flat group-by is the version somebody can read in a year.
    """
    return select(column, func.count()).group_by(column)


async def _tally(session: Any, statement: Select[tuple[Any, int]]) -> dict[str, int]:
    rows = (await session.execute(statement)).all()
    return {str(key): int(total) for key, total in rows if key is not None}


@router.get("/platform/overview")
async def platform_overview(operator: OperatorDep, session: SessionDep) -> dict[str, Any]:
    """Every workspace, in aggregate, plus the health of the deployment underneath them.

    `operator` is taken as a dependency rather than ignored even though nothing below reads it:
    taking it *is* the gate, and a route that resolved the identity and then discarded the
    parameter would be one refactor away from losing the check entirely.
    """
    now = datetime.now(UTC)

    orgs = (await session.scalars(select(Org).order_by(Org.created_at))).all()

    members = await _tally(session, _counts_by_org(Member.org_id))
    campaigns = await _tally(session, _counts_by_org(Campaign.org_id))
    running = await _tally(
        session,
        _counts_by_org(Campaign.org_id).where(Campaign.status == "running"),
    )
    leads = await _tally(session, _counts_by_org(Lead.org_id))
    mailboxes = await _tally(session, _counts_by_org(Mailbox.org_id))
    sent = await _tally(
        session,
        # `'out'`, not `'outbound'` -- `ck_messages_direction` is the authority, and the first
        # version of this line guessed. It counted zero for every workspace for ever, which is
        # indistinguishable from a deployment that has sent nothing, and no test here would have
        # noticed. `test_a_sent_message_is_counted` exists because of it.
        _counts_by_org(Message.org_id).where(Message.direction == "out", Message.status == "sent"),
    )

    # A mailbox counts as connected when it holds a credential the provider has not yet refused.
    # The *reason* it was refused is read here and never returned: that text can quote a
    # provider's reply, and this response reaches a browser and a screenshot.
    connected = await _tally(
        session,
        _counts_by_org(MailCredential.org_id).where(MailCredential.invalid_reason.is_(None)),
    )

    workspaces = [
        {
            "id": str(org.id),
            # The workspace's own name, which is how an operator tells one row from another.
            # Nothing inside it.
            "name": org.name,
            "created_at": org.created_at.isoformat(),
            "paused": org.paused,
            # NULL means "follow the environment" (ADR-010), which is a different state from any
            # of the three modes and is reported as such rather than flattened into one.
            "send_mode": org.send_mode or "environment",
            "dataset_pooling_opt_in": org.dataset_pooling_opt_in,
            "members": members.get(str(org.id), 0),
            "mailboxes": mailboxes.get(str(org.id), 0),
            "mailboxes_connected": connected.get(str(org.id), 0),
            "campaigns": campaigns.get(str(org.id), 0),
            "campaigns_running": running.get(str(org.id), 0),
            "leads": leads.get(str(org.id), 0),
            "emails_sent": sent.get(str(org.id), 0),
        }
        for org in orgs
    ]

    # **The number that actually matters on this screen.** A workspace with a mailbox that no
    # longer authenticates has silently stopped sending, and stopped fetching replies with it --
    # which is invisible from inside, because the dashboard looks exactly the same.
    #
    # Computed from the tallies rather than by reading back the response rows: those are a
    # heterogeneous dict, so the comparison would be against `object`, and casting to make a
    # type checker happy is how a real mix-up gets waved through later.
    stalled = [
        str(org.id)
        for org in orgs
        if mailboxes.get(str(org.id), 0) > 0 and connected.get(str(org.id), 0) == 0
    ]

    dead_jobs = await session.scalar(
        select(func.count()).select_from(Job).where(Job.status == "dead")
    )
    alerts_open = await session.scalar(
        select(func.count()).select_from(Alert).where(Alert.acked_at.is_(None))
    )
    last_seen = await session.scalar(select(func.max(WorkerHeartbeat.last_seen_at)))

    since_day = now - timedelta(days=1)
    since_month = now - timedelta(days=30)

    llm_calls = await session.scalar(
        select(func.count()).select_from(Llm).where(Llm.at >= since_day)
    )
    llm_tokens = await session.scalar(
        select(func.coalesce(func.sum(Llm.tokens_in + Llm.tokens_out), 0)).where(
            Llm.at >= since_day
        )
    )
    # `Outcome.SUCCESS` rather than a copied string: the first version of this line said
    # "ok", which is not a value the enum has, so every successful call was counted as a
    # failure. Reading the authority costs one import and cannot drift from it.
    llm_failures = await session.scalar(
        select(func.count())
        .select_from(Llm)
        .where(Llm.at >= since_day, Llm.outcome != Outcome.SUCCESS.value)
    )
    search_credits = await session.scalar(
        select(func.coalesce(func.sum(SearchUsage.credits), 0)).where(SearchUsage.at >= since_month)
    )

    return {
        "workspaces": workspaces,
        "totals": {
            "workspaces": len(orgs),
            "members": sum(members.values()),
            "mailboxes": sum(mailboxes.values()),
            "mailboxes_connected": sum(connected.values()),
            "campaigns": sum(campaigns.values()),
            "leads": sum(leads.values()),
            "emails_sent": sum(sent.values()),
        },
        "health": {
            "dead_jobs": int(dead_jobs or 0),
            "alerts_open": int(alerts_open or 0),
            "worker_last_seen_at": last_seen.isoformat() if last_seen else None,
            "worker_seen_seconds_ago": (
                int((now - last_seen).total_seconds()) if last_seen else None
            ),
            # Named rather than counted, because the operator's next action is to tell those
            # workspaces to reconnect -- and a count gives them nobody to tell.
            "workspaces_with_no_working_mailbox": stalled,
        },
        "usage": {
            "llm_calls_24h": int(llm_calls or 0),
            "llm_tokens_24h": int(llm_tokens or 0),
            "llm_failures_24h": int(llm_failures or 0),
            "search_credits_30d": int(search_credits or 0),
            "search_budget": SEARCH_BUDGET,
        },
        "deployment": {
            "app_env": config.APP_ENV,
            "signup_mode": config.SIGNUP_MODE,
            "max_orgs": config.MAX_ORGS,
            "grant_days": config.GOOGLE_GRANT_DAYS,
            "google_oauth_configured": config.google_oauth_configured,
        },
        "you": {"email": operator.email},
    }
