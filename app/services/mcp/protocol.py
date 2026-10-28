"""The wire rules of MCP revision 2026-07-28, kept in one place.

**This revision is much simpler to serve than the ones before it, and that is worth stating
because `docs/MCP_PLAN.md` was written against the older shape.** 2026-07-28 removes the
`initialize`/`notifications/initialized` handshake, protocol-level sessions and the
`Mcp-Session-Id` header, the standalone GET stream, and SSE resumability. What is left for a
server that only answers requests is a single POST endpoint that reads a JSON-RPC request and
returns a JSON-RPC response. There is no connection state to hold, which means there is no
connection state to get wrong.

What it adds instead is validation, and the reason is a real attack rather than tidiness:
selected body fields are mirrored into HTTP headers so that intermediaries can route without
parsing the body, and a server that trusted one while executing the other would let a caller
show a load balancer one thing and us another. So the spec requires us to compare them and
refuse a mismatch with `-32020`.

Everything here is pure: no database, no request object, no I/O. The router turns these into
HTTP, which keeps the rules testable without a server.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any, Literal

#: The revision we speak. One version, deliberately: supporting the legacy handshake era as
#: well would mean carrying sessions, a GET stream and resumability for clients that Anthropic's
#: own connector does not need.
PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_VERSIONS: tuple[str, ...] = (PROTOCOL_VERSION,)

#: There is deliberately no write scope. See ADR-027: no tool sends, so none could authorise it.
SCOPES: tuple[str, ...] = ("mcp:read",)

SERVER_NAME = "outreach-automation"

# JSON-RPC and MCP error codes. The `-32020`/`-32022` pair is from the spec's reserved range;
# `-32601`/`-32602`/`-32600` are plain JSON-RPC.
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: `_meta` keys. Prefixed by the spec; written out so a typo is a test failure rather than a
#: silently ignored field.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

_SENTINEL_PREFIX = "=?base64?"
_SENTINEL_SUFFIX = "?="

#: Methods whose `params.name` must be mirrored into `Mcp-Name`.
_NAME_BEARING = {"tools/call"}


class McpError(Exception):
    """A JSON-RPC error with the HTTP status the spec asks us to return beside it.

    The status matters and is not always 400: an unknown *method* must be `404` with `-32601`,
    specifically so a modern client can tell our 404 from the 404 of a legacy server that does
    not host an MCP endpoint at all.
    """

    def __init__(
        self,
        code: int,
        message: str,
        *,
        http_status: int = 400,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.data = data

    def body(self, request_id: Any = None) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        return {"jsonrpc": "2.0", "id": request_id, "error": error}


@dataclass(frozen=True)
class McpRequest:
    """A validated request. Reaching here means the headers and the body agree."""

    id: Any
    method: str
    params: dict[str, Any] = field(default_factory=dict)
    client_name: str | None = None

    @property
    def tool_name(self) -> str | None:
        name = self.params.get("name")
        return name if isinstance(name, str) else None

    @property
    def arguments(self) -> dict[str, Any]:
        args = self.params.get("arguments")
        return args if isinstance(args, dict) else {}


def decode_header_value(raw: str) -> str:
    """Decode the spec's Base64 sentinel, or return the value unchanged.

    A value that cannot be represented as plain ASCII arrives as `=?base64?...?=`, and servers
    **MUST** decode it before comparing it with the body. A malformed sentinel is a header
    mismatch rather than something to guess at: silently treating undecodable bytes as a literal
    string would reintroduce exactly the header/body disagreement this check exists to catch.
    """
    if not (raw.startswith(_SENTINEL_PREFIX) and raw.endswith(_SENTINEL_SUFFIX)):
        return raw
    payload = raw[len(_SENTINEL_PREFIX) : -len(_SENTINEL_SUFFIX)]
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise McpError(
            HEADER_MISMATCH, "A Base64-encoded header value could not be decoded."
        ) from exc


def untrusted(text: str, *, source: str, id: str) -> str:
    """Wrap somebody else's words so a model can see where they begin.

    Defence in depth and **not** the primary defence: the catalogue containing no tool that
    could send is what makes a hijacked agent survivable (ADR-027). This only makes an injection
    visible.

    The closing tag is stripped from the payload first, and that is the whole reason this is a
    function rather than an f-string at each call site. A reply containing `</untrusted>` would
    otherwise close the block early, and everything after it would read as *our* instructions --
    the injection succeeding because of the delimiter meant to expose it.
    """
    safe = text.replace("</untrusted>", "<\\/untrusted>")
    return f'<untrusted source="{source}" id="{id}">\n{safe}\n</untrusted>'


def complete(result: dict[str, Any]) -> dict[str, Any]:
    """Stamp a result as `complete`.

    Every result in this revision carries `resultType`; the other value, `input_required`, is
    for multi round-trip requests, which nothing here uses — a read-only tool has nothing to ask
    the user for.
    """
    return {
        "resultType": "complete",
        **result,
        "_meta": {META_SERVER_INFO: {"name": SERVER_NAME, "version": PROTOCOL_VERSION}},
    }


def cacheable(
    result: dict[str, Any], *, ttl_ms: int, scope: Literal["public", "private"]
) -> dict[str, Any]:
    """A `CacheableResult`: `tools/list` and friends must carry `ttlMs` and `cacheScope`."""
    return complete({**result, "ttlMs": ttl_ms, "cacheScope": scope})


def discover_result(instructions: str) -> dict[str, Any]:
    """`server/discover`, which every server **MUST** implement.

    `capabilities` names `tools` only. We serve no resources and no prompts, and advertising a
    capability we do not implement would have a client ask for something that is not there.
    `listChanged` is absent because the catalogue is static and we open no subscription stream
    to announce a change on.
    """
    return cacheable(
        {
            "supportedVersions": list(SUPPORTED_VERSIONS),
            "capabilities": {"tools": {}},
            "instructions": instructions,
        },
        ttl_ms=3_600_000,
        scope="private",
    )


def tool_result(text: str, structured: Any = None, *, is_error: bool = False) -> dict[str, Any]:
    """A `tools/call` result.

    The text block carries the same information as `structuredContent` because the spec asks a
    tool returning structured content to also return it as text, for clients that do not read
    the structured field.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return complete(result)


def _require_header(headers: dict[str, str], name: str) -> str:
    value = headers.get(name.lower())
    if value is None or value == "":
        raise McpError(HEADER_MISMATCH, f"The {name} header is required and was not sent.")
    return value


def validate(body: Any, headers: dict[str, str]) -> McpRequest:
    """Check the envelope and the header/body agreement, or raise.

    `headers` is expected lower-cased by the caller: field names are case-insensitive and
    comparing them case-sensitively is a bug that only shows up against one client.
    """
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise McpError(
            INVALID_REQUEST, 'The body must be a JSON-RPC 2.0 object with "jsonrpc": "2.0".'
        )

    method = body.get("method")
    if not isinstance(method, str) or not method:
        raise McpError(INVALID_REQUEST, "The body must name a method.")

    request_id = body.get("id")
    params = body.get("params")
    params = params if isinstance(params, dict) else {}
    meta = params.get("_meta")
    meta = meta if isinstance(meta, dict) else {}

    # 1. The protocol version, in the header and in `_meta`, and they must agree.
    header_version = _require_header(headers, "MCP-Protocol-Version")
    meta_version = meta.get(META_PROTOCOL_VERSION)
    if meta_version is not None and meta_version != header_version:
        raise McpError(
            HEADER_MISMATCH,
            "The MCP-Protocol-Version header does not match the version in the request body.",
        )
    if header_version not in SUPPORTED_VERSIONS:
        raise McpError(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            data={"supported": list(SUPPORTED_VERSIONS), "requested": header_version},
        )

    # 2. `Mcp-Method` mirrors `method`.
    header_method = _require_header(headers, "Mcp-Method")
    if decode_header_value(header_method) != method:
        raise McpError(
            HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method header value '{header_method}' "
            f"does not match body value '{method}'.",
        )

    # 3. `Mcp-Name` mirrors `params.name`, for the methods that have one.
    if method in _NAME_BEARING:
        body_name = params.get("name")
        if not isinstance(body_name, str) or not body_name:
            raise McpError(INVALID_PARAMS, "tools/call requires a tool name.")
        header_name = _require_header(headers, "Mcp-Name")
        if decode_header_value(header_name) != body_name:
            raise McpError(
                HEADER_MISMATCH,
                f"Header mismatch: Mcp-Name header value '{header_name}' "
                f"does not match body value '{body_name}'.",
            )

    client = meta.get(META_CLIENT_INFO)
    client_name = client.get("name") if isinstance(client, dict) else None

    return McpRequest(
        id=request_id,
        method=method,
        params=params,
        client_name=client_name if isinstance(client_name, str) else None,
    )


def origin_is_allowed(origin: str | None, allowed: tuple[str, ...]) -> bool:
    """Servers **MUST** validate `Origin` to prevent DNS rebinding, and refuse with 403.

    An absent `Origin` is allowed: the spec's requirement is on a *present and invalid* one, and
    Anthropic connects from its own infrastructure rather than from a browser, so a server-side
    client legitimately sends none. Refusing those would refuse the only caller we have.
    """
    if origin is None or origin == "":
        return True
    return origin in allowed
