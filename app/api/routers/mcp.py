"""The MCP endpoint: one POST, stateless, read-only (ADR-027, ADR-028).

Revision 2026-07-28 removed sessions, the `initialize` handshake, the GET stream and SSE
resumability, so this is a single route that reads a JSON-RPC request and returns a JSON-RPC
response. Three methods: `server/discover` (which servers **MUST** implement), `tools/list` and
`tools/call`.

**It does not use the dashboard's authentication, deliberately.** `deps.current_user` verifies a
Supabase JWT, and Supabase issues `aud: "authenticated"` regardless of the RFC 8707 `resource`
parameter it was asked for (supabase/auth#2610) — so such a token cannot be shown to have been
minted for this endpoint. Accepting one here would mean accepting a credential obtained for
something else, which is the confused-deputy problem resource indicators exist to prevent. The
endpoint takes our own tokens and nothing else; see `app/services/api_tokens.py`.

**The org comes from the membership that token still resolves to**, re-read on every call, and
never from a tool argument (ADR-004). Revoke somebody's membership and their tokens stop working
in the same breath.

`GET` and `DELETE` answer `405`, which is what the spec asks a modern-only server to tell a
client still speaking the session-based revisions.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import SessionDep
from app.core.settings import config
from app.db.repo import OrgContext
from app.db.state import record_event
from app.services import api_tokens
from app.services.mcp import protocol, tools

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

MCP_PATH = "/mcp"


def _json_rpc_error(error: protocol.McpError, request_id: Any = None) -> JSONResponse:
    return JSONResponse(status_code=error.http_status, content=error.body(request_id))


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


def _unauthorised() -> JSONResponse:
    """One sentence for every kind of refusal.

    Unknown, revoked, expired and no-longer-a-member are all just *no* from outside, and the
    difference is useful only to somebody guessing. `WWW-Authenticate` is what tells a client to
    go and get a new credential rather than treat this as permanent.
    """
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": protocol.INVALID_REQUEST,
                "message": (
                    "A valid outreach API token is required. Mint one in the dashboard under "
                    "Settings, and send it as 'Authorization: Bearer oa_...'."
                ),
            },
        },
        headers={"WWW-Authenticate": 'Bearer realm="outreach-mcp"'},
    )


@router.get(MCP_PATH)
@router.delete(MCP_PATH)
async def legacy_transport_methods() -> Response:
    """`405`, as the spec asks of a server that implements only this revision.

    A client on one of the session-based revisions opens with GET to get an SSE stream, or
    DELETEs to end a session. Neither exists here, and answering `405` is what lets such a
    client fall back deterministically instead of hanging.
    """
    return Response(status_code=status.HTTP_405_METHOD_NOT_ALLOWED)


@router.post(MCP_PATH)
async def mcp_endpoint(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """The MCP endpoint. Every JSON-RPC message is its own POST."""
    # 1. Origin, before anything else. Servers MUST refuse a present-and-invalid Origin with 403
    #    to prevent DNS rebinding. An absent one is allowed: Anthropic connects from its own
    #    infrastructure, not a browser, so the only caller we have sends none.
    origin = request.headers.get("origin")
    allowed = tuple(config.allowed_origins())
    if not protocol.origin_is_allowed(origin, allowed):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": protocol.INVALID_REQUEST, "message": "Origin not allowed."},
            },
        )

    # 2. Authentication. Before parsing the body, so an unauthenticated caller cannot use this
    #    endpoint as a JSON parser or learn anything from the shape of a validation error.
    presented = _bearer(authorization)
    if presented is None:
        return _unauthorised()
    bearer = await api_tokens.verify_token(session, presented)
    if bearer is None or not any(scope in protocol.SCOPES for scope in bearer.scopes):
        # A token carrying no scope this endpoint recognises is refused at the door rather than
        # per tool. Which scopes it does carry decides which tools it is shown and may call.
        #
        # **Rolled back, because the lookup opened a transaction.** Returning here without
        # resolving it leaves the pooled connection idle in transaction, holding a share lock on
        # `api_tokens` and `members` until something else tears it down -- which is a refused
        # credential quietly blocking whatever wants those tables next, and is exactly how this
        # deadlocked the test suite's own cleanup. Every path out of this route now ends with
        # the transaction resolved.
        await session.rollback()
        return _unauthorised()
    # `verify_token` may have touched `last_used_at`; it is the only write this endpoint makes
    # and it must land whatever the call goes on to do.
    await session.commit()

    try:
        raw = await request.body()
        body = json.loads(raw) if raw else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        await session.rollback()
        return _json_rpc_error(
            protocol.McpError(protocol.INVALID_REQUEST, "The body is not valid JSON.")
        )

    # 3. Envelope and header/body agreement. The spec requires the comparison because an
    #    intermediary may route on the header while we execute the body.
    headers = {name.lower(): value for name, value in request.headers.items()}
    try:
        rpc = protocol.validate(body, headers)
    except protocol.McpError as error:
        await session.rollback()
        request_id = body.get("id") if isinstance(body, dict) else None
        return _json_rpc_error(error, request_id)

    ctx = OrgContext(org_id=bearer.org_id, actor_id=f"mcp:{bearer.user_id}")

    try:
        result = await _dispatch(rpc, session, ctx, bearer)
    except protocol.McpError as error:
        return _json_rpc_error(error, rpc.id)
    except Exception:
        # A bare exception would escape as a 500 outside the CORS middleware, which is the trap
        # that once made a duplicate-key error look like an unreachable backend. The reason goes
        # to the log; the caller gets a JSON-RPC error it can read.
        logger.exception("mcp call failed", extra={"mcp_method": rpc.method})
        # Same reason as the refusal path: an unresolved transaction holds locks, and this is
        # the branch where nobody knows what the handler had already done.
        await session.rollback()
        return _json_rpc_error(
            protocol.McpError(
                protocol.INTERNAL_ERROR,
                "The server failed to handle that call.",
                http_status=status.HTTP_200_OK,
            ),
            rpc.id,
        )

    return JSONResponse(content={"jsonrpc": "2.0", "id": rpc.id, "result": result})


async def _dispatch(
    rpc: protocol.McpRequest,
    session: SessionDep,
    ctx: OrgContext,
    bearer: api_tokens.TokenBearer,
) -> dict[str, Any]:
    if rpc.method == "server/discover":
        return protocol.discover_result(tools.INSTRUCTIONS)

    if rpc.method == "tools/list":
        return protocol.cacheable(
            # Filtered to what this credential may actually call. A read-only token is not
            # offered the proposal tools, because a tool a caller cannot use is worse than an
            # absent one: the model tries it, is refused, and tries again differently.
            {"tools": tools.describe_all(bearer.scopes)},
            ttl_ms=tools.LIST_TTL_MS,
            scope=tools.LIST_CACHE_SCOPE,
        )

    if rpc.method == "tools/call":
        return await _call_tool(rpc, session, ctx, bearer)

    # An unknown method MUST be 404 with -32601, specifically so a client can tell this from the
    # 404 of a server that hosts no MCP endpoint at all.
    raise protocol.McpError(
        protocol.METHOD_NOT_FOUND,
        f"Unknown method: {rpc.method}",
        http_status=status.HTTP_404_NOT_FOUND,
    )


async def _call_tool(
    rpc: protocol.McpRequest,
    session: SessionDep,
    ctx: OrgContext,
    bearer: api_tokens.TokenBearer,
) -> dict[str, Any]:
    name = rpc.tool_name or ""
    tool = tools.BY_NAME.get(name)
    if tool is None:
        # A protocol error rather than a tool error: a model cannot fix an unknown tool by
        # trying different arguments.
        raise protocol.McpError(protocol.INVALID_PARAMS, f"Unknown tool: {name}")

    # **The scope gate, and it is on the tool rather than written out here.** `required_scope`
    # is derived from whether the tool writes, so a tool added later is gated without anybody
    # remembering to gate it -- there is no permissive default to forget.
    #
    # Reported as a tool error rather than a 401: the credential is perfectly valid for this
    # endpoint and the rest of the catalogue, and only this one tool is out of its reach. The
    # sentence names the scope, because the person who can fix it is the user reading their
    # client, and the fix is to mint a token that has it.
    if not bearer.permits(tool.required_scope):
        await session.rollback()
        return protocol.tool_result(
            f"This token is not allowed to call {name}: it needs the {tool.required_scope} "
            "scope. Mint a token with it in the dashboard under Settings, if that is what you "
            "intend to allow.",
            is_error=True,
        )

    try:
        summary, structured = await tool.handler(session, ctx, rpc.arguments)
    except protocol.McpError as error:
        # **Roll back before answering.** A refused call has usually read a row or two and may
        # have written an event, and returning without resolving that transaction leaves the
        # connection holding locks until the request is torn down. Harmless-looking until it
        # is not: it deadlocked this file's own tests against their cleanup, and on the live
        # database the same shape is a refused tool call blocking whatever touches those rows
        # next. A tool that succeeded has already committed its own work.
        await session.rollback()
        if error.code == protocol.INVALID_PARAMS:
            # Reported as a *tool* error, because this is the class a model can recover from by
            # correcting its arguments — which is exactly what the spec distinguishes.
            return protocol.tool_result(error.message, is_error=True)
        raise

    await _audit(
        session, ctx, bearer, tool_name=name, client=rpc.client_name, wrote=not tool.read_only
    )
    return protocol.tool_result(summary, structured)


async def _audit(
    session: SessionDep,
    ctx: OrgContext,
    bearer: api_tokens.TokenBearer,
    *,
    tool_name: str,
    client: str | None,
    wrote: bool = False,
) -> None:
    """One event per tool call. A call that leaves no trace cannot be told from one that never
    happened, and this is the one surface where somebody else's software is doing the asking.

    Wrapped and never fatal: the audit records the work, it is not the work, and a read must not
    fail because the row describing it could not be written. The failure is logged instead, so a
    trail that has stopped being written is visible rather than silent.

    **A tool that writes commits its own work before this runs**, precisely because the
    `rollback` below would otherwise discard it -- correct for a read, where the audit row is
    the only write in flight, and destructive for a proposal. `wrote` is recorded so the trail
    distinguishes the two without anybody having to know which tools write.
    """
    try:
        await record_event(
            session,
            org_id=ctx.org_id,
            lead_id=None,
            type="mcp.tool_called",
            # `user`, because a person drove this through their own client -- the call is theirs,
            # not the system's, and the trail has to attribute it to somebody.
            actor="user",
            actor_id=f"mcp:{bearer.user_id}",
            data={
                "tool": tool_name,
                "wrote": wrote,
                "token_id": str(bearer.token_id),
                # Self-reported by the client and not verified by the protocol, so it is
                # recorded as a claim rather than treated as identity.
                "client_claimed": client,
            },
        )
        await session.commit()
    except Exception:
        logger.warning("could not write the mcp audit event", extra={"mcp_tool": tool_name})
        await session.rollback()
