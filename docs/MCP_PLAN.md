# Connecting your own Claude to this system

**Status: Phases 0 and 1 are built and merged — read-only tools over a stateless Streamable HTTP
endpoint at `/mcp`, authenticated with this system's own tokens.** Phase 2 (OAuth 2.1) and Phase
3 (proposing drafts) are not built; §7 says what each would now take and why neither was done in
the same change.

The goal, unchanged: a user adds `https://api.arkvane.in/mcp` to their own Claude as a custom
connector and can then ask it about their campaigns, leads, research and replies.

---

## 1. What changed between planning this and building it

This document was first written against protocol revision expectations that no longer hold. Both
corrections made the work *smaller* and one of them closed the plan's biggest open question, so
they are recorded rather than quietly edited away.

### The spec got simpler: revision 2026-07-28 is stateless

Verified against the published changelog and transport pages:

- **No `initialize` handshake and no `notifications/initialized`.** Every request carries its own
  protocol version, client info and capabilities in `_meta`
  (`io.modelcontextprotocol/protocolVersion`, `.../clientInfo`, `.../clientCapabilities`).
- **No protocol-level sessions and no `Mcp-Session-Id`.** Servers needing cross-call state pass
  explicit handles as ordinary tool arguments.
- **No GET stream and no SSE resumability.** `Last-Event-ID` is gone; long-lived notifications
  come from a `subscriptions/listen` request instead, which we do not implement.
- **`server/discover` MUST be implemented**, advertising supported versions, capabilities and
  identity.
- **Every result carries `resultType`** (`"complete"`, or `"input_required"` for multi
  round-trip requests, which a read-only tool never needs).
- **List results carry `ttlMs` and `cacheScope`** (`CacheableResult`).
- **Request metadata is mirrored into headers and MUST be validated**: `MCP-Protocol-Version`,
  `Mcp-Method`, and `Mcp-Name` for `tools/call`. A mismatch is `400` with `-32020`
  (`HeaderMismatch`); an unsupported version is `400` with `-32022`; an unknown method is `404`
  with `-32601`.
- **`Origin` MUST be validated** and an invalid one refused with `403`, against DNS rebinding.
- **Anthropic connects from its cloud**, so the endpoint must be publicly reachable.

So what we had to serve is one POST route with no connection state. The older shape — sessions,
a second GET endpoint, resumable streams — would have been most of the work and none of it is
required.

### Supabase cannot be the authorization server, and that is now settled

The plan called this "the single biggest unknown". It is answered, and the answer is **no**.

[supabase/auth#2610](https://github.com/supabase/auth/issues/2610) is open: Supabase's OAuth 2.1
server **accepts the RFC 8707 `resource` parameter and ignores it**, issuing tokens with
`aud: "authenticated"` regardless of what was requested. Two independent reports in October 2026
confirm it on hosted projects, and one raises the security consequence directly — with dynamic
client registration, a malicious MCP server could obtain a token that another resource server
would accept. That is exactly the confused-deputy attack resource indicators exist to prevent.
[Issue #2850](https://github.com/supabase/auth/issues/2850) separately asks for Client ID
Metadata Documents, which the spec now prefers over the deprecated Dynamic Client Registration.

**This decided the authentication design.** Not just "option 1 is unavailable" — it is a positive
reason to refuse Supabase tokens at this endpoint. A token whose audience is a constant cannot be
shown to have been minted *for* us, so accepting one means accepting a credential obtained for
something else. ADR-028 records that, and `/mcp` refuses a Supabase JWT even though it is a
perfectly valid credential for every `/api/v1` route.

### Still to re-check on the day, if Phase 2 is ever built

- Whether Supabase has shipped `resource` support since October 2026 — check the issue, not a
  blog post.
- Exact plan availability for custom connectors. Two Anthropic pages disagreed when this was
  first written; one of them is stale.

---

## 2. What is built

### The endpoint

`POST /mcp`, mounted at the root rather than under `/api/v1`, because this URL is typed into a
person's MCP client and that is the address this document promises. `GET` and `DELETE` answer
`405`, which is what lets a client still speaking a session-based revision fail deterministically
instead of hanging.

`app/services/mcp/protocol.py` holds the wire rules as pure functions — no database, no request
object — so the envelope is testable without a server. `app/api/routers/mcp.py` turns them into
HTTP.

### Authentication: this system's own tokens

`app.api_tokens`, minted in **Settings → Connect your own Claude**, sent as
`Authorization: Bearer oa_...`.

- **Only the `sha256` hash is stored**, with a short prefix kept in clear so a row can be
  identified when revoking it. `sha256` rather than a password hash deliberately: this is 32
  bytes of `secrets` output, so there is no dictionary to slow down, and the lookup has to be
  deterministic to be a lookup.
- **The secret is returned exactly once.** No route reads one back, because a credential a route
  can return is a credential a stolen session can return.
- **Scope `mcp:read`, and the database refuses anything else** — a check constraint, the same
  device as `ck_orgs_send_mode`. There is no write scope, so one cannot be invented by hand.
- **Default 90-day expiry**, revocable, with `last_used_at` so "is this still in use?" is
  answerable before revoking something.
- **Authorisation re-resolves the `members` row on every call.** The token says who is asking;
  the membership says what they may reach. Remove somebody from a workspace and their tokens stop
  working in the same breath — otherwise a token is an ex-colleague holding a key.

### The tools, all read-only

| Tool | Returns |
|---|---|
| `list_campaigns` | id, name, kind, mode, status |
| `get_campaign` | the above plus settings and a lead count by state |
| `list_leads` | leads, filterable by campaign and state |
| `get_lead` | the lead, its message history, latest draft, recorded outcome, and their replies |
| `get_research_with_sources` | findings **with their citations**, plus the rejected ones |
| `list_reply_queue` | inbound messages with the label of record and its confidence |
| `get_meeting_brief` | everything known about one lead, for a call |

`org_id` is never a tool argument; a test asserts no tool's schema has such a property.
`get_meeting_brief` is composed from the other read paths rather than querying afresh, so it
cannot show something they do not — which would have been this project's signature defect again.

---

## 3. Prompt injection — the part that matters most

Everything returned about a lead, a reply or a researched page is **text somebody else wrote**.
Hard rule 7 already calls it untrusted. What changes is where it goes: today it reaches our
classifier, which can only *label*; over MCP it reaches an agent that has tools.

**The containment is the tool catalogue, and that is the whole safety argument.** There is no
send tool, no approve tool, no tool that touches a send mode or an allowlist, and no scope that
could authorise one. The worst a fully hijacked agent achieves with this catalogue is reading one
workspace's own data back to its owner.

Two further defences, both explicitly secondary:

1. **Third-party text is wrapped and labelled**, `<untrusted source="lead-reply" id="...">`, with
   a standing note in every such tool's description that content inside is data and never
   instructions.
2. **The wrapper cannot be escaped.** A reply containing `</untrusted>` would otherwise close the
   block early and have the rest read as *our* instructions — the injection succeeding *because*
   of the delimiter meant to expose it. The closing tag is escaped in the payload, and
   `test_mcp_safety.py` asserts exactly one closing tag survives.

A design that depended on a model ignoring an injected instruction would fail the first time one
worked. This one does not.

---

## 4. The other rules it keeps

- **Tenancy.** Per-tool tests: a lead or campaign id from another workspace resolves to *nothing*
  rather than a 403, because a refusal that distinguishes "not yours" from "does not exist"
  confirms the id.
- **Audit.** One `mcp.tool_called` event per call, naming the tool, the token and the client's
  *claimed* name — self-reported and recorded as a claim, since the protocol does not verify it.
  Wrapped and never fatal: the audit records the work, it is not the work.
- **No send mode anywhere.** `tests/unit/test_send_guard.py`'s tree-wide structural check covers
  the package, and `test_mcp_safety.py` adds the narrower rule that nothing here imports the mail
  services at all — so a future tool cannot reach them via an import already in the file.
- **Nothing returns a credential**, and no `invalid_reason` is echoed.

---

## 5. How to use it

1. **Settings → Connect your own Claude → Create token.** Copy it then; it is not shown again.
2. **In Claude: Customize → Connectors → Add custom connector.** URL
   `https://api.arkvane.in/mcp`, and add the token as an `Authorization: Bearer <token>` header.
   On Team and Enterprise plans an admin adds it under organisation settings first.
3. Ask *"list my campaigns"*.
4. **Check the audit trail.** Every call writes an `mcp.tool_called` event. A call that left no
   event is a bug, not a detail.
5. **Try to break it.** Ask Claude to send an email. It should find no tool that can, and say so.

Anthropic warns that custom connectors are unverified and tells users to connect only to servers
run by organisations they trust. We are asking our own users to trust us, which is worth being
deliberate rather than quiet about.

---

## 6. What is deliberately not built

**Phase 2 — OAuth 2.1.** A bearer token in a client's settings cannot be scoped per device as
narrowly as an OAuth grant, and that remains true. But the preferred route is closed: Supabase
cannot be the authorization server (§1), so Phase 2 now means **writing and operating a minimal
authorization server of our own** — metadata discovery, an authorization-code flow with PKCE,
`resource` validation, consent, and key management. That is a substantially larger change than
the endpoint it protects, and it should be its own decision rather than a paragraph in this one.

Meanwhile the exposure is bounded by the catalogue: a leaked token reads one workspace's data and
can write nothing. Mitigated by a default 90-day expiry, one-click revocation and `last_used_at`.

**Phase 3 — `propose_draft` / `propose_reply`.** Still the right design: a proposal is the same
`Draft` row the in-app drafter creates, `pending_review`, through the same validator, with
`created_by='mcp:<user>'`. It does not bypass review, it enters it. It is not built because this
plan's own gate says Phase 3 is a separate decision taken **with Phases 0–2 in production**, and
nothing is in production yet. Building it in the same change would have been ignoring the
sequencing that made the first part safe to ship.

**Resources, prompts and `subscriptions/listen`.** Not implemented and not advertised:
`server/discover` names the `tools` capability only. Advertising a capability we do not implement
would have a client ask for something that is not there.

---

## 7. What this does not replace

The in-app LLM flow stays. Most users will not connect a model of their own, and a system whose
drafting only works if you bring your own Claude is a system with a much narrower audience. This
is an addition for the people who want it, not a migration.
