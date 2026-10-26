"""Integration-test harness: a real Postgres, migrated with the real migrations.

These tests exist because the parts they cover cannot be tested any other way. `FOR UPDATE SKIP
LOCKED`, a unique constraint under concurrency, an append-only trigger and `ON CONFLICT DO
NOTHING` are database behaviour; a fake would only assert that the fake works.

The database is created fresh, migrated with `alembic upgrade head` — so the migrations
themselves are exercised on every run — and truncated between tests. It is a separate database
from development, never the same one.

Start Postgres with:  docker compose -f deploy/compose.yml up -d
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _derive_test_url() -> str:
    """Same server and credentials as `DATABASE_URL`, but the `_test` database.

    Derived rather than spelled out, so the password lives in exactly one place and the test URL
    cannot drift onto a different server than the one intended — including, per ADR-005, onto
    port 5432 where another project's Postgres may be listening.
    """
    from app.core.settings import config

    parts = urlsplit(config.DATABASE_URL)
    name = parts.path.lstrip("/") or "outreach"
    suffix = "" if name.endswith("_test") else "_test"
    return urlunsplit(parts._replace(path=f"/{name}{suffix}"))


#: Override only to point CI at its own service container.
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL") or _derive_test_url()

#: Every table the migrations create, child-first, for truncation.
_TABLES = (
    "worker_heartbeats",
    "alerts",
    "suppressions",
    "messages",
    "drafts",
    "jobs",
    "events",
    "leads",
    "campaigns",
    "mailboxes",
    "orgs",
)


def _maintenance_url(url: str) -> tuple[str, str]:
    """Split a URL into (url pointing at `postgres`, target database name)."""
    parts = urlsplit(url)
    db_name = parts.path.lstrip("/")
    return urlunsplit(parts._replace(path="/postgres")), db_name


async def _database_exists_or_create() -> None:
    admin_url, db_name = _maintenance_url(TEST_DATABASE_URL)
    # AUTOCOMMIT: CREATE DATABASE cannot run inside a transaction.
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(
                text("select 1 from pg_database where datname = :n"), {"n": db_name}
            )
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await engine.dispose()


def _run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    # Set explicitly so env.py does not fall back to the development URL from settings.
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _migrated_database() -> AsyncIterator[None]:
    try:
        await _database_exists_or_create()
    except Exception as exc:  # pragma: no cover - environment, not logic
        pytest.skip(
            "no Postgres for integration tests "
            f"({type(exc).__name__}). Start it with: "
            "docker compose -f deploy/compose.yml up -d"
        )
    # env.py calls asyncio.run(), which cannot nest inside this fixture's running loop,
    # so the migration runs on its own thread with its own loop.
    await asyncio.to_thread(_run_migrations)
    yield


@pytest_asyncio.fixture(loop_scope="session")
async def db(_migrated_database: None) -> AsyncIterator[AsyncSession]:
    """A session on the test database, with every table emptied afterwards.

    TRUNCATE rather than a rollback, because several tests deliberately commit — a claim that is
    rolled back proves nothing about SKIP LOCKED.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as session:
            yield session
    finally:
        async with engine.begin() as conn:
            # The append-only trigger blocks DELETE, so events are cleared with the explicit
            # escape hatch the migration provides.
            await conn.execute(text("SET LOCAL app.allow_event_delete = 'on'"))
            await conn.execute(
                text("TRUNCATE TABLE " + ", ".join(f"app.{t}" for t in _TABLES) + " CASCADE")
            )
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def org(db: AsyncSession):
    from app.db.models import Org

    # **Just an org.** There is no deployment-wide mailbox any more (ADR-023), so there is
    # nothing here that distinguishes "the operator's workspace" from anybody else's -- which is
    # the whole point of that change, and is why this fixture lost its one special field.
    record = Org(name="Test Agency")
    db.add(record)
    await db.commit()
    return record


#: The key the test harness seals mailbox credentials with. Generated per run, so nothing here
#: is a value anybody could paste into a real deployment.
TEST_CREDENTIAL_KEY = Fernet.generate_key().decode("ascii")


def mail_settings(**kw: object):
    """Settings a send test can use. Carries the credential key and nothing resembling a login.

    ADR-023 removed `SMTP_EMAIL` and `SMTP_PASSWORD`: there is no account in the environment, so
    a test that wants to send has to connect a mailbox exactly as a person would.
    """
    from app.core.settings import Settings

    base: dict[str, object] = {
        "_env_file": None,
        "GROQ_API_KEY": "gsk_test",
        "CREDENTIAL_KEY": TEST_CREDENTIAL_KEY,
    }
    return Settings(**{**base, **kw})  # type: ignore[arg-type]


async def connect_mailbox(db, mailbox, *, password: str = "not-a-real-password"):
    """Give a mailbox credentials of its own, the way Connect Gmail would.

    An **app password** rather than an OAuth grant, deliberately: resolving an OAuth login mints
    an access token from Google, and hard rule 9's sibling is that nothing in this suite touches
    the network. Which branch of `login_for` runs is not what these tests are about.
    """
    from app.db.models import MailCredential
    from app.services.credentials import seal

    credential = MailCredential(
        org_id=mailbox.org_id,
        mailbox_id=mailbox.id,
        kind="app_password",
        account_email=mailbox.from_email,
        secret=seal(password, mail_settings()).token,
    )
    db.add(credential)
    await db.commit()
    return credential


async def allow_sending_to(db, org, *addresses: str) -> None:
    """Put addresses on this org's allowlist.

    ADR-023: the list is the org's rows, not `SEND_ALLOWLIST` in the environment, so a test that
    rehearses in `allowlist` mode has to write them.
    """
    from app.db.models import SendAllowlistEntry

    for address in addresses:
        db.add(
            SendAllowlistEntry(org_id=org.id, email_norm=address.strip().lower(), added_by="tests")
        )
    await db.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def campaign(db: AsyncSession, org):
    from app.db.models import Campaign

    # **With a persona**, because `POST /campaigns` has always required one and a campaign
    # without one could not exist in production. The fixture creating something the API would
    # refuse is why the suite could not see that a campaign with an unreadable persona crashed
    # the send loop: every test passed its own persona straight to `send_one`, so the campaign
    # row never had to have one.
    record = Campaign(
        org_id=org.id,
        name="Test Campaign",
        status="running",
        persona={
            "sender_name": "Asha Rao",
            "sender_role": "Founder",
            "agency_name": "Arkvane",
            "offer": "a two-week prototype",
            "goal": "book a 20-minute call",
            "cta_text": "Worth a 20-minute call next week?",
        },
    )
    db.add(record)
    await db.commit()
    return record


@pytest_asyncio.fixture(loop_scope="session")
async def lead_factory(db: AsyncSession, org, campaign):
    """Make leads with distinct addresses. Always @example.com — hard rule 9."""
    counter = itertools.count(1)

    async def _make(state: str = "NEW", **kw):
        from app.db.models import Lead

        n = next(counter)
        # Defaults that a caller may override, so a test can set the one field it cares about
        # without having to restate the rest.
        fields: dict[str, object] = {
            "email_norm": kw.pop("email", f"lead{n}@example.com"),
            "email_raw": f"Lead{n}@Example.com",
            "full_name": f"Lead {n}",
            "company_name": f"Company {n}",
        }
        fields.update(kw)
        record = Lead(org_id=org.id, campaign_id=campaign.id, state=state, **fields)
        db.add(record)
        await db.commit()
        return record

    return _make


@pytest_asyncio.fixture(loop_scope="session")
async def engine_factory(_migrated_database: None):
    """Build independent engines, so a test can hold two real concurrent transactions."""
    created = []

    def _make():
        engine = create_async_engine(TEST_DATABASE_URL)
        created.append(engine)
        return engine

    try:
        yield _make
    finally:
        for engine in created:
            await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def session_factory(_migrated_database: None):
    """An `async_sessionmaker` for code that opens its own transactions.

    The ingest loop and the send-recovery pass each commit per message on purpose, so they take
    a factory rather than a session. Sharing one engine keeps the connection count sane while
    still giving every `async with` its own transaction.
    """
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
