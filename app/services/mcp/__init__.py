"""The MCP surface: a stateless JSON-RPC endpoint exposing read-only tools (ADR-027, ADR-028).

`protocol` holds the wire rules of revision 2026-07-28; `tools` holds the catalogue. Nothing in
this package may reach SMTP or decide a send mode, and
`tests/unit/test_mcp_safety.py` asserts that over the source.
"""
