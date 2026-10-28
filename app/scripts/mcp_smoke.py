"""Exercise the **deployed** `/mcp` endpoint over the real network, then revoke the key.

    python -m app.scripts.mcp_smoke --base-url https://api.arkvane.in --email you@example.com

Run it on the server, inside the api container, where the database is reachable:

    sudo docker compose -f deploy/compose.prod.yml exec api \
        python -m app.scripts.mcp_smoke --base-url https://api.arkvane.in --email you@example.com

**Why this exists when there are already 24 integration tests for the same endpoint.** Those
drive a `TestClient` against a test database in the same process. They cannot see TLS, Caddy,
header rewriting, the `Origin` the proxy forwards, or a route mounted at the root rather than
under `/api/v1`. Every one of the six defects that surfaced on this project's first real deploy
was invisible both locally and in CI, and four of them lived in exactly that gap. This check is
the one that answers "does a client on the public internet get what we think it gets?".

**It mints a real credential against the real database**, which is the one risky thing about it,
so two properties are enforced by `tests/unit/test_mcp_smoke_script.py` rather than left to
care: the secret is **never printed** (hard rule 2 — this output gets pasted into terminals and
chat windows), and the token is **revoked in a `finally` block**, so the run that fails does not
leave a live key behind. It is minted with a one-day expiry as a third line of defence, for the
case where the process is killed between the two.

**It is read-only, and refuses to be anything else.** It calls only the catalogue's tools, which
have no send, approve or settings-changing member and no scope that could authorise one. The
tool list is read from `app.services.mcp.tools` rather than restated here, so a tool added later
is exercised without anybody remembering this file exists.

What it asserts, beyond "the tools answer":

- the refusals, which are the half a happy-path check never looks at — no credential, a garbage
  credential, a **Supabase-shaped** credential (ADR-028's two-doors rule), a disallowed `Origin`,
  and the legacy GET and DELETE verbs;
- the 2026-07-28 validation rules, by deliberately breaking each one: a header that disagrees
  with the body is `-32020`, an unsupported version is `-32022`, an unknown method is `404` with
  `-32601`;
- that no tool's input schema has an `org_id` property, on the live deployment rather than in a
  unit test, because tenancy coming from a tool argument is the one mistake that would be
  invisible from inside a single workspace;
- that `tools/list` is ordered stably across two calls, which needs two calls to observe.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The path insert above has to precede these, as in `check_env.py`, so the script also runs
# when invoked by file path rather than with `-m`.
import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db.engine import get_sessionmaker  # noqa: E402
from app.db.models import Event, Member  # noqa: E402
from app.services import api_tokens  # noqa: E402
from app.services.mcp import protocol, tools  # noqa: E402

#: A day, not the 90-day default. This credential is meant to outlive one command and no longer,
#: and the `finally` block below should already have revoked it.
TTL_DAYS = 1

TIMEOUT = httpx.Timeout(30.0)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


class Report:
    """A PASS/FAIL table. Nothing here ever receives the token."""

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {name}{f' — {detail}' if detail else ''}")

    def expect(self, name: str, actual: Any, expected: Any) -> None:
        self.record(name, actual == expected, f"expected {expected!r}, got {actual!r}")

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]


def _headers(
    token: str, method: str, *, name: str | None = None, version: str | None = None
) -> dict[str, str]:
    """The headers revision 2026-07-28 requires, mirroring what the body will say.

    A real client sends these; the point of the mirroring is that an intermediary can route on
    the header while the server executes the body, so the server must check they agree.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": version or protocol.PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _body(
    method: str, *, params: dict[str, Any] | None = None, request_id: int = 1
) -> dict[str, Any]:
    payload = dict(params or {})
    payload["_meta"] = {
        protocol.META_PROTOCOL_VERSION: protocol.PROTOCOL_VERSION,
        protocol.META_CLIENT_INFO: {"name": "outreach-mcp-smoke", "version": "1"},
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": payload}


async def _rpc(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    tool_name: str | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/mcp",
        json=_body(method, params=params),
        headers=headers or _headers(token, method, name=tool_name),
    )


async def _check_refusals(client: httpx.AsyncClient, token: str, report: Report) -> None:
    """The half of the surface a happy-path check never looks at."""
    get_response = await client.get("/mcp")
    report.expect("GET /mcp is 405 (no legacy SSE stream)", get_response.status_code, 405)

    delete_response = await client.request("DELETE", "/mcp")
    report.expect("DELETE /mcp is 405 (no sessions to end)", delete_response.status_code, 405)

    no_auth = await client.post(
        "/mcp", json=_body("tools/list"), headers=_headers("", "tools/list") | {"Authorization": ""}
    )
    report.expect("no credential is 401", no_auth.status_code, 401)
    report.record(
        "a 401 carries WWW-Authenticate",
        "www-authenticate" in {k.lower() for k in no_auth.headers},
        "so a client refreshes rather than giving up",
    )

    garbage = await _rpc(client, "oa_not-a-real-token", "tools/list")
    report.expect("an unknown oa_ token is 401", garbage.status_code, 401)

    # ADR-028's two-doors rule, checked against the live deployment. A Supabase JWT is a
    # perfectly valid credential for every /api/v1 route and must be refused *here*, because
    # Supabase issues `aud: "authenticated"` whatever resource was requested (supabase/auth#2610)
    # and so it cannot be shown to have been minted for this endpoint. This sends a JWT-shaped
    # string rather than a real one: the prefix check refuses it before any lookup, which is the
    # behaviour being asserted.
    jwt_shaped = await _rpc(client, "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.x", "tools/list")
    report.expect("a JWT-shaped credential is 401", jwt_shaped.status_code, 401)

    bad_origin = await _rpc(
        client,
        token,
        "tools/list",
        headers=_headers(token, "tools/list") | {"Origin": "https://evil.example.com"},
    )
    report.expect("a disallowed Origin is 403", bad_origin.status_code, 403)


async def _check_envelope(client: httpx.AsyncClient, token: str, report: Report) -> None:
    """Each 2026-07-28 validation rule, broken on purpose."""
    mismatch = await client.post(
        "/mcp",
        json=_body("tools/list"),
        # The header says one method, the body another. An intermediary routing on the header
        # would send this somewhere the body does not describe.
        headers=_headers(token, "server/discover"),
    )
    report.expect("a header/body method mismatch is 400", mismatch.status_code, 400)
    report.expect(
        "...with code -32020 (HeaderMismatch)",
        mismatch.json().get("error", {}).get("code"),
        protocol.HEADER_MISMATCH,
    )

    old = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers=_headers(token, "tools/list", version="2025-03-26"),
    )
    report.expect("an unsupported protocol version is 400", old.status_code, 400)
    report.expect(
        "...with code -32022",
        old.json().get("error", {}).get("code"),
        protocol.UNSUPPORTED_PROTOCOL_VERSION,
    )

    unknown = await _rpc(client, token, "resources/list")
    report.expect("an unknown method is 404", unknown.status_code, 404)
    report.expect(
        "...with code -32601, so it is distinguishable from no MCP server at all",
        unknown.json().get("error", {}).get("code"),
        protocol.METHOD_NOT_FOUND,
    )


async def _check_discovery(client: httpx.AsyncClient, token: str, report: Report) -> None:
    discover = await _rpc(client, token, "server/discover")
    report.expect("server/discover answers 200", discover.status_code, 200)
    result = discover.json().get("result", {})
    report.record(
        "it advertises this revision",
        protocol.PROTOCOL_VERSION in str(result),
        protocol.PROTOCOL_VERSION,
    )
    report.record(
        "it advertises no capability we do not implement",
        "resources" not in result.get("capabilities", {})
        and "prompts" not in result.get("capabilities", {}),
        "a client must not be told to ask for something absent",
    )


async def _check_catalogue(client: httpx.AsyncClient, token: str, report: Report) -> None:
    first = await _rpc(client, token, "tools/list")
    report.expect("tools/list answers 200", first.status_code, 200)
    listed = first.json().get("result", {}).get("tools", [])
    names = [entry.get("name") for entry in listed]

    expected = sorted(tool.name for tool in tools.CATALOGUE)
    report.record(
        "every catalogue tool is served",
        sorted(name for name in names if name) == expected,
        f"{len(names)} served",
    )

    # Tenancy, asserted against the wire rather than against the source. `org_id` arriving as a
    # tool argument is the one tenancy mistake invisible from inside a single workspace.
    offenders = [
        entry.get("name")
        for entry in listed
        if "org_id" in (entry.get("inputSchema") or {}).get("properties", {})
    ]
    report.record("no tool takes an org_id argument", not offenders, str(offenders or "none"))

    # Nothing in the served catalogue should even describe a write. A model picks tools by their
    # descriptions, and a name promising a send is a name it will try.
    forbidden = ("send", "approve", "deliver", "smtp", "send_mode")
    writes = [name for name in names if name and any(word in name for word in forbidden)]
    report.record("no tool name promises a send or an approval", not writes, str(writes or "none"))

    second = await _rpc(client, token, "tools/list")
    second_names = [entry.get("name") for entry in second.json().get("result", {}).get("tools", [])]
    # The spec asks for a stable order, which takes two calls to observe. Asserting it against a
    # single response is the mistake this replaced.
    report.record("the order is stable across calls", names == second_names)


async def _check_every_tool(client: httpx.AsyncClient, token: str, report: Report) -> int:
    """Call each tool. An empty workspace is a pass; a 500 or a protocol error is not.

    Minimal arguments only: a tool needing an id it cannot be given is exercised for its refusal,
    which still proves it is reachable, parses its arguments and answers in the envelope.
    """
    absent = str(uuid.uuid4())
    arguments: dict[str, dict[str, Any]] = {
        "get_campaign": {"campaign_id": absent},
        "get_lead": {"lead_id": absent},
        "get_research_with_sources": {"lead_id": absent},
        "get_meeting_brief": {"lead_id": absent},
    }

    for tool in tools.CATALOGUE:
        response = await _rpc(
            client,
            token,
            "tools/call",
            params={"name": tool.name, "arguments": arguments.get(tool.name, {})},
            tool_name=tool.name,
        )
        payload = response.json()
        ok = response.status_code == 200 and "result" in payload
        detail = ""
        if ok:
            result = payload["result"]
            # A tool error is a legitimate answer here: an id from nowhere resolves to nothing,
            # which is the tenancy behaviour we want (nothing, not a 403 that confirms the id).
            detail = "tool error (expected for an absent id)" if result.get("isError") else "ok"
            if not result.get("resultType"):
                ok, detail = False, "no resultType on the result"
        else:
            detail = f"HTTP {response.status_code} {str(payload)[:120]}"
        report.record(f"tools/call {tool.name}", ok, detail)

    return len(tools.CATALOGUE)


async def _check_audit(
    session: Any, org_id: uuid.UUID, since: datetime, expected: int, report: Report
) -> None:
    """Every tool call must have left an `mcp.tool_called` row.

    `MCP_PLAN.md` §5 says a call that left no event is a bug rather than a detail, and the
    writing of it is deliberately wrapped and non-fatal — which means a trail that has quietly
    stopped being written looks exactly like one that is working. This is the only check that
    can tell the difference, because it reads the database rather than the response.
    """
    # The session has to see rows the api process committed, not the snapshot it opened with.
    await session.rollback()
    written = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(
            Event.org_id == org_id,
            Event.type == "mcp.tool_called",
            Event.at >= since,
        )
    )
    report.record(
        "every tool call left an audit event",
        (written or 0) >= expected,
        f"{written} events for {expected} calls",
    )


async def run(base_url: str, email: str, report: Report) -> None:
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        member = await session.scalar(select(Member).where(Member.email == email))
        if member is None:
            raise SystemExit(
                f"No member with the address {email!r}. This has to be somebody who already "
                "has a workspace; the check does not create one."
            )

        minted = await api_tokens.create_token(
            session,
            org_id=member.org_id,
            user_id=member.user_id,
            name="smoke check (auto-revoked)",
            ttl_days=TTL_DAYS,
        )
        await session.commit()
        # The prefix, never the secret. It is enough to recognise the row if the revoke below
        # somehow does not land, and useless as a credential. Plain ASCII, because this is read
        # in a container shell and on a Windows console, and one of those mangles an ellipsis.
        print(f"  minted a one-day token {minted.prefix}... for org {member.org_id}\n")
        started = datetime.now(UTC)

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT) as client:
                print("Refusals")
                await _check_refusals(client, minted.secret, report)
                print("\nEnvelope validation")
                await _check_envelope(client, minted.secret, report)
                print("\nDiscovery")
                await _check_discovery(client, minted.secret, report)
                print("\nCatalogue")
                await _check_catalogue(client, minted.secret, report)
                print("\nEvery tool")
                calls = await _check_every_tool(client, minted.secret, report)
                print("\nAudit trail")
                await _check_audit(session, member.org_id, started, calls, report)
        finally:
            # On every path, including an exception above. A token left behind by a failed check
            # is a credential nobody remembers exists.
            revoked = await api_tokens.revoke_token(
                session, org_id=member.org_id, token_id=minted.id
            )
            await session.commit()
            print(f"\n  revoked the smoke token: {revoked}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        required=True,
        help="The public base URL, e.g. https://api.arkvane.in. The point is to go through the "
        "proxy and TLS, so this should not be localhost.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="An existing member's address. Their workspace is the one read.",
    )
    args = parser.parse_args()

    report = Report()
    print(f"MCP smoke check against {args.base_url}\n")
    asyncio.run(run(args.base_url.rstrip("/"), args.email, report))

    failures = report.failures
    print(f"\n{len(report.checks) - len(failures)}/{len(report.checks)} checks passed.")
    for check in failures:
        print(f"  FAIL {check.name}: {check.detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
