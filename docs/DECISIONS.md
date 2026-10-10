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

## ADR-005 — Never run migrations against a shared or default port

**Date** 2026-10-10 · **Status** Accepted · **Prompted by** a near miss, not a hypothetical

**Decision.**

1. This project's Postgres listens on **5433**, never the default 5432, bound to `127.0.0.1`,
   with an explicit container name (`outreach-pg`) and volume (`outreach_pgdata`) so it cannot
   share either with another project on the same machine.
2. Migrations **refuse to run** unless the target is ours. `app/db/guard.py` checks, before any
   DDL, that the connected database is named `outreach` (or `outreach_test`) **and** that it
   contains no tables outside our `app` schema. There is no override flag: if it fires, the fix
   is to correct `DATABASE_URL`, not to force past it.
3. The integration-test URL is **derived** from `DATABASE_URL` rather than written out
   separately, so there is one place to configure credentials and no way for the test harness to
   drift onto a different server.

**Why.** While setting up Phase 1, `.env` contained
`DATABASE_URL=postgresql+asyncpg://outreach:<password>@localhost:5432/outreach`. Port 5432 on
this machine is held by `postgres-ml`, an unrelated project's container. `alembic upgrade head`
connected to **that** database and failed only because the password did not match. Had it
matched, the migration would have created an `app` schema inside another project's database.
Nothing was written, but the only thing standing in the way was a credential mismatch, which is
luck rather than a safety mechanism.

Two details made this easy to get wrong, and both are now fixed rather than documented around:

- The template and the early setup instructions both used `localhost:5432`, the port most likely
  to be already occupied. A default that is also the most contended value is a bad default.
- Docker Compose resolves a bare `.env` relative to the **compose file's** directory, not the
  repository root, so `POSTGRES_PASSWORD` in the root `.env` was silently ignored and the
  container initialised with the built-in default — guaranteeing a mismatch with `DATABASE_URL`.
  The documented command is now `docker compose --env-file .env -f deploy/compose.yml up -d`.

**Cost, accepted.** A non-standard port is mildly surprising, and anyone connecting with `psql`
has to pass `-p 5433`. That is a small price for removing a whole class of cross-project
accident. The guard also adds one round trip before each migration.

**Alternatives rejected.** (a) Documenting the risk without enforcing it — the near miss happened
*while* the risk was understood. (b) Checking host and port rather than database identity —
brittle, since production legitimately uses a different host and port; the database's own name
and contents are what actually matter. (c) An override environment variable — it would be set
once in frustration and then left set.

**Tested by** `tests/integration/test_db_guard.py`: the right database passes, a wrongly named
one is refused, and a correctly named database holding foreign tables is refused — that last case
being the one that would have caught the real near miss.

**The rule, for future sessions:** never point a migration at a shared or default port, and never
assume a connection failure was the thing protecting you.

---

## ADR-006 — Google Workspace on a GoDaddy domain: what it settles

**Date** 2026-10-10 · **Status** Accepted · **Resolves** the `[VERIFY]` in ADR-002, and Q-B

**The facts.** The sending mailbox is **Google Workspace (paid Starter)** on a dedicated domain
whose DNS is at **GoDaddy**.

**What this settles, and why each one mattered:**

1. **ADR-002's Sent-folder recovery works.** Gmail and Workspace copy mail sent through
   `smtp.gmail.com` into the Sent folder automatically. That was flagged `[VERIFY]` because the
   whole crash-recovery path depends on it: a row stuck in `sending` is resolved by searching the
   mailbox for its pre-generated `Message-ID`, and a provider that does not save SMTP-sent mail
   would leave every such row unresolvable. **`BCC_SELF` is therefore not needed.**
2. **The daily cap has real headroom.** Workspace allows roughly 2,000 messages a day per user,
   against our 100. The cap is ours for deliverability reasons, not the provider's limit — so
   hitting it is a choice we are making, not a wall we are near. Personal Gmail's ~500/day would
   have been closer.
3. **C6 is resolved rather than mitigated.** The audit flagged using "a Gmail app password over
   SMTP/IMAP for bulk cold outreach" as an account-suspension risk. On a paid Workspace account
   with its own domain, SMTP relay is a supported use rather than something tolerated.
4. **SPF, DKIM and DMARC are set at GoDaddy.** Google publishes the exact records; the Phase 9
   runbook will give them verbatim with GoDaddy's DNS-panel steps, since that is the one part of
   deliverability that no amount of code can substitute for.

**Still required of the operator**, and recorded so it is not assumed:

- IMAP must be enabled in Workspace (Settings → Apps → Gmail → IMAP access). It is off by default
  on some plans, and the reply engine cannot see anything without it.
- The SMTP password must be an **App Password**, which requires 2-Step Verification on the
  account. A normal account password will be rejected.

**Alternatives rejected.** The Gmail API with OAuth instead of SMTP — better rate limits and no
App Password, but it needs a Google Cloud project, a consent screen and a token refresh path, for
a volume that SMTP handles comfortably. Worth revisiting if we ever outgrow 2,000/day.

---

## ADR-007 — Inbound mail may be an orphan; outbound never may

**Date** 2026-10-10. **Status** accepted. **Supersedes** nothing; tightens the §5 schema.

**Context.** `messages.lead_id` was `NOT NULL`. §10.2 requires that mail matching no lead is
stored anyway — never deleted, never marked read — because the alternative is silently losing
somebody's reply. The two cannot both hold, and the integration test for orphan storage failed
with a not-null violation rather than passing with a workaround.

**Decision.** `messages.lead_id` is nullable, and `CHECK (direction = 'in' OR lead_id IS NOT
NULL)` keeps every outbound row attached to a lead. Migration `e6ad2939ee88`.

**Why not the alternatives.**
- *A synthetic "unmatched" lead per org* would put non-leads in the leads table, where the send
  engine, the cap arithmetic and every count would have to learn to exclude them. One nullable
  column is cheaper than a special case in ten places.
- *A separate `unmatched_messages` table* means two places to look for a reply, and the matcher
  would have to search both when a later message threads to one.

**Consequence.** `outbox.mark_sent` raises if an outbound row somehow has no lead, rather than
transitioning nothing: a send recorded against no lead would leave the lead eligible to be sent
again, which breaks at-most-once. The check constraint makes that unreachable; the raise is there
for the day it is not.

---

## ADR-008 — A clear refusal closes a lead without passing through `REPLIED`

**Date** 2026-10-10. **Status** accepted.

**Context.** `not_interested` has action `CLOSE` and no drafted reply. But the state machine only
allowed `SENT → REPLIED`, so closing a lead meant routing it through `REPLIED` first — a state in
`NEEDS_HUMAN`, which would put "we already have a vendor for this" in the review queue as though
there were something to decide.

**Decision.** `SENT → NOT_INTERESTED` and `FOLLOWUP_SENT → NOT_INTERESTED` are legal transitions.
`NOT_INTERESTED` stays in `RECOVERABLE`, so a human who disagrees can reopen it to `REPLIED`.

**Why this is safe.** `NOT_INTERESTED` is not a suppression: the address is not added to
`suppressions`, and the lead can be reopened. The states that must never reopen — `OPTED_OUT`,
`BOUNCED` — are unchanged and still reachable from anywhere.

---

## ADR-002 addendum — confirmed against the provider, 2026-10-10

ADR-002 kept the `[VERIFY]` that `ARCHITECTURE_TARGET.md` §9.5 put on Sent-folder recovery. It is
now verified, not assumed: a pre-generated `Message-ID`
(`<9ec2b628-…@athenaedu.in>`) was found in `[Gmail]/Sent Mail` by a readonly `BODY.PEEK` search
against the real Google Workspace mailbox, after an ordinary SMTP send. So
`recover_stuck_sends` can resolve a `sending` row by asking the mailbox, and `BCC_SELF` stays
optional. Two incidental findings worth keeping:

- Gmail's special folder names must be quoted (`"[Gmail]/Sent Mail"`); unquoted gives
  `BAD Could not parse command`. Handled by `_quote_folder`.
- The copy in Sent is made by the provider, not by us, so a mailbox on a provider that does *not*
  self-copy will fall through to `unknown` and ask a human — which is the correct outcome, not a
  regression.

---

## Minor decisions (inside spec latitude)

| Date | Decision | Why |
|---|---|---|
| 2026-10-09 | `uv` + `pyproject.toml` + `uv.lock`; delete `requirements.txt` | Spec asks for a lockfile; `uv` is already on the machine and pins the interpreter too |
| 2026-10-09 | Python pinned to 3.12.12 via `uv python install`; keep the existing 3.12.1 venv as found | Machine default is 3.14.3, which the spec forbids; the owner's existing venv already satisfies 3.12 so it was not clobbered |
| 2026-10-09 | Keep the legacy flat modules runnable through Phase 0, delete them module-by-module as `app/` supersedes each | Lets the owner keep using the prototype while the rewrite lands, and makes each deletion reviewable |
| 2026-10-09 | Skip the git history rewrite for `sample_leads.xlsx`; delete the file only | Both addresses are the owner's own and the repo is now private, so a force-push's risk exceeds its benefit. `git-filter-repo` recipe recorded in `RUNBOOK.md` if that changes |
| 2026-10-09 | Rename `docs/CLAUDE_CODE_MASTER_PROMPT.md` → `docs/MASTER_PROMPT.md` | Every cross-reference in the spec and in `ARCHITECTURE_TARGET.md` §4 uses the latter name |
