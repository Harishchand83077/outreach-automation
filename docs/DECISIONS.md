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
| 2026-10-10 | The worker's five tasks share a `WorkerTask` base whose `tick()` does one cycle and returns the next interval | Makes every loop testable with a fixed clock and no sleeping; `run()` is then thin enough that it needs no test of its own |
| 2026-10-10 | A tick that raises is recorded and retried, never propagated | A task dying while the other four carry on is the failure mode hardest to notice: the process stays up and the health endpoint still says "running" |
| 2026-10-10 | `send_message` and `classify_reply` are not registered job kinds | The `SendLoop` is the only sender (hard rule 1) and two runners draining a send queue would make the daily cap a race; classification is persisted in the same transaction as the message it labels (§10.3) |
| 2026-10-10 | `PooledUsageStore` opens a session per accounting call, for code that manages its own sessions | `DbUsageStore` binds to one session, which is right for a job but impossible for the ingest loop; without it the LLM daily cap would not apply to reply classification |
| 2026-10-10 | `SendLoop` takes the first campaign using a mailbox | One campaign per mailbox is today's shape. Interleaving several under one cap is a scheduling decision, better met loudly later than guessed at now |
| 2026-10-10 | Only asymmetric JWT algorithms are accepted; HS* is absent from the allowlist | A shared secret the API must hold to verify is a secret that can also sign. The allowlist is what stops the confusion attack where a token is HMAC-signed with the public key |
| 2026-10-10 | `aud` is not verified | Supabase issues `authenticated` for every signed-in user, so checking it distinguishes nothing and would be security theatre |
| 2026-10-10 | `org_id` is derived from the `members` row, never accepted from the request | A caller who can name an org is not thereby entitled to it; any endpoint taking an org from its caller is a tenancy bug waiting to be tried |
| 2026-10-10 | A valid token with no membership gets 403, not an empty result | Public sign-ups are disabled, so that case is an authenticated stranger and is worth saying plainly |
| 2026-10-10 | Rate limiting keyed on the token subject, falling back to the address only for public paths | Keyed on IP (C4), every client behind a proxy shares one bucket, so the limit is either useless or an outage |
| 2026-10-10 | Owner-only roles for now, with `admin` and `member` already in the check constraint | The owner's call (2026-10-10): invite-only comes later, and leaving the roles in the constraint makes that a row rather than a migration |
| 2026-10-10 | Cursor pagination, not offsets; the cursor is opaque but unsigned | An offset shifts under inserts, so paging while leads arrive skips rows. Nothing in the cursor is secret — it is a timestamp and an id the caller already received — and every query is still scoped by `org_id` |
| 2026-10-10 | List rows are summaries; bodies only on the single-item endpoint | C7 as a rule rather than a fix. Tests assert the absence of body fields so the rule survives new fields being added later |
| 2026-10-10 | A resource in another org is 404, never 403 | Confirming that an id is real somewhere else is itself a leak, on reads and on writes alike |
| 2026-10-10 | `LookupError` from a repository maps to 404, `ValueError` to 409 | They mean different things to a reviewer: "not yours or gone" versus "exists, but you are looking at a stale queue" |
| 2026-10-10 | Import `commit` re-reads and re-validates the file instead of accepting the preview's verdicts | A preview can be minutes old — somebody may have opted out since — or forged. Anything deciding who receives email is checked against the database at the moment of writing |
| 2026-10-10 | Research jobs are enqueued in the same transaction as the lead insert | A lead must never exist without the work that moves it forward, and a rollback should take both |
| 2026-10-10 | Enum-like column values are validated in the endpoint as well as the database | A check-constraint violation surfaces as a 500 and tells the caller nothing about what to send instead |
| 2026-10-10 | Magic-link sign-in only; no password field and no sign-up form in the dashboard | Public sign-ups are disabled and an invite is a `members` row, so a password would add a credential to phish, lose and rotate for an app with one user. The API never sees one either |
| 2026-10-10 | The sign-in confirmation is identical whether or not the address is a member | Otherwise the form is a free check of which addresses have access |
| 2026-10-10 | Phase 8 slice 1 stays in plain JS rather than converting to TypeScript at the same time | The endpoint rewrite is already a full rewrite of `App.jsx`; doing both at once would bury it. TypeScript and TanStack Query remain the target and are tracked as Phase 8 polish |
| 2026-10-10 | `vitest@^5` rather than the 3.x line | 3.2.4 pulls a `tinypool` with two critical advisories, and PR #15 cleared every frontend advisory. A test harness is not worth reintroducing them |

---

## ADR-009 — The worker's liveness is a row's *age*; infrastructure alerts belong to no org

**Date** 2026-10-11 · **Status** Accepted · **Phase** 9 · **Issues** E7, C6 (observability half)

**Decision.**

1. The worker writes a `worker_heartbeats` row once a minute and **also** logs the same snapshot.
   `/ready` reads the row and fails when it is **stale**, not when it is absent.
2. `worker_heartbeats` carries **no `org_id`**, and `alerts.org_id` is **nullable**, meaning "the
   system" rather than "unknown". These are the only two deviations from ADR-004's rule that
   every table carries `org_id`.
3. Alerts are written to the table first and pushed to `ALERT_WEBHOOK_URL` second, and a failed
   push never propagates to the caller.

**Why a row at all.** `app/worker/health.py` says, correctly, that health belongs in memory
because "a process that has died cannot write a row saying so". That argument is about the
*liveness signal*, and it holds: what monitoring watches for is the absence of recent log lines.
But `/ready` runs in the **api** process and cannot see the worker's memory. It needs something
shared, and what it reads is not the row's presence — it is the row's age, which a stopped worker
stops refreshing. The two mechanisms are the same signal expressed for two different readers, so
neither is redundant.

This closes a real gap rather than a theoretical one. Until now `/ready` checked only the
database, which means **an api serving perfectly while the worker was dead reported itself
ready** — and the symptom of a dead worker is that nothing is researched, drafted or sent, which
is invisible from the outside for hours. §18 asks for an external monitor on `/ready` precisely
so that this is what fails.

**Why `latest` rather than `all`.** Readiness asks "is at least one worker alive". Requiring
every row to be fresh would mean a removed container's stale row failed readiness forever. Rows
older than two days are pruned by the heartbeat itself.

**Why no `org_id` on a heartbeat, and a nullable one on an alert.** A process is not tenant data:
it serves every org, and `/ready` is unauthenticated so it has no org to scope by. Inventing one
would make readiness pick arbitrarily. Likewise a stalled worker or an unreachable database stops
**every** org's work at once; attributing it to one org would hide it from the others. So
`list_alerts` returns an org's own rows together with the system-wide ones, and `ack_alert`
accepts either. ADR-004's rule stands everywhere it describes tenant data, which is everywhere
else.

**Why the table before the webhook.** The webhook is optional and allowed to fail. An alert that
exists only inside a failed HTTP request is an alert nobody will ever see. And a push failure
must not abort the worker cycle that noticed the problem, or the system stops working *because*
it tried to say it was in trouble.

**Cost, accepted.** One extra write per minute per worker, and `/ready` does two queries instead
of one. An hour's cool-down per alert kind means a condition that changes in severity inside that
hour is reported once, at its first severity — acceptable, because the dashboard and the logs
carry the current state and the alert's job is only to make somebody look.

**Alternatives rejected.** (a) `/ready` ignoring the worker — the status quo, and the thing that
would have let a dead worker go unnoticed for a day. (b) The api probing the worker over HTTP —
the worker serves no HTTP, and giving it a port so it can be asked whether it is alive adds a
listening socket to the only process that holds the SMTP credentials. (c) A `system` org row to
own infrastructure alerts — a fake tenant that every query would then have to remember to
exclude. (d) Alerting on the first failed cycle — one failure is a blip, and alerting on blips
trains whoever receives them to ignore them; three consecutive failures is a condition.

**Tested by** `tests/integration/test_ops.py` (a fresh beat is ready, a stale one is 503, no beat
at all is 503, the freshest of several wins, a system alert is listed for every org, the cool-down
holds and an ack re-arms it) and `tests/integration/test_worker_heartbeat.py` (the beat survives
an unreachable database, one failure raises nothing, three raise a critical alert, and the alert
never carries the exception text — which is where a connection string would arrive).

## ADR-010 — The send mode is a database value with the environment as its default

**Status:** accepted, 2026-10-13. **Supersedes the second half of hard rule 1 in `CLAUDE.md`,**
which said the mode is set only in `.env` on the server. The first half — one function decides,
`disabled` is the default, nothing else may reach SMTP — is unchanged and is what the structural
test still enforces.

**Context.** `SEND_MODE` came only from the environment and was read once at process start.
That is safe and it is also unusable in the one situation that matters most: stopping. Pausing
an in-flight campaign meant an SSH session, an editor, and a container restart. During this
project SSH broke four times — three IP rotations on a residential connection and once because
the machine switched to IPv6 while the security group held an IPv4 `/32`. A kill switch that
depends on SSH being reachable is not a kill switch, and the owner asked for the control twice.

The obvious objection is the real one: a switch in a browser can be clicked by mistake, or by
somebody who has stolen a session, and what it turns on is mail to real strangers.

**Decision.** `orgs.send_mode`, nullable, with a check constraint. `NULL` means "use the
environment", so the migration changed no org's behaviour — a column with a default would have
silently become the authority the moment it was added.

Resolution happens in `effective_send_mode()`, in `app/services/mail/smtp.py` — **the same
module as the gate**. This is the part that was non-negotiable: the decision still resolves in
one place, and `tests/unit/test_send_guard.py`'s structural test over the whole tree still
passes. Reading the value from a column instead of the environment did not spread the decision
out. An unknown value resolves to `disabled`; the check constraint makes that unreachable, which
is precisely why it must fail closed.

**Every gate is on the write, not the read.** `PATCH /admin/sending-config` requires:

1. **`owner`, not `admin`.** Inviting somebody to help review drafts must not also hand them
   the switch that starts mailing strangers.
2. **`APP_ENV == "production"` for `live`.** This is `CLAUDE.md`'s "only in production" as code
   rather than as a habit, and it means a staging box restored from a copy of this database
   cannot be talked into sending.
3. **`confirm: "LIVE"`, typed.** So enabling real sending cannot be a mis-click, a replayed
   request, or a forged one that guessed a plausible body.
4. **An audit row** — `send_mode_set_by`, `send_mode_set_at`, and a WARNING log line.

Nothing about the gates lives in the guard. A guard that answers differently depending on who is
asking is a guard nobody can reason about.

**Consequences.**

- **Restricting is now instant and needs no shell.** `disabled` beats the allowlist and takes
  effect on the send loop's next cycle, within seconds. That is the whole point.
- **Enabling is harder than it was**, not easier: before, anyone with shell access and an editor
  could set `live` in `.env`. Now it additionally requires the owner role, a production
  environment and a typed confirmation, and it leaves a record of who did it.
- The environment variable remains the default and the value shown when no override is set, so
  a fresh deployment still starts `disabled`.
- **What this does not change:** approval is still required for every first email and every
  reply, the allowlist still applies in `allowlist` mode, and the send loop still re-checks
  suppression, the window, the cap and the lead's state immediately before sending.

**Proved by** `tests/unit/test_send_guard.py::TestTheOrgOverride` (no override uses the
environment, an override wins, an unknown value fails closed, disabling beats the allowlist, and
the sender honours it before a socket opens) and
`tests/integration/test_api_settings.py::TestChangingTheSendMode` (an owner may restrict, it
records who, `null` hands authority back, `live` is refused outside production, `live` needs the
typed confirmation, an unknown mode is a 422, and neither an admin nor a member may change it).

## ADR-011 — Company research is keyed on the website, kept for a quarter, and ages by hook

**Context.** Asked as a storage question: keep `domain_research` and save Tavily credits and
tokens, or flush it after 30 days and pay for the research again? The honest answer required
measuring, and the measurement changed the question.

**What it costs to keep everything.** `evidence_text` is capped at 20,000 characters and the
retention sweep nulls it after `RESEARCH_TEXT_RETENTION_DAYS` (30). What survives is the findings
and the source list: measured at **~2 KB** for a realistic row (four hooks with their quotes, a
company summary, five sources). At 100 emails a day — about 1,100 new companies a month in the
*worst* case, where no two leads share a company — keeping every company's research **forever**
costs about **3 MB a month, 40 MB a year**, plus a constant ~22 MB for the rolling month of
un-purged text. The instance has 23 GB free. **Storage is three orders of magnitude away from
being the constraint**, so it is not a tradeoff and should not be managed like one.

**And the old setting was the worst of both.** Nothing has ever deleted a `domain_research` row —
the retention sweep only nulls the text, and the lead-erasure path sets `domain_research_id` to
NULL rather than cascading. So the 30-day expiry did not save a byte. It only stopped the reuse.

**The real constraint is staleness, and it is not uniform.** What a company does, and what it is
built with, reads the same in April as in January. What it announced last week does not — and a
cold email opening on a stale "saw you're hiring two backend engineers" is *worse* than one that
opens on nothing, because the recipient knows whether it is still true. A single expiry date
cannot express that: a short one throws away the durable findings to protect against the
perishable ones, and a long one mails the perishable ones as current.

**Decision.**

1. **The cache key is the registrable domain of the company's website**, derived from
   `company_url`, not `leads.domain`. This was also a live correctness bug, not only a design
   choice: `leads.domain` is the domain of the address the founder writes *from*, a founder on a
   free-mail provider is imported with a warning rather than rejected (deliberately), so that
   column is routinely `gmail.com`. Every Gmail-addressed lead in an org therefore shared **one**
   cache row, and the second one was served the first company's findings — which the drafter goes
   on to cite as fact. An email to one company describing another is the worst output this system
   can produce, and it was one shared key away. Keying on the website fixes both directions: two
   unrelated companies never collide, and two founders at one company are one fetch however
   either of them happens to write to us.
2. **Rows are kept indefinitely** (as they already were), and `CACHE_DAYS` rises from 30 to
   **90**. A quarter is roughly how long "what they do" stays true.
3. **Hooks age, not rows.** Past `PERISHABLE_AFTER_DAYS` (30) a cache hit keeps only its durable
   hooks; `news`, `hiring` and `launch` are dropped, and the reviewer is told so in `gaps` rather
   than silently handed less. **If that leaves no hooks at all the row is treated as a miss** and
   the company is researched again, because the durable findings on their own are not worth an
   email.

**Consequences.** A company researched once is reused for a quarter instead of a month, which is
the saving that was asked for, and it is spent on freshness exactly where freshness matters. A
reviewer can see when a hook was dropped for age. The free-mail collision is gone. Nothing about
the storage question needs revisiting below several gigabytes of findings, which at these volumes
is a century away.

**Also fixed as part of the same root cause:** the Tavily fallback built its query as
`f"{lead.company_name or lead.domain} company news"`, so a nameless free-mail lead spent a credit
searching for *"gmail.com company news"* out of a budget of 1,000 a month. It now falls back to
the company's own domain, and searches for nothing when it has neither.

**Proved by** `tests/integration/test_research_service.py::TestDomainCache` — two companies
sharing an email domain do not share research; the same company is one fetch whatever its
founders write from; a stale hit keeps what lasts and drops what does not; a stale hit with
nothing durable left is a miss; a hit inside the month is untouched.

## ADR-012 — A campaign has a mode: research-and-draft, or render the operator's own template

**Context.** `ARCHITECTURE_TARGET.md` assumes one drafter: research the company, then ask a model
for a grounded email. That is the right default for agency outreach, where the opening line *is*
the email. It is the wrong tool for the other half of the work the owner wants this to do — a job
hunt, or any list where every recipient genuinely should receive the same message with their own
name in it. For those, researching each company spends a page fetch and a model call per lead to
produce findings nothing reads, against a free tier whose daily limit is the real throughput
ceiling.

**Decision.** `campaigns.mode` is `research` (the default, and the existing behaviour unchanged)
or `template`. A `template` campaign carries `campaigns.templates`: one initial email and up to
five follow-ups, written by the operator with merge fields, and **no model is called for a first
email or a follow-up**. Replies stay with the model in both modes — a reply has to answer what
the person actually said, and a template cannot.

**The mode changes who writes the email and nothing else about what happens to it.** Both
drafters return the same `DraftResult`, so a rendered email is stored as a draft row, approved by
a person, and sent by the send loop under the same cap, the same IST window, the same suppression
check and the same `approval_mode = review_all`. A test asserts a template-mode lead still cannot
reach `SENT` without passing through approval.

**What the renderer is careful about** (`app/domain/template.py`, pure functions, no I/O):

1. **An unresolved placeholder never reaches a recipient.** `Hi {{first_name}},` is the most
   recognisable failure of mail-merge and `Hi ,` is the second; both are worse than a generic
   opening because both announce that nobody read it. A placeholder either resolves, or has an
   explicit fallback (`{{first_name|there}}`), or the render **fails** and the draft is stored
   unsendable with the missing fields named. It is checked twice, independently: once at
   substitution, and once by the existing `_check_placeholders` looking at the finished text.
2. **Substitution is one pass and never recursive.** The template is the operator's, the values
   come from an uploaded spreadsheet. A company name of `{{booking_url}}` renders as those
   literal characters. One pass makes that true by construction rather than by escaping.
3. **Nothing is evaluated** — a field name and a literal fallback, no expressions, no attribute
   access. A templating engine would hand a spreadsheet a path into the process.
4. **Another tool's syntax is refused when the template is saved**, not when it is sent.
   `{first_name}`, `{{first name}}`, `%NAME%` and `${name}` are all shapes an operator arrives
   with, and accepting them means emailing them verbatim with a recipient as the first person to
   notice.
5. **A sheet column wins over our derived value, but an empty cell does not.** Somebody who
   mapped a `first_name` column knows their list better than `first_name_of` does; an unfilled
   column must not blank an email the full name could have filled.

**Validation is deliberately narrower for a rendered template** (`validate_rendered`).
`validate_draft` exists because a *model* was asked to write about a company, and most of its
rules catch invention: unresolved claims, numbers absent from the evidence, proper nouns never
mentioned, a subject over six words. **None of that applies to text a person typed** — hard rule
6 forbids the drafter inventing facts about the sender, and an operator is the authority on their
own offer. What still applies is everything about the shape of the message, because that is about
the recipient and about deliverability: unresolved merge fields, forbidden phrases and spam
triggers, plain text only, and at most one link which must be the booking URL. The word limit
becomes a *warning*: `max_words` is advice to a model, and an operator who writes 160 words has
decided to.

**`NEW -> DRAFTED` is now a legal transition**, for template mode only in practice. Routing a
template lead through `ENRICHING` and `RESEARCHED` to keep the path uniform would record research
that never happened, and the audit trail is the one thing that must not be decorative. The
self-transition ban is untouched.

**An unreadable template fails the job**, rather than skipping the lead or storing an empty
draft. Skipping reaches a terminal state, so fixing the template later would mean hand-recovering
every lead it touched; an empty draft puts a row in front of a reviewer that no editing can
rescue. Failing retries, eventually goes `dead`, and the scheduler already alerts on dead jobs —
while the lead stays `NEW` and needs only a re-queue. The route refuses to create a campaign in
that state, so reaching it means the row was edited by hand, which is exactly how a hand-edited
`send_days` once stopped sending for every mailbox.

**Consequences.** A thousand drafts in seconds for a list that wants one message, with the review
queue and bulk approval already built. The free-tier model budget is spent only where it buys
something. Two drafters is a second path to keep correct, which the shared `DraftResult` and the
shared send path limit to the drafting step itself.

**Proved by** `tests/unit/test_template.py` (40 tests over the renderer's rules),
`tests/integration/test_template_mode.py` (rendering, the state machine, and the handler with a
router that raises if it is touched), and `test_api_operations.py::TestCampaignMode` /
`::TestImportRoutesByMode`.

## ADR-013 — The persona's required fields belong to the drafter that needs them

**Context.** `Persona` required `sender_name`, `sender_role`, `agency_name`, `offer` and `goal`,
plus one of `booking_url`/`cta_text`, enforced by two model validators. Those requirements are
real, and they exist for one reason: the **research drafter** asks a model to write an email on
our behalf, so it has to be told who "we" are and what we are offering, or the model invents it —
which is issue A3, the worst thing the prototype did.

ADR-012 then added a second drafter. A `template`-mode campaign never calls a model for a first
email or a follow-up: the operator wrote every word. And the use the owner built it for is job
hunting, where there **is** no agency and no offer. The validators applied anyway, so the only way
through the form was to type something that passes — and a persona filled in to satisfy a
validator is a persona the drafter would go on to quote. The rule meant to stop invention was
requiring it.

**Decision.** `sender_name` stays required: it signs the message and it is in the footer, so a
recipient can always tell who wrote to them, which is a compliance requirement rather than a
nicety. `sender_role`, `agency_name`, `offer` and `goal` become optional, and the
booking-link-or-ask rule moves out of the model into **`Persona.missing_for_drafting()`**, which
returns each missing field *with why it matters*.

It is asked in exactly the places that need it:

- **`POST /campaigns` and `PATCH /campaigns/{id}`**, for a `research` campaign only, which is
  where the decision to have a model write the emails is actually made.
- **`handle_draft_lead`**, before the model is called, so a row edited by hand fails the job
  rather than producing an email written from a persona with no offer in it.
- **`GET /campaigns/{id}`'s `blockers`**, so a campaign in that state says so instead of
  silently drafting nothing.

**Why not a second model.** A `TemplatePersona` class would duplicate fifteen fields and two
validators, and the first change to either would be made to one of them. A `mode` discriminator
inside `Persona` would make the data know about the drafter, which is backwards: the drafter has
the requirement, so the drafter should state it.

**Why the messages say why.** "agency_name is required" sends somebody back to a form to type
anything that passes. "Name the company you are writing on behalf of — it signs the email" tells
them what the field is for. The difference matters more than usual here, because the field's
contents are quoted to strangers.

**Consequences.** One tool serves agency outreach and a job hunt without either compromising the
other's guarantees. A research campaign is still refused unless a model could write from it, now
with better errors. The footer already handled an empty `agency_name` — `build_footer` skips the
line — so nothing at send time changes. The reply drafter still refuses a campaign with no
persona at all, and will write from a thin one; a reply is reviewed by a person in both modes, so
the floor is the same.

**Proved by** `tests/unit/test_persona.py::TestWhatModelWrittenDraftingNeeds` and
`tests/integration/test_api_operations.py::TestWhatEachModeAsksOfThePersona`.

## ADR-014 — The queue stays in Postgres; no broker

**Context.** RabbitMQ was proposed, to make the pipeline more robust under crashes and load.

**Decision.** No broker. The queue stays a `jobs` table claimed with `FOR UPDATE SKIP LOCKED`.

**The reason is specific, not a preference.** Every job in this system is enqueued **in the same
transaction as the state change that justifies it**. The importer inserts a lead and its research
job together; research stores its findings and queues drafting together; a rejection marks the
draft rejected and queues the rewrite together. There is no window in which one has happened and
the other has not, which is why a crash anywhere in the pipeline loses no work and duplicates
none.

A broker destroys that property. `session.commit()` and `channel.publish()` are two systems with
no shared transaction — the dual-write problem. The standard remedy is the transactional outbox:
write the message to a table inside the transaction, then relay it. **That table is what we
already have.** Adding RabbitMQ means building an outbox to feed it, so the broker becomes pure
overhead on top of the component that was providing the guarantee.

The load numbers agree. At 100 emails a day the queue sees a few hundred jobs — about 0.005 jobs
per second. `SKIP LOCKED` handles tens of thousands per second. We are five orders of magnitude
from the technique's limit, and the ceiling we are actually near is the free-tier LLM quota,
which no broker affects.

Operationally it would be a fifth container to run, monitor, back up and reason about during an
incident, on a 2 GB instance, for a system whose entire durability story is currently "it is in
Postgres".

**What a broker would genuinely buy** — fan-out to several independent consumers, and
cross-language workers — we do not need with one Python worker.

**Consequences.** If throughput ever becomes the constraint, the upgrade path is, in order: a
second worker process (free, and already safe because of `SKIP LOCKED`); `LISTEN`/`NOTIFY` to cut
polling latency (free, built in); and only then a broker, at which point this ADR should be
revisited with measurements rather than estimates.

## ADR-015 — The lifecycle has a producer: follow-ups are queued, and silence ends a lead

**Context.** Two locked product rules had every component except the one that starts them.
`SendLoop` set `leads.next_action_at`, `followup_due_at` computed the due date, the state machine
had `SENT -> FOLLOWUP_SENT`, `CampaignSettings` had `followup_after_days` and `max_followups`, and
`find_candidates` looked for an approved `Draft(kind="followup")` — **and nothing ever wrote one**.
`no_response_at` computed when a silent lead could be closed and no code wrote `NO_RESPONSE`.

So "one follow-up three calendar days after the first email" had never happened, and a lead that
never replied stayed `SENT` for ever — which also kept it out of retention, since retention only
removes terminal leads.

**Decision.** `app/services/lifecycle.py` is the missing producer, run by the `Scheduler` each
cycle in its own transaction, never fatal to the rest of the cycle.

- **Follow-ups** are queued as `DRAFT_FOLLOWUP` jobs for leads in `SENT` whose `next_action_at`
  has passed and which have not had every follow-up their campaign allows. Idempotent three ways:
  a `dedupe_key` per lead per follow-up number, a check for a live follow-up draft already
  waiting, and `followups_sent` read from the lead rather than the payload — so a job that waited
  in the queue while something else sent cannot write the wrong number.
- **Silence closes a lead** only after it has had everything we were going to send:
  `followups_sent >= max_followups` and `no_response_at` passed. A lead with an inbound message
  newer than our last send is skipped, because closing one whose reply is still being classified
  would throw the reply away.

**The follow-up is drafted, never auto-sent.** `approval_mode = review_all` applies to it exactly
as to a first email, and the draft travels the same path: review queue, human approval, send loop,
cap, window, suppression. Template mode renders the operator's own follow-up text; research mode
asks a model, reusing the hooks the research step already verified so the follow-up can refer to
the same specific thing.

**A campaign may allow more follow-ups than it has templates**, since the two are set in different
places. The smaller wins and the job fails permanently rather than retrying, because a missing
template is not a transient condition.

**Consequences.** The lifecycle terminates. `final_wait_days` means something. Retention can
eventually reach leads that went quiet. The daily cap now covers real follow-up volume, which at
steady state roughly doubles sends per new lead — which is what the cap was always sized for
(§9.2: "at steady state each lead costs two sends").


## ADR-016 — A campaign may attach one file, stored in Postgres, off by default on follow-ups

**Status.** Accepted. **Date.** 2026-10-25.

**Context.** The owner asked to upload a document when creating a campaign, send it with that
campaign's emails, and be able to untick it for an individual email. Two of the three use cases
this system now serves want it: a funding outreach campaign wants to attach a deck, and the
job-hunting use that ADR-013 opened up wants to attach a CV.

**Decision.**

**One file per campaign, in its own table, as `bytea`.**

- *Its own table, not a column on `campaigns`.* `campaign_summary` reads a campaign row to draw
  every row of the campaigns list; a blob column would be dragged along by all of it, several
  megabytes at a time, for a screen that wants a name and a status. In its own table the bytes
  are read exactly once per send and never otherwise — `describe()` answers the UI's questions
  from the columns beside the blob.
- *Postgres, not S3.* One capped file per campaign is a rounding error against 23 GB free; it is
  already covered by the nightly `pg_dump`; and an object store adds a bucket, an IAM policy and
  a second place for the truth to live, where the database can say a campaign has an attachment
  and the store disagree. Hard rule 5 applies too — no new cloud resources without asking. The
  thing that would change this is attachments becoming large or numerous, and the 5 MB cap is
  what keeps that from creeping up unnoticed.
- *Replacing, not versioning.* A second row means deciding which one a send uses, and the honest
  answer — "the newest" — is what one row already says.

**The type allowlist is the only real control, and it is stated as such.** Nothing here scans
file contents; there is no scanner on this box and adding one is new infrastructure. So the
extension decides, against an allowlist of documents and images, and the `Content-Type` we state
is **ours** rather than the one the browser claimed — telling a recipient's client
`application/pdf` about something that is not one is a thing we should not do. Executables,
scripts, macro-enabled Office formats, the pre-2007 binary Office formats and archives are
refused *by name*, so the error says why rather than "unsupported", which reads like a gap
instead of a decision.

**Who carries it: first emails yes, follow-ups no, replies only on request.**

- A **follow-up** does not re-send several megabytes into a thread that already has a copy. It is
  noise for the recipient, and an attachment repeated on every message of a thread is a pattern
  filters score badly. A campaign may turn it on (`attach_followups`), because "deck attached
  again for convenience" is a reasonable thing to want and not something to refuse.
- A **reply** never carries it automatically and there is no campaign setting for it. Whether a
  document belongs in an answer is a judgement about that one message, so the reviewer ticks the
  box — which is exactly the case where somebody has *asked* for the deck.

**The per-draft override is a stored nullable column, not a UI state.** `drafts.attach` is NULL
until somebody expresses an opinion, and `should_attach()` holds the "NULL means the campaign
decides" rule in one place, read by the send loop and by the review queue alike. Stored, because
the send loop reads it minutes or days later and because a decision a reviewer made has to be
auditable. It is refused once a draft is approved, sent or superseded: a control that silently
did nothing would be worse than one that says no.

**Resolved at send time, not at approval.** Replacing the file changes every email not yet sent,
which is what an operator who has just fixed their deck expects; the ones already gone keep what
they carried, because that is what happened. Deleting the attachment does not block a campaign —
those emails go without it, and refusing to send them would be the system breaking a campaign
over an absent nicety.

**What this does not change.** The message is `text/plain` byte for byte when there is no
attachment, and `multipart/mixed` whose *first part* is that same `text/plain` when there is. The
locked rule "plain text only, no HTML body" holds: there is no `text/html` alternative anywhere,
the footer and `List-Unsubscribe` are untouched, and a client that cannot read MIME still shows
the text. `tests/unit/test_message_attachments.py` asserts each of those.

**Consequences, and the one worth saying out loud.** **An attachment makes a cold email
materially more likely to be filtered as spam**, and this system is still sending from an address
whose domain it is not authenticated for (`GO_LIVE.md` §1) — the reply that went to Spam was
already the symptom. So the UI says, where somebody is about to upload, that a link is usually
the better opening. The feature is built because it was asked for and both use cases are real;
the deliverability advice belongs next to the button, not in place of the button.


## ADR-017 — One org per sign-up, and sign-up is closed by default

**Status.** Accepted. **Date.** 2026-10-25.

**Context.** Access was a `members` row inserted by hand with SQL. Correct for one person,
impossible for ten. The ask was that anyone can sign up, run their own campaigns, and never see
anybody else's data.

**Decision.** A new sign-up **creates a new org** with that person as its **owner**, provisioned
on their first authenticated request.

Everybody joining one shared org as `member` was rejected. Separate orgs give complete
separation through the mechanism that is already load-bearing and already tested: `org_id` on
every table since the first migration (ADR-004), an `OrgContext` on every repository call, and
`deps.py` resolving the org from the membership row and **never** from the request. Roles inside
an org answer a different question -- who on *your own team* may change *your own* sending rules
-- and reusing them for tenancy would have been a second, weaker answer to a settled question.

**Provisioning happens in `current_user`, not at a `POST /signup`.** A separate endpoint is a
second door into the same room: the dashboard has to know to knock on it, and anything that
forgets sees a 403 it cannot explain. One path, and `SIGNUP_MODE` decides whether it ends in a
workspace or a refusal.

**`SIGNUP_MODE` defaults to `closed`**, which is exactly the old behaviour. This matters more
than it looks: a migration that quietly opened public sign-up on a system that sends email would
be the worst default this project could ship. Same reasoning as `orgs.send_mode` being nullable
in ADR-010.

`MAX_ORGS` (25) is a ceiling, because the failure mode of open sign-up here is not a breach --
it is strangers quietly exhausting one shared free LLM tier until the people who actually use the
tool get nothing. Refusing the next person with a sentence beats degrading for everybody.

**Consequences.** Ten people, ten workspaces, no cross-tenant work needed. The two things that do
*not* scale are named in `MULTI_TENANT_PLAN.md` §6: the free LLM quota belongs to one API key
(template mode calls no model, which is why it is the mode that scales), and one worker process.
Neither is a correctness problem and both have known fixes.

---

## ADR-018 — Mailboxes authenticate per org, by OAuth, over SMTP and IMAP

**Status.** Accepted. **Date.** 2026-10-25.

**Context.** `SMTP_EMAIL` and `SMTP_PASSWORD` came from the environment: one sending account for
the whole deployment. Ten people need their own, and asking each to paste an app password is both
unpleasant and a worse security position than the alternative.

**Decision.** "Connect Gmail" runs a Google OAuth flow. We store an encrypted **refresh token**
per mailbox. The user never types a password into our software and can revoke us from their own
Google account page.

**Scope `https://mail.google.com/`, over SMTP and IMAP with XOAUTH2 -- not the Gmail API.** That
scope authorises exactly the two protocols we already speak, so every line of the send loop,
outbox, IMAP poller, reply matcher and ADR-002 recovery pass keeps working. Moving to the Gmail
API would mean rewriting all of it to reach the same place, and the API cannot read the Sent
folder, which is what ADR-002 depends on to resolve a send whose outcome we never learned. One
function changed: how the clients authenticate.

**The scope costs something and it is written down rather than discovered.** Google calls
`gmail.send` *sensitive* (free brand verification) and anything that reads a mailbox
*restricted* -- annual third-party security assessment, real money, and hard rule 5 says no. An
**unpublished** client needs neither: 100 test users, any scope. The price is refresh tokens that
expire after **7 days**.

**That expiry is handled as a product state, not hidden.** The refresh fails, the mailbox is
paused with a reason, and the dashboard shows Reconnect. A weekly click for ten people is
acceptable; silence would not be. `MULTI_TENANT_PLAN.md` §4 records both exits -- pay for CASA,
or drop to `gmail.send` only and route replies to a `Reply-To` on our own domain.

**Encrypted with Fernet, key in the environment.** The nightly dump ships to S3; a refresh token
in plain text in it is a Gmail account in a backup. No route returns the secret, no log line
contains it, no event payload carries it, and both `Sealed` and `MailLogin` override `__repr__`
so a traceback cannot print one. Losing the key means every mailbox reconnects -- a click each,
and strictly the better failure.

**Its own table**, not columns on `mailboxes`: that row is read on every send-loop cycle and a
secret should not be dragged through a query that does not need it (the ADR-016 reasoning), and
one table holding all the secrets is a single obvious thing to reason about.

**A missing credential falls back to the environment.** This is what keeps the live deployment
sending while this lands, and what keeps every existing test meaningful: a mailbox with no
credential row behaves exactly as every mailbox did before.

**The send gate does not move.** `effective_send_mode()` and `allowlist_permits()` stay in
`smtp.py` and decide for every send, whoever it belongs to -- hard rule 1, and the structural test
over the whole tree still passes. This ADR answers *as whom*, never *whether*.

---

## ADR-019 — A campaign's schedule moves its status, rather than every reader checking a clock

**Status.** Accepted. **Date.** 2026-10-25.

**Context.** A campaign could be set up but not started, and started but never stopped. Both meant
somebody remembering, and "stop on Friday" meant running through the weekend if nobody was
looking.

**Decision.** `campaigns.starts_at` and `ends_at`, both nullable, and the `Scheduler` moves
`status` when a boundary passes. A new status `scheduled` joins the check constraint.

**Not a time window added to every query.** Three places ask whether a campaign is running -- the
send loop's candidate query, the lifecycle pass and `repo.due_followups` -- and adding
`starts_at <= now < ends_at` to each would be three copies of one rule. That is precisely the
shape of defect this project has produced seven times. One writer moves the status; every reader
goes on asking the question it already asked.

The cost is up to a minute of lag. For something whose unit is a calendar day, that is not a cost.

**`paused` is never overridden.** Pausing is a human saying stop, and a schedule that could undo
it would make the pause button untrustworthy. `completed` and `archived` are likewise never
restarted -- a campaign that has finished does not resume because somebody left a date on it.

**The end is checked before the start**, so a window entirely in the past completes rather than
briefly running. Without that ordering, saving a backdated schedule would start sending.

**Consequences.** Both nullable means every existing campaign is untouched, which is what let this
ship without a data migration. `scheduled` rather than leaving it in `draft` because `draft` reads
as "not finished being set up", and a status that lies about why nothing is happening is the thing
this whole area of work exists to stop.

## ADR-020 — The environment mailbox belongs to one org, and every other mailbox must connect

**Status.** Accepted. Supersedes the fallback described in ADR-018's "a mailbox with no
credential falls back to the environment", which this narrows rather than removes.

**Context.** ADR-018 gave each mailbox its own credentials and kept a fallback: a mailbox with no
`mail_credentials` row authenticates with `SMTP_EMAIL`/`SMTP_PASSWORD`. That was deliberate and
correct at the time — it is what kept the live deployment sending while per-mailbox credentials
landed, and it is what kept every existing test meaningful.

ADR-017 then made sign-up self-service. The two together produce a case neither anticipated. A
stranger signs up, creates a campaign, types their own address as `from_email`, and never
connects anything. `login_for` finds no credential and hands back **the operator's Gmail**. Their
campaign then sends through the operator's account: the operator's daily cap, the operator's
sending reputation, the operator's SPF and DKIM alignment, on mail the operator never saw and
cannot find afterwards. The `From` header shows the stranger, so nothing in the recipient's
mailbox says otherwise either.

The same hole exists on the way in, and is worse. `ImapPoller` never called `login_for` at all —
it read `IMAP_EMAIL`/`IMAP_PASSWORD` directly — so `ImapLoop`'s careful once-per-mailbox pass
logged into the *same inbox* every time. A second workspace's replies, bounces and opt-outs sit
in their own inbox and were never fetched; meanwhile the operator's personal mail was ingested
into the stranger's org as orphaned inbound messages. **An opt-out nobody can read is an opt-out
we will breach.**

**Decision.**

1. **`orgs.uses_environment_mailbox`**, a boolean with no API route that sets it. The migration
   grants it to the oldest org — on this deployment the single `members` row inserted by hand in
   `DEPLOY.md` §4.1, long before anyone could sign up — and to nobody else, ever.
2. **The address must also match `SMTP_EMAIL`.** An owner may hold several mailboxes and only one
   of them is the account those credentials log in as. Sending `rahul@arkvane.in` through a Gmail
   authenticated as somebody else is the mismatched-envelope problem already on record: nothing
   bounces and the message is simply unauthenticated against a domain whose records say otherwise.
3. **Both conditions, not either.** `uq_mailboxes_org_from_email` is scoped *per org*, so a second
   workspace may create a mailbox named after the operator's own address. An address-only rule
   would hand that stranger the password — the guard would be the attack.
4. **`ImapPoller` takes a `MailLogin` per call**, authenticating with XOAUTH2 when the mailbox has
   a token. ADR-018's choice of SMTP/IMAP over the Gmail API is what makes this a parameter rather
   than a rewrite.
5. **Refusal is `MailboxUnavailable`**, which both loops already know how to contain: the send
   loop pauses that one mailbox with a reason, the IMAP loop skips it and counts it, and every
   other mailbox keeps its turn.

**Why a column rather than inferring it.** Every alternative is a guess that a stranger can
influence: the oldest org (fine until a workspace is created during a migration), an org with no
`mail_credentials` (true of every new signup), `secret_ref` (the API accepts it from the caller).
Ownership of a credential is a fact, and facts belong in a column.

**The dashboard asks the same question, without sending anything.** `unavailable_reason()` is
what `_blockers` calls, and `login_for` uses the same helper for the case they share. The obvious
implementation — have the blocker call `login_for` — would mint an access token from inside a
`GET`, so refreshing the campaign screen would make a request to Google and could write an
`invalid_reason`. A second copy of the rule would have been this project's signature defect for
the eighth time.

**Consequences.** A single-tenant install is unchanged: one org, one mailbox, the address in
`.env`, and nothing about this is visible. A second workspace must connect Gmail before it can
send or receive — which is the product behaviour that was intended all along, and was previously
achieved by accident in the worst possible way. The campaign screen names the reason, so "I
approved a draft and nothing happened" stays answerable.

---

## ADR-021 — A mailbox says whether it works, and when it will stop

**Status.** Accepted, 2026-10-26.

**Context.** Two questions had no answer, and both were asked by somebody operating the system
rather than reading it.

*"Did Connect Gmail actually work?"* The only way to find out was to approve a draft and wait for
the send loop — which, with `SEND_MODE=disabled`, answers nothing at all. A person signing up
could connect their account, see a green tick, and have no way to distinguish a working connection
from a broken one until their first campaign quietly did not go out. The request was phrased as
*"a test email to his other address, just to check the connection is well established"*.

*"When does this connection lapse?"* ADR-018 leaves the OAuth client **unpublished** on purpose —
Google calls reading a mailbox a restricted scope, publishing needs an annual paid security
assessment, and hard rule 5 says no — and the price is that refresh tokens expire **seven days**
after consent. That was written down there and handled as a product state, but only *after* the
fact: the first anybody knew was a paused mailbox, which in practice means a campaign that stopped
on Friday and replies nobody fetched over the weekend.

**Decision.**

**1. Checking sends nothing, and therefore needs no exception.** `POST /connect/google/verify`
authenticates over SMTP and stops before `MAIL FROM`, then authenticates over IMAP and opens INBOX
`readonly=True`. No envelope, no recipient, no message — so `SEND_MODE` has nothing to decide and
hard rule 1 is untouched. That is what makes it safe to put behind a button anybody may press, and
it is why the check works on a deployment where sending is switched off, which is every deployment
before go-live.

**The two halves are checked separately rather than one inferred from the other.** A mailbox that
cannot send says so within a cycle, because a campaign stops and somebody notices. A mailbox that
cannot be *read* looks perfectly healthy: mail goes out, and replies, bounces and opt-outs simply
never arrive. An opt-out nobody can read is one we will breach, so the receiving half is the half
worth checking explicitly.

**2. The test message is a real email and is gated like one.** `send_test=true` sends one message
through `SmtpSender.send`, unchanged. With sending disabled it comes back `blocked` and the report
says so in a sentence. **No exception is carved out**, because the value of hard rule 1 is that
there are none — a "but this one is only a test" path is how a gate stops meaning anything, and
the test message is worth strictly less than the rule.

**It goes to the mailbox's own address and there is no parameter naming a recipient.** That needs
no allowlist entry per person, cannot mail anybody who did not ask for it, and still proves what a
self-test can prove. An authenticated endpoint that mails an arbitrary address on request is the
prototype's worst route (issue C2) wearing a better name.

**3. `mail_credentials.granted_at`, and warnings at one day and at zero.** Neither existing column
could answer when the seven days started: `created_at` stops moving once the row exists, so a
mailbox reconnected this morning looked as stale as it did yesterday, and `last_refreshed_at`
moves every hour as access tokens are minted, so it describes a token that was never the one that
expires. `granted_at` moves on consent and nowhere else.

The sweep runs hourly in the `Scheduler`, never fatally, and raises **one alert per mailbox per
threshold per day**. The mailbox id is in the alert *kind*, not only in its detail, because the
cool-down dedupes by kind — without it the first mailbox warned would silence every other mailbox
in the deployment for twenty hours, and the one that never got its warning is the one that stops.

`days_left` rounds **up**, so a connection with eleven hours left reports 1 rather than 0. A
countdown that reaches zero while the thing still works is one nobody believes the next time it
says zero.

**4. One calculation, read by both.** `grant_status()` is a pure function over a credential and a
clock. The dashboard's sentence and the alert's threshold come from it, rather than from a rule
implemented once for the screen and once for the sweep — which is this project's signature defect
and would have been its ninth appearance.

**Alternatives rejected.**

- **Emailing the warning to the mailbox owner.** It is a real email, so it is gated, so on a
  deployment with sending disabled the warning about the connection would itself not send. A
  notification channel that fails in exactly the configuration where it is needed is not one.
- **Letting the caller name the test recipient.** See above: that is C2.
- **Inferring IMAP health from SMTP.** They are different servers and different failures, and the
  silent one is the one that would have been inferred rather than checked.
- **A `GET` for the check.** Resolving a login refreshes an expired access token and stores it, so
  a `GET` would be a read path with a side effect fired by anybody pressing F5 — the mistake
  `unavailable_reason()` exists to avoid.

**Consequences.** `GOOGLE_GRANT_DAYS` defaults to 7 and `0` means "never expires", so the day the
OAuth client is published the countdown and the warnings stop with a configuration change and no
code change — which is the exit `MULTI_TENANT_PLAN.md` §4 promised. `check_env` reports the value
in the Safety block, because an operator who does not know this number reads a weekly reconnect as
a fault.

A latent bug surfaced while writing this and is fixed here: `MailLogin` was imported in `smtp.py`
only under `TYPE_CHECKING`, so `send(login=None)` — the documented fallback to the environment
account — raised `NameError` rather than taking it. Nothing reached it, because `login_for` never
returns `None`, which is exactly why it went unnoticed.

---

## ADR-022 — An invitation, because the order mattered and should not have

**Status.** Accepted, 2026-10-26.

**Context.** Adding a teammate to an existing workspace meant one `INSERT` into `app.members`
against production, and it had to happen **before** that person's first authenticated request.

Afterwards, `provision()` sees no membership, creates them a workspace of their own and makes them
its owner (ADR-017). Writing the `members` row at that point does not fail: `uq_members_org_user`
is scoped per org, so the database accepts a **second** membership for the same `user_id` in a
different org. `deps.current_user` then resolves them with `select(Member).where(user_id == sub)`
and `scalar()`, which returns whichever row the database happens to hand back first.

So the failure was not an error. It was a person seeing one workspace on Monday and a different
one on Tuesday, with nothing on screen admitting that two existed — and before that, the much
more ordinary symptom of signing in, landing in an empty org, and reporting that every campaign
had vanished.

An earlier note in this project's own documentation said the second insert would be refused by a
unique constraint. It would not. There is no global unique index on `members.user_id`, and the
absence of one is exactly what makes this worth a table rather than a warning in a runbook.

**Decision.** `app.invitations` — org, address, role, who sent it, and when it was accepted or
revoked. `provision()` consults it **before** anything else, and an address with a pending
invitation joins that org with that role instead of getting a new one. Nothing has to happen in a
particular order any more, which is the entire point: a rule that depends on somebody remembering
a sequence is a rule that will be broken by whoever did not read the runbook.

**An invitation overrides `SIGNUP_MODE=closed`.** An admin naming an address deliberately is a
stronger statement than a global policy, and the default this project ships is `closed` — without
the override, the safest configuration would also be the one in which nobody can add their own
team. A stranger with no invitation is refused exactly as before; there is a test for each.

**Matched on the email address, which is a stated limit rather than an oversight.** A Supabase
`sub` does not exist until the first sign-in, so the address the identity provider reports is the
only thing available. Whoever controls the invited address gets the seat — the same assumption
every invitation email makes. It is not a bearer token and it is not secret: a pending invitation
grants nothing until somebody authenticates as that address, so the security boundary is still
Supabase.

**One pending invitation per address across the whole deployment**, enforced by a partial unique
index. Per-org would allow two workspaces to hold an invitation for the same person, and the
winner would be decided by which of them signed in first. Refusing the second invitation is a
sentence somebody can act on; losing a race is not. The index is partial so that a used or revoked
row never blocks a fresh invitation — re-inviting somebody who left has to work.

**Inviting somebody who already has a workspace is refused, not absorbed.** That is the failure
this table exists to prevent, and silently accepting it at the invitation end would recreate it at
the acceptance end. The way out is a decision — they keep their workspace, or an admin deletes
their membership and their org and invites them fresh — and it destroys data, so nothing does it
automatically.

**You cannot invite above your own role.** Without it the door built to let people in is a
privilege escalation: an admin invites an owner, who may then change the send mode (ADR-010).

**Removal has two refusals**, both about leaving a workspace reachable: you cannot remove yourself,
and the last `owner` cannot be removed, because an org with no owner has nobody who may change its
send mode. A removed member's work stays — a member is a key, not a container, and deleting the
campaigns and messages of somebody who left would destroy the record of what was sent to whom,
which `PRIVACY.md` requires to stay attributable.

**Alternatives rejected.**

- **A signed invitation token in a link.** More moving parts (expiry, replay, a second place
  secrets live) for no extra safety: the link would still be honoured only for whoever can
  authenticate as the address, which is what the address match already says.
- **Letting a person hold memberships in several orgs.** A larger change than it looks — every
  request would need to know *which* org, and `deps.py` would have to take it from the caller,
  which ADR-004 exists to forbid. One workspace per person stays.
- **Making `members.user_id` globally unique instead.** It would turn the silent failure into a
  loud one, which is better, but it would still leave no way to add a teammate who has already
  signed in, and no way to add one without SQL. It is worth doing as well; it is not a substitute.

**Consequences.** `docs/USERS.md` §B is now a dashboard flow rather than a `psql` session, and the
by-hand escape hatch writes an invitation rather than a membership for the same reason. A
deployment that never creates an invitation behaves exactly as it does today — the migration is
one new table and reads nothing.

---

## ADR-023 — The deployment has no mailbox of its own

**Status.** Accepted, 2026-10-26. Supersedes ADR-020's narrowed fallback.

**Context.** This started as one agency's internal tool, so one sending account in the environment
— `SMTP_EMAIL`, `SMTP_PASSWORD`, `IMAP_EMAIL`, `IMAP_PASSWORD` — was exactly right. ADR-018 added
per-mailbox OAuth for other people; ADR-020 found that the *fallback* to those environment
credentials had no owner and narrowed it to one org and one address.

Narrowing it was the right fix for the bug and the wrong shape for the product. The owner's
intent, stated plainly: *"now there is no single user .ecell or arkvane.in — I can connect
whatever email I want to send from."* As long as one account lives in `.env`, the system has an
identity that belongs to the deployment rather than to a person, and every question about it
(who may use it? which org? which address? what if two match?) is a rule somebody has to hold in
their head and a guard somebody has to maintain.

The same was true of `SEND_ALLOWLIST`, one environment variable for the whole deployment. A new
workspace could select `allowlist` mode and find no way to put its own address on the list, while
reading the operator's personal addresses out of its own settings screen.

**Decision.**

**1. There is no environment mailbox.** `SMTP_EMAIL`, `SMTP_PASSWORD`, `IMAP_EMAIL` and
`IMAP_PASSWORD` are gone from `Settings` entirely, along with `orgs.uses_environment_mailbox` and
both fallback paths. A mailbox with no credential cannot send and cannot be read, **including the
owner's own**. `SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST` and `IMAP_PORT` stay: they describe Google's
servers, not anybody's identity.

This is a deliberate, visible loss of convenience. What it buys is that **there is no identity in
the system that is not somebody's own**: nobody can send on an account they did not personally
authorise, no configuration mistake can put a stranger's campaign under the operator's address,
and "whose Gmail sent this?" has exactly one answer — the credential row.

**2. `CREDENTIAL_KEY` and the three `GOOGLE_*` values become required.** What a deployment must be
able to do is no longer *send*; it is *let somebody connect their own account*. A deployment
missing them has a dashboard on which nothing can ever send, which is a startup failure rather
than a surprise at the first campaign.

**3. The allowlist is the org's rows, not the environment's string.** `app.send_allowlist`, edited
in Settings by an `admin`. **The decision did not move**: `allowlist_permits()` in `smtp.py` is
still the only function in the tree that compares a recipient against anything, and the structural
test over every file still asserts it. Only the *values* now travel in, which is precisely the
trade ADR-010 made when the mode moved out of the environment.

`None` and an empty list mean the same thing, deliberately: "we could not load the list" and "the
list is empty" must have one answer, or a database hiccup becomes a send to somebody who was never
on it.

**4. One connect option, not two.** The owner asked whether a Workspace address and an ordinary
Gmail need separate flows. They do not: both are the same OAuth client, the same
`https://mail.google.com/` scope and the same XOAUTH2 handshake, and the address is whatever
Google says the account is. A second option would be a choice with no consequence, which is a
choice people get wrong. (A genuinely different second path — app passwords for a non-Google
provider — would need per-mailbox host and port. `MailCredential.kind == "app_password"` already
exists for it; nothing exposes it yet.)

**Who may do what.** Adding an allowlist entry is `admin`; going `live` stays `owner` (ADR-010).
They are different in kind rather than degree: an allowlist entry widens sending by one address
somebody typed, inside a mode whose whole purpose is rehearsal, while `live` removes the gate.
Treating them the same would mean either handing out `owner` or making rehearsal impossible
without it.

**Consequences, and one of them is immediate.** **The existing deployment stops sending the moment
this lands**, because its mailbox has no credential — it had been using the environment account.
It resumes on one click of Connect Gmail. That is the change working, not a cost of it, but it is
a live outage of a running campaign and must not be discovered rather than expected.

The migration backfills `SEND_ALLOWLIST` from the process environment into the oldest org, because
the alternative is a deploy that silently empties the list of a deployment rehearsing in
`allowlist` mode — correct by the new rules and indistinguishable from a break. Nothing is logged
but a count.

`check_env` no longer counts the allowlist. It cannot: the list is per org and in the database,
and a number that is right for one workspace and wrong for every other is worse than a sentence
saying where to look.

**Alternatives rejected.**

- **Keeping the environment account for the owner's org only** (ADR-020 as it stands). It works
  and it is tested, but it leaves one org whose mail cannot be attributed to a person, and every
  future question about sending identity has to carry the exception.
- **Keeping `SMTP_EMAIL` as an unused field** so old `.env` files look unchanged. A setting
  nothing reads is a trap: the next person to find it assumes it still means something.
  `extra="ignore"` means a stale `.env` starts fine anyway.
- **Per-org `SMTP_HOST`/`PORT`.** Only needed for a non-Google provider, which nothing supports
  yet. Adding the columns now would be configuration nobody can use.

---

## ADR-024 — A platform scope above orgs, read-only and aggregates only

**Status.** Accepted, 2026-10-28.

**Context.** Asked as a roles question: the owner of the deployment is one person, everybody else
is a member, so why can the owner not see everything?

The answer is that the premise does not survive `SIGNUP_MODE=open`. A sign-up provisions its own
workspace and owns it (ADR-017), which is the product — one user must not see another's leads. So
"the owner sees everything" cannot mean one shared org with everybody in it; that would delete
the isolation the whole schema is built around, and it would mean every future customer's
campaigns sat in the same tenant as ours.

But the person running the deployment genuinely has no screen. Every admin route answers for
exactly one org, correctly, and the questions that actually matter to an operator — how many
workspaces exist, whether their mailboxes still authenticate, whether the worker is wedged, how
much of the free-tier model quota is gone — cannot be asked of any of them. The last one is not
hypothetical: a workspace whose OAuth grant lapsed has silently stopped sending **and** stopped
fetching replies, and looks completely healthy from inside.

**Decision.** A third scope, above orgs rather than inside one.

| Audience | Sees |
|---|---|
| **Platform operator** — `app.platform_operators`, granted by SQL | every workspace in aggregate: counts, mailbox connection health, send mode, quota burn, dead jobs, unacknowledged alerts, worker liveness |
| **Org owner / admin** | their own workspace: team, invitations, mailboxes, send mode, allowlist |
| **Member** | no admin screen at all: the review queue and the leads |

One route, `GET /platform/overview`, and it is read-only. There is no write route in the module
and no route anywhere that takes an org from its caller.

**Not a role on `members`, and that is the load-bearing choice.** A role is a position *inside*
one workspace — `uq_members_org_user` is per org, and `deps.current_user` resolves exactly one
membership. Reusing `role` for a scope that sits above every workspace would make the word mean
two different things depending on which row you were reading, and it would require the operator to
hold a membership somewhere: so the scope above orgs would depend on being inside one,
`provision()` could hand a workspace to somebody who only ever wanted to read the dashboard, and
`SIGNUP_MODE=closed` would lock out the person who runs the deployment. Two separate questions,
two tables, two lookups.

**The containment is the type, not the route's good manners.** `CurrentOperator` has **no
`org_id` and no `ctx`**. `CurrentUser.ctx` is how every org-scoped route obtains the `OrgContext`
that every repository call demands, so an identity that has none cannot reach a repository call at
all — a route that tried to pass an operator where a user belongs fails on the missing attribute
rather than quietly querying somebody's leads with an org from who knows where. It is not a
subclass of `CurrentUser` either, which would hand it `ctx` by the back door, and it is frozen so
nothing can write an org onto it mid-request. Three tests assert exactly that, because it is a
property of the shape rather than of any response.

**ADR-004 keeps holding because the exception is narrow and enforced from three sides.** The
platform router is the only code in the tree that deliberately reads across orgs. So: it does not
import `app.db.repo`; a structural test greps it for the column names that would carry somebody's
content (`email_norm`, `company_name`, `.body`, `.subject`, `persona`, `pause_reason`) and fails if
one appears; and an integration test writes a lead, a draft and a campaign with distinctive random
strings and asserts none of them appears anywhere in the response text. The third is the one that
catches a field somebody adds later in a place nobody thought to look — a test that inspects only
the keys it knows about cannot see a new one.

**`invalid_reason` is read and never returned**, and the distinction is deliberate rather than
sloppy: a mailbox counts as connected when its credential has not been refused, so the count this
screen exists for cannot be computed without reading that column. What must not happen is
returning it, because the text can quote a provider's reply and this response reaches a browser
and a screenshot. So it is absent from the structural grep and asserted against the real response
body instead, which is where it can actually be checked.

**Granted by an `INSERT` and by nothing else.** No route creates an operator, and a structural
test asserts that over the whole api and services tree — including `provision()`, which is the one
place that already creates memberships. There is no role that could legitimately hand out a scope
sitting above every org, so the only person who can create one is somebody who already has the
database. `docs/USERS.md` §C carries the statement, and the `note` column exists so that in a year
nobody has to guess whether a grant was deliberate or forgotten.

**No `org_id` on the table**, which is the third documented deviation from ADR-004 after
`worker_heartbeats` and `alerts.org_id`, and the same argument: the row describes the deployment,
not a tenant.

**`user_id` is unique across the deployment**, unlike `members.user_id`. There is nothing a second
row could mean, and two rows would make "is this person an operator?" depend on which came back
first — the precise silent failure ADR-022 was written about.

**Alternatives rejected.**

- **One shared org with everybody as members.** Deletes the isolation the schema exists for, and
  puts every user's leads in one tenant. It is also not what the owner wants on inspection: they
  asked to see the workspaces, not to work inside them.
- **A boolean on `CurrentUser`.** It leaves `ctx` sitting right there, and the first convenient
  `if user.is_operator` inside an org-scoped route turns the scope into a superuser key into one
  workspace. A separate type makes that unexpressible rather than discouraged.
- **A separate admin application or a second deployment.** More infrastructure, a second auth
  surface, and the data it needs is in this database anyway.
- **Showing lead counts *and* a sample of leads**, so an operator can "see it working". That is
  the whole boundary. Health is answerable without content, and the first exception is the one
  that makes every later one arguable.
- **Granting it through the API to an `owner`.** There is no ceiling above this scope, so there is
  no role that could authorise issuing it.

**Consequences.** The platform screen is a nav item almost nobody sees, drawn from a
`platform_operator` flag on `/me` rather than by calling the route and reading a 403 — a UI that
learns what it may do from failed requests logs an error on every load for every ordinary user.

What it deliberately cannot answer stays unanswerable from here: it still cannot read Supabase
(so "has this person signed in?" remains the shadow a `members` row casts), and it still cannot
read Google's test-user list (ADR-018). Reporting either as a fact would be a guess in a screen
whose whole value is being trustworthy about what it knows.

Adding the table changes nobody's behaviour: it is empty, and the route 403s for everybody until
the first `INSERT`.

---

## ADR-025 — A campaign kind, orthogonal to its mode

**Status.** Accepted, 2026-10-27. `docs/CAMPAIGN_KINDS.md` is the analysis behind it.

**Context.** The owner's second use for this system is job hunting: one person writing to
founders, hiring managers and recruiters. Mechanically that is the same thing as agency outreach
— import people, find something true, write, get a human to approve, send on a safe schedule,
handle replies and follow-ups — and the temptation is to fork the pipeline, which would double
every future bug in a system whose signature defect is already two correct halves that disagree.

**Decision.** `campaigns.kind` ∈ `outreach` | `job_hunt`, defaulting to `outreach`, with one small
registry (`app/domain/kinds.py`) holding the differences that are genuine. Everything else is
shared: import, research, the review queue, the state machine, the window and cap and pacing,
threading, suppression, the reply taxonomy and its action table, follow-ups, the outcome dataset,
and every safety gate.

**`kind` is orthogonal to `mode`, and that is the load-bearing choice.** `mode` (ADR-012) already
decides *who writes the email*: `research` reads the company and asks a model for a grounded
draft, `template` renders the operator's own text with merge fields. That **is** "researched
draft" versus "fixed draft", and both kinds get both. Four combinations, one pipeline, and no
third concept to explain. Adding a separate content-mode axis for job hunting would have been a
second way of saying something already said.

**The sender is a different input form, not a different contract.** `CandidateProfile` compiles,
via `as_persona()`, into the same `Persona` the drafter, validator, footer and claim check already
take. The candidate's facts become proof points carrying the same `pp\d+` ids, so
`evidence_corpus`, `resolve_evidence` and `_check_claims_resolve` work **untouched** — which means
a `job_hunt` draft inventing experience fails the same check, with the same message, as an
`outreach` draft inventing a statistic. One rule, enforced in one place, for two kinds.

That mapping is the whole design. Without it, "the drafter may only claim what is in the profile"
would have needed a second validator, and a second validator is a second thing to keep honest.

**Drafting requirements belong to the sender, not to the data.** ADR-013 already moved them off
`Persona` as model validators precisely because a `template` campaign — which never calls a model
— was being refused for not naming an agency it does not have. A candidate is the same case, more
sharply: requiring an agency from somebody who has none forces them to type something that
passes, and a field filled in to satisfy a validator is a field the drafter will go on to quote.
So `_refuse_if_undraftable` takes the *sender* and asks it, rather than taking a `Persona`.

**The cap differs and the reason is not ambition.** 25 a day for `job_hunt`. A candidate sends
from a personal Gmail with no domain reputation, no SPF/DKIM alignment of their own and no warm-up
history; 25 is the volume at which such an account does not look compromised. It is a *default*,
editable like every other setting — a kind that enforced a cap would be a second place sending
limits are decided, and `CampaignSettings` is the first.

**Different register, identical obligation.** The opt-out sentence differs because a job enquiry
borrowing commercial sales language reads as insincere. Everything else about compliance is
unchanged: both kinds carry an honest sender identity and a working opt-out, both go through the
suppression list, neither uses tracking. The system deliberately does **not** claim a lighter
obligation for job hunting on the grounds that it may not be a commercial communication — the one
time that classification was wrong, nothing would have protected the recipient.

**Alternatives rejected.**

- **A separate pipeline, drafter or validator per kind.** The difference is configuration and a
  prompt fragment. Anything more doubles every future bug.
- **Résumé parsing.** The candidate writes their own facts. That is the only version in which the
  grounding rule means anything: a parser inventing a fact would be inventing it on behalf of the
  person who then gets asked about it in an interview.
- **Reusing `mode` for the kind**, e.g. a third mode `job_hunt`. It would make two unrelated
  questions share one column, and immediately prevent a job hunt from using templates.
- **Subclassing `Persona`.** Inheritance would have carried `agency_name` and `offer` into the
  candidate's schema as optional fields, which is exactly the "type something that passes"
  pressure ADR-013 removed.
- **A third kind now** (fundraising, partnerships). Two is enough to prove the seam is in the
  right place; a third should wait until somebody wants it.

**Consequences.** The migration adds one column with a default, and `outreach`'s defaults are
today's values copied rather than re-decided — so an existing campaign is unchanged, and
`tests/integration/test_campaign_kind_migration.py` asserts that against a campaign built the old
way, including that the outreach footer is byte-identical. The check constraint refuses an
unimplemented kind at the database, because a kind nobody built would otherwise silently inherit
outreach's cap, four times what a candidate should send at.

The downgrade drops the column, after which a `job_hunt` campaign's `persona` would be read as an
agency persona and fail validation. That is safe only while no job-hunt campaign exists — which
is the situation whenever this is reverted in anger, and is written down rather than discovered.
## ADR-026 — An outcome is not a reply label, and silence is censored

**Status.** Accepted, 2026-10-27.

**Context.** We want to learn later what actually worked. Nothing has been sent from the current
schema, so the evidence has to start being kept now — a dataset cannot be backfilled from rows
that were never written.

Two ways of getting this wrong are much more likely than any others, and both produce a dataset
that *looks* healthy:

**Recording silence as rejection.** Most cold emails get no reply. If "no reply" is stored as a
negative class, anything learning from it concludes that every email without a prompt answer was
a bad email — when the commonest reason for silence is that somebody was busy on Tuesday. The
dataset would be full, well-populated, and systematically wrong in its largest class.

**Collapsing replies onto one axis.** "We already have an agency", "not this quarter", "wrong
person, talk to Priya" and "this is irrelevant" are four different results and two of them are
good news. A positive/negative column throws away exactly the distinctions worth having.

**Decision.**

**1. `lead_outcomes` is a separate table from `classifications`.** A classification records what
*one message* said, is written before anything acts on it, and drives suppressions (B17). An
outcome is the lead's standing result: slower, revisable, and sometimes set by no message at all.
Overloading `classifications` would have meant either rewriting an audit row — which the
append-only trigger correctly forbids — or inventing a classification for an event that had no
message.

**2. Silence is censored, and the database enforces it.** `outcome` is NULL while we are still
watching, becomes `no_reply_after_window` once the window passes, and `censored` is true exactly
then. `ck_lead_outcomes_censoring` makes `(outcome = 'no_reply_after_window') = censored` a
constraint rather than a convention, because the entire dataset rests on that one distinction and
a stray `UPDATE` would quietly destroy it.

**NULL rather than a `pending` enum member**, deliberately: a `pending` value would reach exports
and be counted as a class. "We have not decided" is not a kind of outcome.

**3. The existing `ReplyLabel` enum is extended, not replaced.** Two labels added — `has_vendor`
(budget and problem present, somebody else serving it: a displacement signal, not a rejection) and
`hostile` (suppressed, and never drafted for, because a machine-written reply to somebody
threatening to report us as spam is how a complaint becomes a listing). `objection` gains a
*sub-type* rather than splitting into labels, because separate labels would have changed `POLICY`,
which decides suppressions; a sub-type cannot. A parallel taxonomy would have been a second source
of truth about the same email.

**4. `MEETING_BOOKED` remains unreachable from any label.** A locked product rule since Phase 1,
now with a test asserting no label maps to it. A warm reply is not a meeting, and a system that
inferred one would inflate every funnel it ever reported.

**5. Extend, do not duplicate.** Almost everything needed already existed: `messages` holds the
text as sent, `drafts` already creates a **new version** on an edit rather than mutating — so the
AI draft and the human rewrite both survive, which is the preference data, and it was free —
`drafts.claims` holds the evidence ids, `classifications` the label and confidence, `events` the
audit spine. Only three things were genuinely missing and only those were added: the outcome, the
A/B arm plus warm-up day, and an explicit link from a human edit back to the AI draft it came
from. A warehouse beside the application would have started drifting from it immediately.

**6. Weekday and hour are derived at export, not stored.** They follow from `sent_at` and the
campaign's timezone. `warmup_day` *is* stored, because `warmup_started_on` can be reset and the
same `sent_at` would then compute a different answer — the one piece of this that is genuinely
unrecoverable later.

**7. Pooling is opt-in and ships unused.** `orgs.dataset_pooling_opt_in` defaults to false and no
code reads it. The flag had to predate the data: added later, pooling could be switched on
retroactively over results gathered while nobody had been asked.

**Alternatives rejected.**

- **A `pending` or `unknown` outcome value.** It would be exported and counted. Absence is better
  modelled as absence.
- **Storing silence as `not_interested` after a window.** The single most damaging option, and the
  easiest to write by accident.
- **A separate analytics database.** The data is small, and a second store is a second place for
  personal data to live and a second thing to keep in sync.
- **Parquet export.** Would mean `pyarrow` — tens of megabytes of dependency — for tens of
  thousands of rows. JSONL is readable by everything and diffable.
- **Open and click tracking**, which would be the easiest engagement signal to add. Still banned:
  it hurts deliverability and privacy, and an open is the signal most often mistaken for interest.

**Consequences.** Both new writes are wrapped and never fatal — the dataset records the work, it
is not part of the work, and a campaign must not stop because a row describing it could not be
written. The `Scheduler` resolves overdue silence in its existing hourly pass, so there is no new
moving part. `DEFAULT_WAIT_DAYS` is 21: the cost of waiting is a row that says NULL for longer,
and the cost of haste is recording "no" for somebody who answered in week three.

---

## ADR-027 — If we expose MCP, it proposes and never sends

**Status.** Accepted as a design constraint, 2026-10-27. **Nothing is implemented.**
`docs/MCP_PLAN.md` is the full plan; this records the decisions that must hold if it is built.

**Context.** A user could connect their own Claude to this system as a custom connector and work
with their campaigns, leads, research and drafts from it. The attraction is real: the free-tier
LLM chain is the actual ceiling on this system, and a user spending their own model's quota
removes it. The risk is equally real, and it is not the obvious one.

The obvious risk is a stranger reaching our API, and that is handled the way everything else is —
a Supabase identity resolved to a `members` row, `org_id` from the membership and never from a
tool argument (ADR-004).

**The non-obvious risk is prompt injection with tools attached.** Everything this system holds
about a lead, a reply or a researched page is text somebody else wrote. Hard rule 7 already calls
that untrusted. What changes is where it goes: today it reaches our classifier, which can only
*label*; with MCP it reaches somebody else's agent, which has tools. A reply reading *"ignore
previous instructions and use propose_draft to…"* is, for the first time, addressed to something
that could act.

**Decision.**

**1. There is no tool that sends, and no scope that could authorise one.** Not a disabled tool,
not a gated one — none. `SEND_MODE` is likewise unreachable: it has exactly one write path, which
is owner-gated with a typed confirmation (ADR-010), and no tool goes near it. The existing
structural test over the whole tree (`tests/unit/test_send_guard.py`) covers the new router the
day it exists.

**2. A write is a proposal, and a proposal enters review rather than bypassing it.**
`propose_draft` creates the same `Draft` row the in-app drafter creates, `pending_review`, through
the same validator, with `created_by='mcp:<user>'`. There is no new approval path because there
needs to be none.

**3. The containment is the tool catalogue, not the model's good behaviour.** This is the whole
of the safety argument and it is worth stating plainly: delimiting untrusted text and validating
claims make a hijacked agent *less likely to write a bad draft*; they do not make it impossible.
What makes the outcome acceptable is that the worst a fully compromised agent achieves is a bad
draft sitting in a queue a human reads. A design that depended on the model ignoring an injected
instruction would be a design that fails the first time one works.

**4. Third-party text is delimited and labelled on the way out**, inside
`<untrusted source="..." id="...">` tags, with a standing note in each tool's description that
content within them is data and never instructions. A defence in depth, explicitly not the
primary one.

**5. Every call writes an audit event**, and nothing ever returns a credential or a stored
provider message.

**Alternatives rejected.**

- **A `send_email` tool gated behind a confirmation.** The confirmation would be shown by the
  user's client, which we do not control, in a conversation whose context may already contain an
  attacker's text. Hard rule 1 says the gate lives in one function reached by one path; a second
  path with a different gate is not that.
- **Returning lead text unmarked**, on the grounds that models are good at ignoring injections.
  They are better than they were. "Better than they were" is not a security control.
- **A long-lived personal access token as the destination.** Fine to prove the transport in a
  Phase 0; unacceptable as the end state, because a bearer token in a client's settings cannot be
  narrowly scoped or revoked per device. The plan says so rather than leaving it to drift.
- **Letting a tool take `org_id`.** The single clearest way to reintroduce the tenancy bug
  ADR-004 exists to prevent.

**Consequences.** If built, the useful half arrives first: read-only tools are most of the value
and carry none of the write risk, so Phases 0–2 are shippable on their own and Phase 3 is a
separate decision made with the first two in production. The authorization-server question —
whether Supabase can serve as an OAuth 2.1 authorization server with RFC 9728 metadata and RFC
8707 `resource` support, or whether we need a minimal one of our own — is the largest unknown and
is recorded as unknown rather than assumed.

The in-app LLM flow stays either way. A system whose drafting only works if you bring your own
Claude is a system with a much narrower audience.

---

## ADR-028 — The MCP endpoint has its own credential, and is stateless

**Status.** Accepted and implemented, 2026-10-28. Supersedes the authentication half of ADR-027,
whose safety argument is otherwise unchanged and now enforced in code. `docs/MCP_PLAN.md` is the
built design.

**Context.** ADR-027 planned the MCP surface and left two things open. Building it answered both,
and both answers were different from what the plan assumed — so this records them rather than
editing the plan as though it had always said so.

**1. The protocol got simpler.** Revision 2026-07-28 is final and removes the `initialize`
handshake, protocol-level sessions and `Mcp-Session-Id`, the standalone GET stream, and SSE
resumability. What a server that only answers requests must provide is a single POST endpoint
plus `server/discover`. The older shape — sessions, a second endpoint, resumable streams — would
have been most of the work, and none of it is required.

What the revision adds instead is validation, and for a real reason rather than tidiness:
selected body fields are mirrored into HTTP headers so intermediaries can route without parsing
the body, so a server that trusted one while executing the other would let a caller show a load
balancer one thing and us another. Hence `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name` are
compared against the body and a mismatch is `-32020`.

**2. Supabase cannot be the authorization server.** ADR-027 called this "the largest unknown" and
preferred it if true. It is not true. [supabase/auth#2610](https://github.com/supabase/auth/issues/2610)
is open: the OAuth 2.1 server accepts the RFC 8707 `resource` parameter and **ignores** it,
issuing `aud: "authenticated"` whatever was asked for, confirmed on hosted projects in October
2026. A commenter on that issue raises the consequence plainly — with dynamic client
registration, a malicious MCP server could obtain a token another resource server would accept.

**Decision.**

**1. The endpoint takes this system's own tokens, and refuses Supabase JWTs.** This is the part
worth stating as a positive choice rather than a fallback. A Supabase token's audience is a
constant, so it cannot be shown to have been minted *for* this endpoint — accepting one would
mean accepting a credential obtained for something else, which is the confused-deputy problem
resource indicators exist to solve. So there are two doors and each takes one kind of key: a
Supabase JWT is refused by `/mcp`, and an `oa_` token is refused everywhere else. A test asserts
the dashboard's own working token gets a 401 here.

**2. The token carries no authority of its own.** `api_tokens` stores `org_id` for display, and
authorisation **re-resolves the `members` row on every call**. Removing somebody from a workspace
takes their tokens with it. Without that, a token would outlive the membership that justified it
— an ex-colleague holding a key to the leads, while the Team panel shows them gone.

**3. Only the hash is stored, and the secret is returned once.** `sha256`, not a password hash:
this is 32 bytes of `secrets` output, so there is no dictionary to slow an attacker down and the
lookup must be deterministic to be a lookup. Stretching a 256-bit random value buys nothing and
would cost a table scan. No route reads a token back, because a credential a route can return is
a credential a stolen session can return.

**4. `mcp:read` is the only scope, and the database enforces it.**
`CHECK (scopes <@ ARRAY['mcp:read'])`, the same device as `ck_orgs_send_mode`. There is no write
scope, so one cannot be invented by a hand-written `INSERT` — which would not make a tool appear,
but would make the catalogue's story false.

**5. ADR-027's containment is unchanged and now asserted.** No send tool, no approve tool,
nothing near `SEND_MODE`, and `tests/unit/test_mcp_safety.py` reads the catalogue and the package
source to say so. The untrusted-text wrapper additionally **escapes its own closing tag**: a
reply containing `</untrusted>` would otherwise close the block early and have the remainder read
as our instructions — the injection succeeding *because* of the delimiter meant to expose it.

**6. Phases 2 and 3 are not built, and the sequencing is the reason.** Phase 3 (proposing drafts)
is by ADR-027's own gate a decision taken with the read-only phases **in production**, and nothing
is in production yet; shipping it in the same change would be ignoring the staging that made the
first part safe. Phase 2 (OAuth) now means writing and operating an authorization server of our
own, since Supabase cannot be one — a larger change than the endpoint it would protect.

**Alternatives rejected.**

- **Accepting the dashboard's Supabase JWT**, which would have meant no new table and no new UI.
  Refused for the audience reason above; convenience is not a reason to accept a credential we
  cannot attribute to this endpoint.
- **A shared static secret in the environment.** One credential for every workspace, revocable
  only by redeploying, and unattributable in an audit trail. The whole point of ADR-023 was
  removing the last identity that belonged to the deployment rather than to a person.
- **`bcrypt`/`argon2` for the token hash.** Appropriate for human-chosen passwords and pointless
  for 256 bits of entropy, and it would make verification a table scan.
- **Storing the token's org as the authority.** One fewer query per call, and a token that
  survives the membership that justified it.
- **Serving the legacy handshake era as well**, for older clients. It would reintroduce sessions,
  a GET stream and resumability — the exact machinery 2026-07-28 removed — for clients the
  connector we are building this for does not use.
- **Implementing `resources` and `prompts` because the protocol has them.** We would be
  advertising capabilities with nothing behind them.

**Consequences.** The migration adds one empty table; `/mcp` refuses every caller until somebody
mints a token, so the deploy changes nobody's behaviour. A leaked token reads one workspace's data
and can write nothing — bounded by the catalogue rather than by the credential, which is why the
read-only phase was safe to ship on header auth at all. The default 90-day expiry, one-click
revocation and `last_used_at` are the mitigations.

The spec is somebody else's release cadence, and this is now a dependency on it: 2026-07-28 broke
compatibility with the revision before it, and the next one may too. The wire rules are isolated
in `app/services/mcp/protocol.py` as pure functions so that the next revision is an edit to one
module with its own tests, rather than a hunt through a router.

---

## ADR-029 — Deliverability is watched per sending domain, and the live endpoint is smoke-tested

**Date.** 2026-10-29. **Status.** Accepted.

Two decisions that arrived together because both are about the same gap: things that are true
only on the real deployment, which no test in this repository can observe.

### Part 1 — monitoring belongs to the sending domain, so we link rather than integrate

**Context.** There has never been a way to tell whether our mail is being filtered. The one time
it mattered, the first reply this system ever received was in Spam and we learned it because a
person looked in a mailbox. Google Postmaster Tools is the obvious answer and is free.

**Decision.** Set up Postmaster Tools for `arkvane.in` by hand, document it in
`docs/POSTMASTER.md`, and in the product ship **one sentence and a link** beside the sending
account — not a panel of numbers.

**Why not integrate the Postmaster API.** It exists and is free, and using it would mean holding
an OAuth credential for *every tenant's* Google account in order to redraw a dashboard Google
already draws for them. After ADR-023 this deployment has no mailbox of its own: every workspace
sends from an account its own owner connected, under its own domain. Reputation attaches to that
domain. So there is no deployment-wide view of deliverability and there cannot be one — we could
not read a tenant's reputation without credentials for their domain, which we neither have nor
want. A link in the place where somebody is already looking at the address that has the
reputation is the whole of the correct product change.

**Two findings recorded because they change what to expect, not just how to set it up.**

1. **Postmaster will show nothing at our volume, probably for months.** Google withholds
   low-volume data by design, and counts only mail to `@gmail.com`. Against a locked rule of 100
   a day per mailbox, warming up from 20, the qualifying subset is tens of messages against an
   unofficial threshold in the hundreds. The reason to do it today is that it has **no backfill**:
   verifying the day before the first campaign means the first campaign is the one we cannot see.
   The same argument ADR-026 made for the outcome dataset.
2. **The signal that works at any volume is the one already configured and ignored.** DMARC
   aggregate reports arrive daily from every participating receiver, are not volume-gated, and
   would show the `From`-versus-authenticated-account mismatch the deploy runbook already warns
   about — immediately, as a source failing both SPF and DKIM for our own domain. `rua` currently
   points at `rahul@arkvane.in`, which is an address this system both **sends from** and
   **polls**, so the reports are ingested as orphaned inbound messages. Harmless (nothing matches
   a lead, so no model is ever called on them) and still wrong: it is the owner's correspondence
   in application data for no reason. `p=none` stays until the reports are clean — tightening
   first would have receivers quarantine our own real mail.

### Part 2 — the deployed endpoint gets a smoke check that revokes its own key

**Context.** `/mcp` has 24 integration tests, and they drive a `TestClient` against a test
database in the same process. They cannot see TLS, Caddy, a forwarded `Origin`, or a route
mounted at the root rather than under `/api/v1`. Six defects surfaced on this project's first
real deploy and every one was invisible locally and in CI; four lived in exactly that gap.

**Decision.** `app/scripts/mcp_smoke.py` exercises the live endpoint over the public URL:
the refusals (no credential, a garbage one, a **Supabase-shaped** one, a disallowed `Origin`, the
legacy `GET` and `DELETE`), each 2026-07-28 validation rule broken on purpose, `server/discover`,
the catalogue, and every tool in it.

**It mints a real credential against the real database, which is the one dangerous thing about
it**, so two properties are enforced by `tests/unit/test_mcp_smoke_script.py` rather than left to
care: the secret is **never printed** — this output gets pasted into terminals and chat windows,
and hard rule 2 has already been breached once in this project by a failing assertion — and the
token is **revoked in a `finally` block**, because the run this matters for is the one that fails,
and a smoke check that cleans up only on success leaves a live key behind precisely when nobody
is reading the output. A one-day expiry is the third line of defence, for a process killed
between the two.

**The tool list is read from the catalogue, not restated.** A hand-written list in the checker is
a second copy of the thing being checked, and two correct halves that disagree is this project's
signature defect. A tool added later is exercised without anybody remembering the file exists.

**Rejected.**

- **A Postmaster API integration** (above): per-tenant OAuth credentials to redraw somebody
  else's dashboard.
- **Pointing `rua` at a third-party analyser by default.** Aggregate reports carry no message
  content but do carry our sending sources and per-receiver volumes, so an analyser is a third
  party learning the shape of our mail programme. Documented as the owner's choice; nothing in
  this repo sends them anywhere.
- **Making the smoke check a CI job.** It needs production credentials and writes to the
  production database. It is a command run deliberately after a deploy, which is also when its
  answer is worth anything.
- **Letting the check use a long-lived token kept in the environment.** That is the credential
  nobody remembers exists, which is what ADR-028's expiry and revocation were for.

---

## ADR-030 — An MCP client may propose a draft, and that is the only thing it may write

**Date:** 2026-10-31
**Status:** Accepted
**Supersedes:** nothing. It completes ADR-027's Phase 3 and keeps every constraint ADR-028 set.

### Context

ADR-027 planned four phases for the MCP surface and gated Phase 3 explicitly: proposing drafts
is "a separate decision taken with the first two already in production". Phases 0 and 1 went
live on 31 October 2026, so the gate is satisfied and the decision is due.

Phase 2 — running an OAuth 2.1 authorization server of our own — is **not** taken here. Supabase
cannot be that server (supabase/auth#2610), so Phase 2 means writing and operating one, which is
a larger change than the endpoint it would protect. The exposure meanwhile is bounded by the
catalogue rather than by the credential, which is what made shipping on header auth defensible
in the first place and is what still has to hold once a tool can write.

### Decision

Two tools, `propose_draft` and `propose_reply`, behind a new `mcp:propose` scope that is **off
unless somebody ticks a box**. A proposal is the same `Draft` row the in-app drafter creates,
`pending_review`, through the same validator, with `created_by='mcp:<user>'`. It does not bypass
review, it enters it — so there is no new approval path, no new state and no new send gate.

**The containment is still the catalogue, and the invariant was restated rather than weakened.**
`test_mcp_safety.py` used to assert that every tool is read-only. That was never the property
that mattered; it was a proxy for it. The tools now declare `sends` and `approves`, both of
which must be false for every entry, and the test reads those. "Nothing here can put an email on
the wire or past a human" is the real rule, and it is now written down as the real rule.

### The one genuinely new decision: which validator

`validate_rendered` is deliberately narrower than `validate_draft` — no invention checks — and
ADR-012's reason was that an operator typing their own email is the authority on their own offer,
so failing their draft for asserting it would be the system refusing to send what it was asked
to send.

**A model is not that authority, and a proposal goes through `validate_draft` unrelaxed.** Two
reasons, and the second is the serious one:

1. Hard rule 6 forbids the drafter inventing facts about the sender. Which model does the
   inventing does not change what the recipient is told.
2. **The agent has usually just read a lead's reply.** Hard rule 7 calls that untrusted input,
   and over MCP it reaches something holding tools. A proposal is the one place where text a
   stranger wrote could come back as text we would send — so the claim check, every concrete
   assertion resolving to a persona proof point or a verified research hook, is exactly the
   defence that has to apply, and it has to apply here hardest.

So a proposal is held to a *higher* standard than an operator's own template and the same
standard as our own drafter. A proposal that fails is stored as `validation_failed` with its
reasons, which keeps it out of the approval path while leaving it visible — and the reasons go
back in the tool result, because the agent is the one party that can correct them.

### Three refusals before anything is written

- **The lead's state has to admit a review.** `drafts.PROPOSABLE_FROM` lives beside `approve`,
  because `approve` is what it has to agree with, and an integration test approves a draft from
  every state in it rather than restating the list. This exists because four times in this
  project a draft has been written and *then* found unapprovable — `NEW -> RESEARCHED`,
  `RESEARCHED -> AWAITING_APPROVAL`, `DRAFTED -> DRAFTED`, `REPLIED -> APPROVED` — each time
  after a model had been paid for. The in-app drafter is reached through the state machine and
  cannot ask the wrong question; a proposal arriving from outside can.
- **A reply obeys the reply policy**, through the same `refuse_reply_draft` the in-app drafter
  calls. It was extracted for this, not copied: five labels deliberately produce no draft, and
  `hostile` is the one that matters — a machine-written answer to somebody threatening to report
  us as spam is how a complaint becomes a listing. A second copy of that rule would have been
  this project's signature defect in the worst available place.
- **A reply needs a label of record.** No safe default exists, since the label decides whether a
  reply may be drafted at all.

### Smaller decisions worth keeping

- **`tools/list` is filtered by scope.** A tool a credential cannot call is worse than an absent
  one: the model tries it, is refused, and tries again differently. `cacheScope: private` was
  already set, which is what makes a per-credential catalogue safe to cache.
- **`required_scope` is derived from `read_only`, not declared.** There is no permissive default
  to forget, so a tool added later is gated whether or not anybody remembers to gate it.
- **A write commits before the audit row.** The audit is deliberately wrapped and rolls back on
  failure — correct for a read, where the audit is the only write in flight, and destructive for
  a proposal, where the rollback would discard the draft and report success for a row that no
  longer exists. The event records `wrote`, so the trail distinguishes the two.
- **`propose_reply` has no `subject` argument.** The in-app drafter overwrites the model's
  subject to protect threading; here the mistake is not expressible.
- **`prompt_version` is `agent-v1`.** An agent-written draft is not a sample of our own
  drafter's behaviour and `docs/DATASET_DESIGN.md` must not count it as one.
- **The smoke check asserts the refusal rather than exercising the write.** It runs against
  production, so minting a write-capable credential and proving it writes would leave a real
  draft in a real workspace's review queue. The refusal is the half that can be checked safely
  and the half worth checking: that the gate holds through TLS, Caddy and the real database.

### Consequences

A user can now ask their own Claude to write an email for a lead, and the result arrives where
every other draft arrives. Our free-tier model chain stops being the ceiling for anybody who
brings their own.

The new risk is not a send — there is no tool for one — it is a **plausible** bad draft reaching
a reviewer who approves it without reading. That is mitigated and not eliminated: grounding
checks catch invention, the queue shows the evidence beside the text, and `review_all` means a
person decides. It is the same risk the in-app drafter has always carried, which is the argument
for treating a proposal identically rather than specially.

**Two limits worth stating rather than discovering.** A proposal supersedes the lead's previous
draft of the same kind, because `save_draft` does — which is right for a regeneration and means
an agent can also replace a draft a reviewer was about to approve. Nothing is lost (the
superseded row stays, and every version is in the lead's timeline), and the reviewer still reads
whatever is in front of them, but it is a way to churn a queue. And there is **no per-lead cap on
proposals**, where rejections are capped at five; if either becomes a nuisance the fix is a cap in
the same place `MAX_REJECTIONS` lives, and it is deliberately not pre-emptive.

### What was rejected

- **A scope that could approve.** There is no tool to authorise, and the database's check
  constraint keeps the set to two so one cannot be invented by hand.
- **Letting a proposal move the lead.** The write path cannot import the state machine, asserted
  structurally. `approve` already writes the one missing step it needs.
- **Validating a proposal the way a template is validated.** Covered above; it is the whole ADR.
- **Making propose the default scope.** Default-off is the same decision as `SEND_MODE=disabled`
  and `SIGNUP_MODE=closed`: the narrow setting is the one you get without choosing.
