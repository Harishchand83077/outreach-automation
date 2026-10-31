# Connecting your own Claude to this system

**Status: Phases 0, 1 and 3 are built and merged** — read-only tools plus two proposal tools
over a stateless Streamable HTTP endpoint at `/mcp`, authenticated with this system's own
tokens. Phase 3 landed once Phases 0–1 were in production, which is the gate ADR-027 set for it;
ADR-030 records the decision. **Phase 2 (OAuth 2.1) is not built**, and §6 says what it would now
take and why.

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

### The two that write (ADR-030)

| Tool | Does |
|---|---|
| `propose_draft` | writes a first email or follow-up into the review queue, `pending_review` |
| `propose_reply` | answers the lead's newest reply, into the same queue |

Both need the **`mcp:propose`** scope, which is off unless somebody ticks the box when minting a
token, and `tools/list` does not show them to a credential that lacks it. Neither sends and
neither approves: a person reads the draft in the dashboard exactly as they do for one this
system wrote, and there is no tool that can do it for them.

A proposal is validated with **`validate_draft`**, not the narrower `validate_rendered` the
template path uses. That is the one genuinely new decision in Phase 3 and ADR-030 is where it is
argued: an operator typing their own email is the authority on their own offer, a model is not,
and the model in question has usually just read a lead's reply — so the claim check is exactly
the defence that has to apply here, unrelaxed. A proposal that fails is stored as
`validation_failed` with its reasons, which keeps it out of the approval path, and the reasons
go back to the agent, which is the only party that can fix them.

Three refusals happen before anything is written: the lead's state must be one the approval path
can actually handle (`drafts.PROPOSABLE_FROM`, asserted to agree with `approve` rather than
restated), a reply must pass the same `refuse_reply_draft` the in-app drafter calls, and a reply
must have a label of record. `propose_reply` takes no `subject` — the thread's subject is ours.

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
could authorise one.

**What the worst case is, now that one tool writes.** Before Phase 3 it was "reading one
workspace's own data back to its owner". It is now **a bad draft sitting in a queue a human
reads**, which is the bound ADR-027 argued for from the start and the reason a proposal was
always the only write worth allowing. Three things make that the ceiling rather than a hope:

- A proposal enters review; it cannot approve itself, cannot move the lead, and cannot reach the
  send path. The write path is structurally barred from importing the state machine or naming an
  approved status.
- It goes through `validate_draft`, so an injected instruction that produces a confident claim
  about us is refused for citing nothing — the grounding check is also an injection defence, and
  it is the one that applies to the exact text a hijacked agent would write.
- A proposal that fails is stored `validation_failed`, which cannot be approved without somebody
  editing it first.

What remains, and is not eliminated, is a **plausible** bad draft reaching a reviewer who
approves it without reading. That is the same risk the in-app drafter has always carried, which
is the argument for treating a proposal identically rather than specially.

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
  *claimed* name — self-reported and recorded as a claim, since the protocol does not verify it —
  plus whether the call **wrote**. Wrapped and never fatal: the audit records the work, it is not
  the work. A tool that writes commits its own work *before* the audit attempt, because the
  rollback that makes a failed audit harmless to a read would otherwise discard the draft.
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
6. **To let it write**, tick *Let it propose drafts into the review queue* when minting the
   token. Then ask it to draft an email for one of your leads: the draft appears under Review
   with its evidence beside it, and nothing leaves until you approve it. Ask it to approve one
   and it will find no tool for that either.

A note on the `--env-file .env` in the command below, because leaving it out is the easy
mistake: `.env` on the server is root-owned and Compose interpolates the file at parse time, so
without it every variable reads as missing and the command fails with six `required variable ...
is missing a value` lines that look like a broken config rather than a missing flag.

### Checking the deployment itself, before wiring a client to it

The integration suite drives a `TestClient` against a test database in the same process, so it
cannot see TLS, Caddy, a forwarded `Origin`, or a route mounted at the root rather than under
`/api/v1` — the gap four of this project's first six deploy defects lived in. `mcp_smoke.py`
closes it, over the public URL:

```bash
sudo docker compose --env-file .env -f deploy/compose.prod.yml exec api \
    python -m app.scripts.mcp_smoke --base-url https://api.arkvane.in --email you@example.com
```

It mints a one-day **read-only** token — deliberately, because this runs against production and
a write-capable credential proving it writes would leave a real draft in somebody's real review
queue; the proposal tools are asserted to be withheld from it and to refuse it, which is the
half of Phase 3 that can be checked safely and the half worth checking. It exercises the
refusals (including that a **Supabase-shaped** credential is refused here), breaks each
2026-07-28 validation rule on purpose, calls every read tool in the
catalogue, asserts on the wire that no tool's schema takes an `org_id`, checks that every call
left an audit row, and **revokes the token in a `finally` block**. It never prints the secret.
Both of those last two are enforced by `tests/unit/test_mcp_smoke_script.py` rather than left to
care, because a check that leaks or outlives its own credential is worse than no check.

Anthropic warns that custom connectors are unverified and tells users to connect only to servers
run by organisations they trust. We are asking our own users to trust us, which is worth being
deliberate rather than quiet about.

---

## 6. The remaining phases, and what each one would take

**Phase 2 — OAuth 2.1.** A bearer token in a client's settings cannot be scoped per device as
narrowly as an OAuth grant, and that remains true. But the preferred route is closed: Supabase
cannot be the authorization server (§1), so Phase 2 now means **writing and operating a minimal
authorization server of our own** — metadata discovery, an authorization-code flow with PKCE,
`resource` validation, consent, and key management. That is a substantially larger change than
the endpoint it protects, and it should be its own decision rather than a paragraph in this one.

Meanwhile the exposure is bounded by the catalogue: a leaked token reads one workspace's data and
can write nothing. Mitigated by a default 90-day expiry, one-click revocation and `last_used_at`.

**Phase 3 — `propose_draft` / `propose_reply`.** Built; see §2 and ADR-030. The design held
exactly as written here, which is worth recording because it was planned before any of it ran: a
proposal is the same `Draft` row the in-app drafter creates, `pending_review`, through the same
validator, with `created_by='mcp:<user>'`. The gate this plan set — Phases 0–1 in production
first — was waited out rather than waived, and the two things building it added were both
refusals the plan had not thought of: the lead's state has to admit a review, and a reply has to
clear the reply policy.

**Resources, prompts and `subscriptions/listen`.** Not implemented and not advertised:
`server/discover` names the `tools` capability only. Advertising a capability we do not implement
would have a client ask for something that is not there.

---

## 7. What this does not replace

The in-app LLM flow stays. Most users will not connect a model of their own, and a system whose
drafting only works if you bring your own Claude is a system with a much narrower audience. This
is an addition for the people who want it, not a migration.
