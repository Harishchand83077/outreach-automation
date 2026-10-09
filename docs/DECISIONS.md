# Decisions (ADRs)

Deviating from `ARCHITECTURE_TARGET.md` requires an entry here. Decisions inside the spec's latitude
(library choices, schema details, naming, UI layout, test strategy) are logged here too, briefly.

---

## ADR-001 — Run Postgres in Docker Compose; use Supabase for Auth only

**Date** 2026-10-09 · **Status** Accepted · **Supersedes** `ARCHITECTURE_TARGET.md` §3 (database row)

**Decision.** The database is a `postgres` service in the same `docker compose` file as `api`,
`worker` and `caddy`, on the always-on VM. Supabase is retained for **Auth only**, exactly as §14
specifies (JWT verified locally against JWKS).

**Why.** §3 selects Supabase Postgres on the free plan and then spends five separate mitigations
coping with the consequences: the session-pooler string because the direct connection is IPv6-only,
a 500 MB storage ceiling with a usage endpoint and a 70% warning, turning the auto-generated Data
API off, RLS deny-all as a "second lock" against that API, and the risk of a free project being
paused for inactivity. Every one of those exists because the database is a separate managed service
exposed to the internet. Putting Postgres on the VM — which we already operate, for the worker that
must be always-on anyway — removes all five rather than mitigating them. It also removes a network
hop from the hot path of a queue that is polled every 2 seconds.

**Cost, accepted.** The database now shares the VM's failure domain, so §19's nightly `pg_dump` plus
a weekly off-box copy stops being belt-and-braces and becomes the actual recovery plan. The restore
drill in `RUNBOOK.md` must therefore be rehearsed, not just written. We also lose Supabase's
dashboard and managed backups.

**Alternatives rejected.** (a) Supabase Postgres as specified — rejected on the mitigation count
above, for a workload of roughly 50 new leads/day. (b) SQLite — rejected: the queue needs
`FOR UPDATE SKIP LOCKED` and concurrent writers. (c) A managed Postgres elsewhere — rejected: costs
money, and reintroduces the network exposure.

**Revisit if** a second VM or a second region appears, or the DB outgrows a single small VM.

---

## ADR-002 — Send recovery searches both folders; `BCC_SELF` is optional

**Date** 2026-10-09 · **Status** Accepted · **Refines** `ARCHITECTURE_TARGET.md` §9.5

**Decision.** Keep the at-most-once outbox exactly as §9.5 describes — pre-generated `Message-ID`,
`idempotency_key` UNIQUE, claim/send/mark in two transactions, `unknown` resolved by a human, never
an automatic resend. Change only the recovery lookup: search the mailbox's **Sent folder and INBOX**
by `Message-ID`, and treat `BCC_SELF` as an optional fallback rather than a prerequisite.

**Why.** §9.5's recovery path depends on the provider copying SMTP-sent mail into the Sent folder,
which the spec itself marks `[VERIFY]` and which many providers do not do. Making correctness depend
on an unverified provider behaviour is the wrong shape. Searching both folders costs one extra IMAP
query on a path that only runs after a crash, and if neither folder yields the message the lead ends
`unknown` for a human — which is the specified outcome anyway.

**Alternatives rejected.** Mandating `BCC_SELF` — rejected: it doubles inbound volume and pollutes
the mailbox used for reply matching.

---

## ADR-003 — Vertical-slice sequencing

**Date** 2026-10-09 · **Status** Accepted · **Reorders** `ARCHITECTURE_TARGET.md` / master prompt §9

**Decision.** After the Phase 0 safety patch, build the narrowest path that gets **one lead** from
sheet to sent mail and back: data layer and state machine → import → research → draft and review →
send → replies. Auth/multi-tenancy, the admin UI, the warm-up ramp, Tavily search, the full
four-provider LLM chain and the storage/usage dashboards come after that path works end-to-end in
`SEND_MODE=allowlist`. No target from the spec is dropped; only the order changes.

**Why.** The master prompt's P0–P10 order puts auth and multi-tenancy (P2) ahead of campaigns,
research and drafting, so the first genuinely useful email would be months out and the riskiest
assumptions — whether grounded research produces a draft worth sending, and how many tokens that
actually costs — stay untested longest. §20's own capacity numbers are explicitly "planning numbers,
to be replaced by measurements"; the slice is how we get the measurements. Security is not deferred
by this: the Phase 0 patch closes the exploitable holes in the legacy code immediately, and the new
`app/` API is never exposed to the internet before Phase 7 lands auth.

**Cost, accepted.** The API runs locally without auth between Phase 1 and Phase 7. Mitigated by
binding it to localhost, and by `org_id` + `OrgContext` existing from the first migration (ADR-004)
so no retrofit is needed.

---

## ADR-004 — `org_id` from the first migration; members/roles UI deferred

**Date** 2026-10-09 · **Status** Accepted · **Refines** `ARCHITECTURE_TARGET.md` §5, §14

**Decision.** Every table carries `org_id` and every repository function takes an `OrgContext` from
migration 1 onward, with a single seeded org. The invite flow, the `owner|admin|member` role checks,
the cross-org test matrix and the members UI land in Phase 7.

**Why.** Adding a tenancy column to a populated schema later is the expensive, error-prone kind of
migration, and the spec is right that it belongs from day one. But the *enforcement* surface (roles,
invites, JWT verification, cross-org tests) is independent of the column and does not need to block
the first send. Splitting them gets the irreversible part done early and the reversible part done
when it is needed.

**Cost, accepted.** `OrgContext` is threaded through code that cannot yet be tested against a second
org. Mitigated by making the repository layer refuse a query with no `org_id` filter, so the
constraint is structural from the start rather than a convention.

---

## Minor decisions (inside spec latitude)

| Date | Decision | Why |
|---|---|---|
| 2026-10-09 | `uv` + `pyproject.toml` + `uv.lock`; delete `requirements.txt` | Spec asks for a lockfile; `uv` is already on the machine and pins the interpreter too |
| 2026-10-09 | Python pinned to 3.12.12 via `uv python install`; keep the existing 3.12.1 venv as found | Machine default is 3.14.3, which the spec forbids; the owner's existing venv already satisfies 3.12 so it was not clobbered |
| 2026-10-09 | Keep the legacy flat modules runnable through Phase 0, delete them module-by-module as `app/` supersedes each | Lets the owner keep using the prototype while the rewrite lands, and makes each deletion reviewable |
| 2026-10-09 | Skip the git history rewrite for `sample_leads.xlsx`; delete the file only | Both addresses are the owner's own and the repo is now private, so a force-push's risk exceeds its benefit. `git-filter-repo` recipe recorded in `RUNBOOK.md` if that changes |
| 2026-10-09 | Rename `docs/CLAUDE_CODE_MASTER_PROMPT.md` → `docs/MASTER_PROMPT.md` | Every cross-reference in the spec and in `ARCHITECTURE_TARGET.md` §4 uses the latter name |
