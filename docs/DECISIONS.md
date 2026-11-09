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

### Replacing a draft: optimistic concurrency, not a lock and not a ban

The first version of this shipped `save_draft`'s superseding behaviour unchanged, and I filed the
consequence as a nuisance not worth pre-empting. **That was the wrong call and the owner caught
it.** It is not a churn problem, it is a correctness one: the draft a reviewer has open can be
replaced under them, so the draft they approve is not the draft they read. A queue whose contents
change while somebody is reading them is the same class of defect as the headline number that
fell as the campaign succeeded — the screen tells the truth about a different moment than the one
the person is acting in.

Superseding itself is right for a *regeneration*, because a reviewer asked for that one. What is
wrong is a proposal arriving from outside and doing it silently. So:

- **A proposal must name the draft it replaces**, `replaces_draft_id`, and the only way to know
  that id is to have read the lead. You cannot replace a draft you have not looked at.
- **A stale id is refused**, naming the current draft. That makes this concurrency control rather
  than a speed bump: a re-run against a queue that has moved on cannot clobber, and two agents
  cannot race.
- **An id for a lead with no current draft is refused too**, because it means the caller is
  working from a stale read, which is exactly the state being guarded against.
- **A draft a person wrote is never replaced**, whatever id is passed. Somebody's own words are
  not an agent's to overwrite.
- **An approved draft is never replaced.** A person has decided and it is queued to send;
  undoing that is not something to do from a tool.

Rejected: *always refuse while a draft is waiting*, which is the simplest invariant and makes the
tool useless — most leads sit in `AWAITING_APPROVAL` with a pending draft, so "write this one
better" could never be answered. Rejected: *keep replacing and flag it in the queue*, which tells
the reviewer afterwards instead of preventing it.

`REPLACEABLE_STATUSES` and `current_draft()` live in `app/db/drafts.py` beside `approve`, for the
same reason `PROPOSABLE_FROM` does. The status comparisons are predicates there
(`was_approved`, `was_written_by_a_person`) rather than string literals at the call site: the
status vocabulary belongs to that module, and a literal copied into a caller that cannot see it
change is this project's recurring defect. The structural guard that forbids the MCP package
naming an approved status is what forced that, which is the guard doing its job.

### A cap of five proposals per lead per day

`MAX_REJECTIONS` is five for the same failure — each one is a model call, and a loop spends a
day's free-tier quota on one lead while every other lead waits behind it. The cap counts drafts
whose `created_by` carries the `mcp:` prefix, so **our own pipeline's drafts do not count**: the
limit is on what the agent did, not on how busy the lead has been. Per day rather than per
lifetime, so a legitimate user who comes back to a lead next week is not permanently stuck, and
the refusal says what to do instead, because a limit that reads as a malfunction is one somebody
retries against.

`AGENT_CREATED_BY_PREFIX` is defined once and used by both the writer and the counter. Two copies
of that string is how the cap would quietly stop counting.

### What was rejected

- **A scope that could approve.** There is no tool to authorise, and the database's check
  constraint keeps the set to two so one cannot be invented by hand.
- **Letting a proposal move the lead.** The write path cannot import the state machine, asserted
  structurally. `approve` already writes the one missing step it needs.
- **Validating a proposal the way a template is validated.** Covered above; it is the whole ADR.
- **Making propose the default scope.** Default-off is the same decision as `SEND_MODE=disabled`
  and `SIGNUP_MODE=closed`: the narrow setting is the one you get without choosing.

---

## ADR-033 — One-click unsubscribe is an https endpoint, and the header is not sent without one

**Date:** 2026-10-31
**Status:** Accepted (part 1 of 2 — the header claim; the endpoint lands with F4's slice S11.4)
**Relates to:** F4 in `docs/OUTREACH_PLATFORM_V3.md`, `docs/IMPLEMENTATION_PLAN_V3.md` §1.2.6

### Context

Gmail and Yahoo's bulk-sender rules require a one-click unsubscribe that follows RFC 8058. This
repo has been sending, on every message:

```
List-Unsubscribe: <mailto:asha@example.com?subject=unsubscribe>
List-Unsubscribe-Post: List-Unsubscribe=One-Click
```

**RFC 8058 is about an `https` URI a receiver can POST to.** The second header beside a bare
`mailto:` announces a capability that does not exist. The code comment beside it read "tells a
client the mailto is safe to trigger without a confirmation step", which is a misreading of the
RFC — one-click is not "no confirmation", it is "POST here and we will act on it".

**This is worse than a missing feature, and that is why it is fixed before the endpoint is
built.** Most wrong headers are inert. This one is *testable by the receiver*: a mail provider can
POST, find nothing there, and conclude that the sender misrepresents its own compliance — against
exactly the criteria that decide whether our mail is delivered. The repo had been claiming
compliance with the one rule it had not implemented.

### Decision

1. **`List-Unsubscribe-Post` is sent only when an https unsubscribe URL exists.** Until S11.4
   builds the endpoint, that means it is not sent at all. The `mailto:` stays, so there is still
   an unsubscribe mechanism in every message — it is simply not described as something it is not.
2. **`List-Unsubscribe` carries the https URL first and the mailto second**, once there is a URL.
   A receiver takes the first target it understands, and the POST is the one the bulk rules ask
   for. The mailto is kept because it needs no endpoint to be reachable and a reply to it lands
   in the inbox the IMAP poller already watches.
3. **A non-https URL is refused rather than normalised.** `http` would have a receiver POST
   somebody's opt-out in clear, and silently rewriting a caller's URL is how the wrong endpoint
   ends up in a header nobody reads.
4. **The URL is a parameter of `build_message`**, defaulting to `None`. So this slice changes
   behaviour in exactly one way — a header stops being sent — and S11.4 is a wiring change rather
   than a second edit to the message builder.

### Consequences

Our messages no longer claim RFC 8058 compliance. That is a *reduction* in what we advertise and
an increase in how true it is; until S11.4 lands we are a sender with a mailto unsubscribe, which
is where we actually were.

`tests/unit/test_mail_message.py::test_one_click_is_advertised` **asserted the defect** and is now
`test_one_click_is_not_advertised_without_a_url_to_post_to`. That is the fourth time in this
project a test has encoded the bug it should have caught (after `parse_chain`'s empty chain, the
mailbox-credential fallback, and the `tools/list` ordering assertion that compared a list with
itself), and it keeps happening for one reason: a test written from the implementation describes
the implementation.

### What was rejected

- **Building the endpoint in the same slice.** It needs a public unauthenticated route, a signing
  scheme and a suppression write — a `needs-human` change on the sending path. Removing a false
  claim should not wait behind it.
- **Dropping the mailto once there is a URL.** It costs nothing and works where an https POST
  does not reach.
- **Leaving it as it was until the endpoint existed.** The header is read by the systems that
  decide our deliverability; a false claim there is not a cosmetic debt.

### Part 2, 2026-10-31 — the endpoint (slice S11.4)

`POST /u/{token}` and `GET /u/{token}`, mounted at the root outside `/api/v1`. **The only
unauthenticated write in the system**, by necessity: a recipient must be able to unsubscribe
without an account, and a receiving mail server POSTs the URL automatically.

**POST acts, GET does not**, and that split is a safety property rather than pedantry. Mail
clients, security scanners and link-preview crawlers fetch URLs they find in a message. If GET
unsubscribed, a scanner opening the mail would opt the recipient out of a campaign they had not
read — and we would record it as their choice. GET returns a page with a button; the button
POSTs.

**The token carries its own proof.** HMAC-SHA256 over `org_id || lead_id`, truncated to 128 bits,
unpadded base64url so nothing in it needs escaping by whatever rewrites links in a recipient's
client. Integrity, not confidentiality: anyone holding the email holds the token and that is the
design — what must be impossible is editing one into a different lead.

**The signing key is derived from `CREDENTIAL_KEY` under a fixed label, not that key itself.**
`CREDENTIAL_KEY` encrypts mailbox credentials; this signs public URLs. One secret for both would
mean a flaw in either reached the other, and deriving adds no new secret for the owner to manage.

**Every failure answers 200 after doing nothing** — an unknown token, a forged one, a lead that
no longer exists. A public endpoint that distinguishes them is an oracle for which tokens exist,
and from the recipient's side the outcome is identical either way: they will not be emailed
again. `verify()` returns `None` for every malformed input and raises for none, because a public
route that can be made to raise is a 500 handled outside the CORS middleware — the trap that once
made a duplicate key look like an unreachable backend.

**The org–lead pairing is checked before writing**, which is the one forgery the MAC cannot catch
alone: an attacker holds no secret, so they cannot re-sign, but the route verifies the lead
actually belongs to the org the token names rather than trusting the pair.

**`PUBLIC_API_URL` is its own setting, and empty means no one-click header at all.** Deriving it
from `GOOGLE_REDIRECT_URI` would be a public URL that breaks quietly when that value changes, and
a URL built on a guess would have receivers POST opt-outs into the void — which is the defect
part 1 of this ADR exists to fix, wearing a different hat.
## ADR-032 — Job deduplication is scoped to the workspace

**Date:** 2026-10-31
**Status:** Accepted
**Relates to:** F21, `docs/IMPLEMENTATION_PLAN_V3.md` §1.2.1

### Context

`jobs.dedupe_key` plus a partial unique index is what makes enqueueing idempotent: the scheduler
may notice the same due lead twice, and `enqueue` returning `None` for a duplicate is how that
stays harmless (ADR-014 — a job is enqueued in the same transaction as the state change that
justifies it, which is why there is no broker).

The index, since migration 1, was unique on **`(kind, dedupe_key)`**. No `org_id`.

**So a live job in one workspace refused the same key in every other workspace.** And the refusal
is reported by returning `None`, which every caller in the tree treats as "an identical job is
already queued, nothing to do". The second workspace's work would simply not happen: no
exception, no log line, no dead job, nothing on any screen. It is the quietest possible failure,
and it is a cross-tenant one in a codebase whose whole schema is built on ADR-004.

**Why it has never bitten.** Every dedupe key in the tree today is built from a UUID —
`draft:{lead.id}`, `lead:{lead.id}` — and a UUID cannot collide across orgs. The guarantee held
by accident of key choice rather than by design.

**Why it must be fixed now.** It stops holding the moment a key is built from an identifier two
workspaces genuinely share: `feed:greenhouse:{token}`, `company:{domain}`, `poll:{ats_slug}`.
Those are exactly the keys the job-post intake (P12) and signal polling (P15) need. Two users
watching the same company is the ordinary case, not an edge one — this is a product where that
should be expected.

### Decision

The index becomes unique on **`(org_id, kind, dedupe_key)`**, same partial predicate
(`dedupe_key IS NOT NULL AND status IN ('queued','running')`). `enqueue`'s
`on_conflict_do_nothing` names the same three columns, because Postgres infers the constraint
from them and a mismatch is rejected outright.

Three places express this and all three were changed together: the migration, the `Index` in
`models.py` metadata (without which autogenerate reads the index as drift and writes a migration
that **drops** it — which it has done once already, per the comment that has sat there since),
and the `index_elements` in `queue.py`.

### Consequences

Two workspaces can hold the same key at once; within one workspace a duplicate is still refused,
which is what dedupe is for and is now its own test.

**The migration is index-only — no row is read, altered or moved** — and creates the new index
before dropping the old one, so at no instant is the guarantee weaker than it was.

**The downgrade needs care the upgrade does not**, because the old index is *stricter*. If two
workspaces hold a live job with the same `(kind, dedupe_key)` — precisely the case this ADR
allows — recreating the old index would fail. The revert therefore **cancels** the newer
duplicates rather than deleting them, with the reason written into `last_error`: a cancelled job
is visible and can be enqueued again, where a deleted one is gone. Both directions were run
against the development database and the index was checked each way.

### What was rejected

- **Leaving it and documenting the constraint on key naming** ("never build a key from a shared
  identifier"). That is a rule held in one person's head, enforced by nothing, against a failure
  mode that is invisible when it happens.
- **Dropping dedupe and relying on handler idempotency.** The index is what makes the
  enqueue-in-the-same-transaction design safe to retry; handlers being idempotent as well is
  defence in depth, not a replacement.
- **Including `lead_id`.** It is nullable, and NULLs are not equal in a unique index, so
  lead-less jobs would stop deduplicating at all.

### Addendum, 2026-10-31 — recording an outcome requires still holding the lease

Found while writing the crash-safety test this ADR's half of F21 called for (slice S11.3), which
is the argument for writing it.

`queue.succeed()` had no ownership check, and `JobRunner._keep_lease` **deliberately tolerates a
failed heartbeat** — the comment says so, and it is right to, because a reaped-and-retried job is
the queue working as designed. The two together left a hole:

1. Worker A claims a job. Its lease lapses: a slow job, a long pause, a blip.
2. `reap_expired` requeues it, worker B claims it and runs the handler.
3. Worker A, oblivious, finishes and calls `succeed()` — which had no idea A no longer owned it.

**The handler ran twice and nothing about the queue looked wrong.** Nothing was ever *sent* twice,
because that is the outbox's unique `idempotency_key` and it is tested with two concurrent claims
in `test_outbox.py` — but every other side effect a handler has was duplicated.

**Decision.** `succeed()` takes `worker_id` and scopes the `UPDATE` by `locked_by`, returning
whether the row was still ours. The runner passes its own id, and a `False` raises inside the
`session.begin()` block — so **the handler's work is rolled back with the outcome it could not
record**. Only the worker that still owns the job leaves anything behind.

The lost attempt is **not** routed through `_finish`. Recording a failure there would stamp an
error on a job the new owner is running and burn an attempt belonging to them; the new owner
decides the outcome. It is counted as `lost` in the runner's report, separately from `failed`,
because nothing went wrong with the work.

`worker_id=None` keeps the unscoped behaviour for callers that hold no lease, so every existing
caller and test is unaffected.

**What was rejected:** making the heartbeat fatal. A worker that cannot reach the database for a
moment would then abandon work it still owned, which trades a rare duplicate for a common loss.
The lease is the authority, and the check belongs where the outcome is written.

### Addendum 2, 2026-10-31 — starvation is solved by ordering, not by refusing work

The last of F21 (slice S11.2). The criterion was "a workspace over its queue cap cannot starve
another", and the important word is **starve**.

`claim` ordered by `(priority, run_after)` across the whole table. A workspace that imported a
thousand leads therefore held the thousand oldest `queued` rows, and every claim for the next
while was theirs — correct by that ordering and useless by any other measure, because a second
workspace's single urgent job waited behind all of it.

**Decision: round-robin across workspaces.** Each workspace's due jobs are ranked among its own
with `row_number() over (partition by org_id order by priority, run_after)`, and the claim takes
everybody's first before anybody's second. Ordering *within* a workspace is untouched, so a
priority still means what it meant. This is the same answer PR #67 reached for two campaigns
sharing one mailbox, and for the same reason: with one shared allowance, strict
first-in-first-out lets a large holder starve a small one for a working week.

**A queue cap is deliberately not the mechanism.** An import of a thousand leads legitimately
creates a thousand jobs, spread by `run_after` over the days they could actually be sent.
Refusing that would break the product to solve a problem the ordering already solves. So
`MAX_QUEUED_JOBS_PER_ORG` is a **runaway guard** set at 20,000 — far above any real import — and
reaching it means something is enqueueing in a cycle.

**It raises `QueueFull` rather than returning `None`.** `enqueue` reports a duplicate with
`None`, and every caller reads that as "already queued, nothing to do", so reporting a full queue
the same way would silently drop work — the very bug the first half of this ADR fixed. The
enqueue shares its transaction with the state change that justifies it (ADR-014), so raising
abandons both, which is right: the work is not recorded as queued when it is not.

The import route catches it and answers **429 with the reason**, rolling the whole import back
rather than leaving leads half-queued — a lead with no research job sits `NEW` for ever, looking
imported and doing nothing. It is caught rather than allowed to escape because FastAPI handles a
bare exception in `ServerErrorMiddleware`, which sits *outside* `CORSMiddleware`, so a 500 would
have the dashboard report the backend unreachable while it answered perfectly. That trap has
already cost this project one long investigation.

**Verified against real Postgres rather than only in tests**, because `FOR UPDATE ... SKIP LOCKED`
alongside a window-function subquery join is the kind of SQL a type checker cannot judge: a probe
against the development database with four jobs in one workspace and one in another returned one
from each for a claim of two.

---

## ADR-034 — Deliverability may only pause

**Date:** 2026-10-31
**Status:** Accepted
**Relates to:** F3 in `docs/OUTREACH_PLATFORM_V3.md`, slice S11.6

### Context

Gmail and Yahoo cap spam complaints at 0.3% and judge senders on bounce rates too. The first
reply this system ever received was in Spam, and nothing in the system could have told us: there
was no per-mailbox health signal at all, and `mailboxes.paused` was a flag only a person set.

### Decision

`mailbox_health` holds one row per mailbox per IST day — sent, bounced — and an hourly
`Scheduler` pass rolls it up and then **pauses** any mailbox whose bounce rate over a rolling
window has crossed the limit, raising an alert that names the counts.

**The one rule everything else follows from: this pass may only restrict.** There is no branch
that resumes a mailbox, raises a cap, extends a window or touches a send mode — asserted over the
source by `test_nothing_here_can_resume_a_mailbox_or_raise_a_cap`, which also strips comments
first so the prose explaining the rule cannot satisfy it.

That asymmetry is what makes automatic action acceptable without a human in the loop. **A signal
that can only restrict cannot cause the outage it would then report.** Resuming is the risky
direction, so resuming stays a person's decision. The structural test exists because the edit
that breaks this is one somebody would make for a good-sounding reason — "un-pause it once the
rate recovers" is exactly how this would become something that resumes sending on its own.

### Three guards, which matter more than the threshold

1. **Minimum volume (20 sends).** At five sends one bounce is 20%, a number about nothing.
   Alerting on noise is how an operator learns to ignore alerts.
2. **A rolling 7-day window, not a day.** One bad afternoon is not a reputation problem.
3. **An already-paused mailbox is skipped**, so the alert fires once rather than every hour for
   something somebody is already dealing with.

The threshold is 5% — the lenient end of the usual 2–5% guidance — because the action is
automatic and stopping a campaign wrongly is worse than stopping it a day later.

### A rollup, not counters

`mailbox_health` is recomputed from `messages` and `classifications`, which are the record.
Counters incremented at send time drift: a crash between the send and the increment leaves one
wrong for ever and nothing notices. The cost is that the numbers lag one pass, which for a signal
measured in days is not a cost.

### `complaint_rate` is nullable and never written

Deliberately, not unfinished. A complaint rate needs a feed from the receiving provider, and
Postmaster Tools is a link we ask the owner to open rather than an integration (ADR-029). **A
zero would read as "no complaints" when it means "we cannot see complaints"**, and the entire
value of this table is that an operator can trust what it says. The column exists so a future
provider feed has somewhere to go, and `source` says where a number came from.

### The alert kind carries the mailbox id

The cool-down dedupes by kind, so without the id the first mailbox paused would silence every
other mailbox in the deployment — and the one that never got its alert is the one still bouncing.
The same reasoning as the OAuth grant warnings (ADR-021), and three tests fail without it.

### Consequences

A mailbox bouncing badly stops by itself, within an hour, with an alert saying why. Nobody has to
be watching. The deliverability signal the project has been missing since its first send now
exists, and it is the half that works without Postmaster Tools.

What it still cannot see is complaints, which is the number Gmail actually judges on. DMARC
aggregate reports and Postmaster remain the owner's manual steps (`docs/POSTMASTER.md`).

### What was rejected

- **Auto-resuming when the rate recovers.** The whole safety argument is that this can only
  restrict.
- **Lowering the cap instead of pausing.** A cap is a product rule; this is a fault signal, and
  mixing them would mean a bug here quietly changed how much a healthy workspace may send.
- **Counting at send time.** Covered above: counters drift silently.
- **Writing `complaint_rate = 0.0` until a feed exists.** Covered above: a fabricated zero is
  worse than a null.

---

## ADR-035 — A provider's domain is not the sender's to configure

**Date:** 2026-10-31
**Status:** Accepted
**Relates to:** F4, slice S11.7, and ADR-023 (every workspace sends from an account its owner
connected)

### Context

The sender-requirement report (S11.5) ran every sending domain through the same DNS checklist.
Correct for a domain the sender owns, and wrong for the commonest case in a multi-user product.

`gmail.com` answers an SPF record and a DMARC record, and **no selector we could guess** —
Google signs outbound Gmail with its own rotating selectors. So a user who connected a Gmail
address was told their domain was **not ready**, over a DKIM record they cannot publish, do not
control and do not need. Most people signing up connect a Gmail address, so that was the
majority of users being told to fix something unfixable.

This was caught by the owner pointing out, for the second time, that the product is not tied to
one domain. The code was already domain-agnostic — ADR-023 removed the deployment mailbox and no
sending logic names a domain — but the *report* had been written as though every sender owned
their domain, which is exactly the assumption a single-tenant mindset produces.

### Decision

A domain in `PROVIDER_DOMAINS` is reported as the provider's business: one **Authentication**
check, passing, saying there is nothing to publish. No DNS query is made at all, because nothing
in DNS could change the answer.

The detail carries the two things that **are** true of a shared domain, because both are easy to
assume away:

1. **You share its reputation** with everybody else sending from it.
2. **You cannot add it to Postmaster Tools**, because it is not your domain.

Followed by the one sentence that makes it actionable rather than a shrug: sending from a domain
you own gives you both of those.

**One-click unsubscribe is still checked and can still fail.** That header is ours, not the
provider's.

A domain the sender owns gets the full four-item checklist, unchanged. The point is to tell the
two cases apart, not to skip the checks — asserted by
`test_a_domain_the_sender_owns_still_gets_the_full_checklist`.

### Consequences

A Gmail user sees `ready: true` and one honest line instead of a warning they can do nothing
about. A user on their own domain sees the checklist that is genuinely theirs to act on. The
report keeps being read, which is the only property that matters about a report.

`provider_owned` is on the response so a screen can say this in words rather than leaving
somebody to infer it from a single row.

### What was rejected

- **Probing more selectors.** DNS cannot enumerate selectors, so the list could never be
  complete, and a longer list would still fail for a provider rotating them.
- **Reporting it as `UNKNOWN`.** Honest about our knowledge and useless to the reader: nothing
  they do can resolve it, so it would sit there for ever.
- **Hiding the shared-reputation and Postmaster limits** to keep the message simple. They are
  the real difference between sending from Gmail and sending from your own domain, and somebody
  choosing between the two deserves to know.

---

## ADR-036 — Public ATS feeds are the sanctioned substitute for scraping job boards

**Date:** 2026-10-31
**Status:** Accepted
**Relates to:** F5, slices S12.1 and S12.2

Greenhouse, Lever and Ashby publish JSON job feeds. We poll those for boards the user names, and
for everything else — Naukri, Instahyre, a LinkedIn post — the user **pastes** the URL and the
text. Nothing fetches a pasted URL, asserted by a structural test over the router: that is what
keeps this on the right side of hard rule 4 and of each board's terms.

**The endpoints were tested before anything was built on them**, because the v3 doc's URLs came
from one third-party article. All three answer 200; `docs/ATS_FEEDS.md` records the shapes. The
difference that mattered: **Lever returns a bare JSON array where the other two wrap theirs in
`jobs`**, and its timestamps are epoch milliseconds. A parser written against an assumed shape
would have failed for exactly one source, which reads like a bad slug rather than a bad
assumption. Greenhouse's list response carries no description at all, so `body_text` is nullable
from the first migration rather than discovered to be optional later.

A response size cap as well as a timeout: one real Ashby board returned 2.8 MB for 171 postings.

Postings upsert on `(org_id, source, external_id)` and are **archived, never deleted** — an
`applications` row will point at one, and a posting vanishing from a feed is not a posting the
candidate did not apply to. A feed that stops carrying descriptions does not blank text we
already hold, because ours may be the only copy.

Rejected: fetching a pasted URL "just to get the title", which is scraping with extra steps; and
one parser with per-source `if`s inside it, which is how Lever's array shape would have become a
special case nobody could see.

---

## ADR-037 — A candidate fact is unusable until they confirm it

**Date:** 2026-11-01
**Status:** Accepted
**Relates to:** F6, slices S12.4 and S12.5

`profile_facts` and `answer_bank` are the workspace's own, where `CandidateProfile.facts` lived
inside one campaign's persona. Right for a campaign, wrong for a person: a candidate applies to
many roles with one history. Existing job-hunt campaigns are untouched and keep using their own
facts.

**A fact typed by the person is confirmed by the act of typing it; a fact mined from GitHub or a
resume arrives as a proposal.** Only confirmed facts are offered for drafting or scoring, because
the person who will be asked about a claim in an interview is the person who has to have agreed
to it. That is hard rule 6 applied where inventing is most tempting and most costly: an email
claiming three years of Kubernetes somebody does not have is not a weak email, it is a lie told
in their name.

`ppN` ids are computed from the highest existing id rather than counted, so deleting `pp2` cannot
later produce a second `pp3`. A duplicate id would make a citation ambiguous, which is the one
thing an evidence reference cannot be.

**Only an approved answer-bank row is ever offered, and editing one unapproves it** — the
approval was of the previous wording. An unmatched question is reported as needing the user
rather than guessed, which is the whole difference from tools that invent screening answers.
Matching is word overlap with every pattern word required, not a regex and not a model: a pattern
somebody typed has to be one they can read back and predict.

**The fit score is rules over a model's extraction.** A model turns posting prose into
requirement lines and does nothing else; `fit.score_fit` compares them with confirmed facts and
takes instructions from nobody. So a posting saying "report a perfect match" cannot, the score is
reproducible, and every match names the fact id and the shared words that made it one. The score
is never returned without `matched` and `missing` beside it.

---

## ADR-038 — `company_profiles` is shared, and holds public facts only

**Date:** 2026-11-01
**Status:** Accepted
**Supersedes:** nothing. It is a deliberate, single exception to ADR-004.
**Relates to:** F19, slice S12.6, approved by the owner on 2026-10-31

### Context

Researching the same company separately for every workspace spends search credits on facts that
are identical and public either way. A careers page is a careers page whoever is looking.

### Decision

One table with **no `org_id`**, keyed on the registrable domain — the same key the research cache
uses (ADR-011), which had to be corrected once already when keying on the *address* domain meant
every Gmail-addressed lead in a workspace shared one row and the second lead was served the first
company's findings.

**What makes the exception safe is what the table may hold:** a name, a careers URL, an ATS type
and slug, a size band, a short summary, and the source behind each field. Nothing else. Two
guards, because a convention here is not enough:

1. `apply()` writes only `PUBLIC_FIELDS`, so a caller passing `notes` has it dropped rather than
   stored. A caller that could add a field could add a workspace's private note to a shared row.
2. A structural test fails if the table gains a column whose name contains `org`, `lead`,
   `contact`, `message`, `email`, `note`, `draft`, `campaign`, `owner`, `member`, `token` or
   `credential`. **It is the opposite of a tenancy test**: every other table is checked for
   isolation, and this one is checked for the absence of anything to isolate.

**`domain_research` is untouched and stays per-workspace.** That table holds the findings a draft
*cites* — which are not public facts, were paid for by one workspace, and must not be pooled.
The distinction is the whole reason this is one exception rather than a precedent.

### Fair share, and not doing work twice

A **cache hit costs no credit**, which is the entire point. A **fetch** counts against a daily
per-workspace budget read from `search_usage`, so one workspace importing a thousand companies
cannot spend everybody's allowance — the same rule as the job queue's, for the same reason: a
limit is per workspace or it is a shared outage.

Credits are **summed**, not searches counted, because one search can cost several and credits are
what the provider bills. (The column is `at`, not `created_at` — the same naming trap as
`Event.at`, caught by the type checker for the third time in this project.)

Past the age limit, the **content hash is compared before anything is written**: an unchanged
page costs the fetch and nothing else, and `version` only moves on a real change, so a consumer
holding version N can trust that N still means what it meant.

### What was rejected

- **Pooling `domain_research`.** Those findings are cited in somebody's email and were paid for
  by one workspace.
- **Pooling uploaded email lists or suppression lists** (F23). Consent and purpose limits under
  the DPDP Rules, and it stays unbuilt pending legal advice.
- **A headcount instead of a size band.** A precise number is wrong within a month, and a shared
  wrong number is wrong for everybody.

---

## ADR-039 — The extension gateway is a third door

**Date:** 2026-11-01
**Status:** Accepted
**Relates to:** F8, slices S13.1-S13.3; extends ADR-028's two-doors rule

### Context

The Chrome extension needs to ask this system for an answer to a form field. Its credential lives
in a browser extension on somebody's laptop, which is the least defensible place a credential in
this system will ever sit.

### Decision

A third door: `/ext/v1`, authenticated by an `oa_` token whose only scope is `ext:suggest`.

**Each key opens exactly one door.** A Supabase JWT opens the dashboard, an `mcp:` token opens
the connector, an `ext:suggest` token opens the gateway, and none of them opens another. The one
worth stating is that an extension token **cannot** call `/mcp` — if it could, a token sitting in
a browser would read every campaign, lead and reply in the workspace. Five tests assert the
matrix, and the minting route refuses to issue `ext:suggest` alongside an MCP scope at all.

`ALL_SCOPES` is one tuple, and the database's check constraint is built from it, so the
constraint, the minting route and each door's gate cannot disagree about what a scope is.

### What the gateway may do

Suggest. There is no route that submits a form, uploads a file, fills a field, sends mail or
approves anything, and a structural test asserts the module imports neither the mail services nor
any HTTP client. **The server returns data, never rules** — the store's policy requires the
extension's logic to ship inside the package, and a server that could send a rule is a server
that could send a different one later.

**An unmatched label comes back as `needs_user`, never as a guess.** No model is reachable from
this path at all, which is deliberate: a guess dressed as an answer is worse than a blank, and
guessing screening answers is exactly what the tools we are not copying do.

### What it stores

A form capture stores the **labels and the URL and no page text**. Only labels are needed to
match an answer; the rest of an application page is the user's own data and somebody else's copy.
Captures are kept for days, and `PRIVACY.md` says so.

`suggestions.state` records accepted, edited or rejected. An edit is the most valuable of the
three (ADR-026): it says what the answer should have been.

### What was rejected

- **One token for everything.** Convenient, and it would make the extension's token the most
  valuable credential in the system.
- **A model to answer unmatched questions.** The honest answer is "we do not have one", and the
  person reading the form is the one who can give it.
- **Sending the whole page for form help.** Nothing needs it, and it is the user's own data.

## ADR-040 — The extension is a side panel with no standing access

**Date:** 2026-11-02
**Status:** Accepted
**Relates to:** F8, slice S13.4; implements the client half of ADR-039

### Context

ADR-039 built the gateway and argued the containment is the *token*: an `ext:suggest` credential
can read the answer bank and record an outcome, and can reach neither `/api/v1` nor `/mcp`. That
bounds what a stolen token achieves. It says nothing about what the extension itself can do to a
page the user is looking at, and that is the larger surface: an extension with a content script
on every page can read every page, whatever its server is allowed to answer.

The feature also has to survive the Chrome Web Store's rules, which forbid remotely hosted code
— so the logic that recognises a board cannot be served from our API.

### Decision

**No content script in the manifest.** `collect.js` is injected by the side panel on a button
press, under `activeTab`. Chrome grants that for one tab, for as long as it stays on that page,
and only because the user clicked this extension's own toolbar button. **There is no code path
by which the extension could read a page nobody asked about** — which is a stronger statement
than a privacy policy, because it is checkable.

**No host permissions in the package.** `optional_host_permissions` only, and the user's own API
origin is requested at the moment they save its address. So the install prompt covers nothing,
and the grant names one host. A packaged wildcard would be a permission over the entire web,
accepted once at install time for a reason nobody could evaluate.

**The mappers ship inside the package.** Required by the store, and the better design regardless:
a selector a server could change is a selector that can start reading the wrong element on a page
the user is about to submit.

**Four absences are asserted over the source**, with comments stripped first so the prose
explaining a rule cannot satisfy it: no form submission, no assignment into a page's field, no
file upload, and no read of a field's *contents* — `getAttribute("value")` and `.checked`
included, since both are the back door round the assignment rule. The panel also has **no
channel back into the page**: nothing sends a message to the content script and the content
script registers no listener, so a panel that wanted to fill the form has nothing to ask.

**A page that cannot be fully read is reported as incomplete.** Some boards render the form
inside a closed shadow root, where `host.shadowRoot` is null by design. An open root is walked; a
custom element with no open root is counted as a field we could not read. That **over-reports** —
not every `<x-y>` is a field — and over-reporting leaves the panel saying the list may be
incomplete, which is the harmless direction. Claiming a complete reading of a page we cannot see
into is the other one.

**A radio or checkbox takes its group's legend, not its wrapping label.** Lever wraps each
*option* in a label, so the obvious rule asks the answer bank about "A referral" once per option
instead of "How did you hear about this role?" once.

**`name` is never used as a question.** `job_application[answers_attributes][3][text_value]` is
not a question anybody wrote an answer to, and neither is nearby text: a `<div>` above an input is
a question often enough to be tempting and wrong often enough to produce a confidently misapplied
answer. No label of any kind means no question, counted and reported.

**The token lives in `chrome.storage.local`.** `sync` would push a bearer credential through the
user's Google account to every browser they sign into.

### Consequences

- The extension is useless until the user pastes an address and a token, and that is the install
  experience: nothing is granted before there is something to grant it for.
- `form.ts` exists separately from `collect.ts` for a mechanical reason that is easy to lose:
  `executeScript({ files })` injects a **classic** script, so one `export` statement in that
  bundle is a syntax error at the moment of injection. Keeping every export out of the entry
  means the built content script has no module syntax at all, and `tests/build.test.ts` asserts
  that against the real build — which is also why CI builds before it tests.
- The scanner tests run against **hand-written** fixtures, not captured pages, and
  `tests/fixtures/README.md` says so. They prove the scanner's rules, not any live board's
  current markup. What makes that acceptable is the `generic` mapper, which takes the page's
  largest form, so the per-board mappers are an optimisation over a fallback that works.
- The safety guard is duplicated in `tests/unit/test_extension_package.py`. Deliberate: the npm
  suite is a job that can be skipped or reordered, and the Python suite gates every change.

### Rejected

- **A declared content script on the boards we know.** Simpler, and it would mean the extension
  reads every Greenhouse, Lever and Ashby page the user ever opens, whether they asked or not.
- **Fetching the mappers from the API.** A store violation, and a way for a server change to
  start reading the wrong element on somebody's half-filled application.
- **Filling the fields.** The feature people would ask for next, and the one that turns a
  suggestion the person read into an answer submitted in their name. The code for it is absent
  and asserted absent.
- **Playwright against the three live boards.** It is the test that would prove the most, and it
  needs a browser download in CI and a network call to somebody else's application form on every
  run. jsdom against saved structures, plus a fallback that does not depend on them, buys most of
  it for none of that.

## ADR-041 — Contact lookups are a button press on the user's own key

**Date:** 2026-11-02
**Status:** Accepted
**Relates to:** F9, slice S13.5; `docs/CONTACT_PROVIDERS.md`

### Context

A job-hunt campaign needs somebody to write to, and a posting gives a company rather than a
person. Apollo, Hunter and Snov all answer that question, for money.

Two things make this different from every other integration in this system. **It spends the
user's money**, not ours — so the question is not only "is it correct?" but "can it bill somebody
for work they did not ask for?". And **it returns personal data about people who have not heard of
us**, arriving from a third party, which is a privacy surface before it is a feature.

The owner holds no provider key (plan question 3, answered "none"), so the whole thing had to be
buildable, testable and demonstrable without one.

### Decision

**Nothing but a person starts a lookup.** There is no job kind, no scheduler pass, and no worker
module that imports the lookup. `tests/unit/test_contacts_button_press_only.py` asserts all three
over the source tree with `ast`, and asserts *positively* that the only caller of `lookup.run` is
the contacts router — so a caller added later fails rather than being quietly allowed. A lookup
that could run by itself is one that bills somebody in the background, and the evidence would be
a provider's invoice.

**No `GET` on this router talks to a provider.** `GET /mailboxes` is non-networked because
refreshing it would mint OAuth tokens; here the stake is higher, because a `GET` that called a
provider would spend the user's credits every time somebody pressed F5.

**There is no shared key.** No `APOLLO_API_KEY` in `Settings`, no deployment credential, no
fallback. The same argument as ADR-023, which removed the deployment's mailbox: as long as one
identity lives in the environment, every question about who may use it — which org, which address,
what if two match — is a rule somebody has to hold in their head and a guard somebody has to
maintain. A workspace with no key gets a refusal naming what to do.

**Hard rule 8 is in the schema, not in a code path.** A partial unique index on
`(org_id, provider) WHERE revoked_at IS NULL` makes holding several live keys for one provider
inexpressible, so "rotate five keys to dodge a free tier" is not a loop somebody writes later.
Saving a key revokes the previous row rather than deleting it, so the change stays auditable.

**Three refusals before any network call**, cheapest first: no key or a key the provider already
refused; over the daily budget; no domain. Calling with a key we know is dead spends a request to
be told what we recorded last time.

**The budget and the audit trail are the same fact.** 25 lookups per workspace per rolling 24
hours, counted from `events` rows of type `contact.lookup`. `events` refuses UPDATE and DELETE by
trigger, so the count cannot drift — where a counter column incremented at call time can, and the
mailbox health rollup already chose recomputation over a counter for exactly that reason. It also
means a lookup that spent a credit and left no trace is impossible: **this is the one audit write
in the system that is allowed to fail its request.** Everywhere else the work is the point and the
record of it is not; here the record *is* the accounting.

**A rolling window, not a calendar day**, because a midnight reset in one timezone is a free second
allowance for anybody who waits for it, and this budget exists to stop a loop rather than to
apportion a day.

**A proposal is not a lead.** `contact_candidates` is not `leads`, so nothing a provider proposed
can be picked up by the send loop. Turning one into a lead is a separate explicit action.

**And that action goes through the ordinary importer, as a one-row CSV.** This is the decision most
worth defending, because it looks like indirection for its own sake. `importer.commit` is where the
suppression check, the MX check, the already-in-this-campaign check, the free-mail warning and the
batch id that makes an import undoable all live. A second `Lead(...)` insert would be a second
place every one of those could be forgotten — and **the one that would get forgotten is the
suppression check**, which is the one that must not be: a provider will cheerfully propose somebody
who asked us never to write again, because it has no idea they did.
`test_a_suppressed_address_is_refused` is the most important test in this slice.

**The importer needs a company name and a provider does not give one.** The shared public-company
cache (F19) usually has it, since this is a company somebody has already looked at. When it does
not, the fallback is **the domain itself** — a true fact about the company. Deriving a trading name
from it (`acme.test` → "Acme") would be inventing one, and hard rule 6 applies even to a field this
minor.

**`email_status` is a column, not a note inside a confidence score.** Several providers return
`first.last@domain` built from a naming convention and present it beside a verified address.
Mailing a guess is how a bounce rate rises, which now pauses a mailbox by itself (ADR-034), so the
distinction has to survive into the review screen rather than being folded into a number.

**Apollo's locked placeholder is stored as no address at all.** `email_not_unlocked@domain.com`
sitting in an email column is an address something eventually tries to mail. The proposal keeps its
name and title, which is still something a person can act on, and the import route refuses it with
a sentence rather than a validation error.

**A response we cannot read raises.** `ProviderUnreadable`, naming the provider and the missing
field. The job-post intake taught this: an unknown source whose payload happened to have the right
shape returned **zero postings**, indistinguishable from an empty board. Here the equivalent reads
as "nobody works at this company", and somebody would believe it.

**No raw provider payload is stored.** It would be the fastest way to debug a shape change, and it
is a third party's personal data about people who have not heard of us. The fields we chose are the
fields we keep.

**The key travels in a header.** Hunter documents `api_key` as a query parameter; a credential in a
URL is a credential in an access log, a proxy log and a browser history. A test asserts the key
does not appear in the request URL.

**`mock` is a first-class provider, in the check constraint.** Without it the feature would be
unreachable and untestable on this deployment, and its first run would be against a paid account.

### Consequences

- **`SettingsDep` exists because of this slice, and it closes a real gap.** Sealing a provider key
  needs `CREDENTIAL_KEY`, and routers until now reached for the module-level `config` singleton —
  so an app constructed with its own `Settings` was partially ignoring them. That is the signature
  shape again: a correct producer (`create_app(settings)`) and consumers reading a different
  object. New routers take `SettingsDep`; the older ones still import `config` and that is recorded
  rather than silently left.
- **It surfaced a hard-rule-2 near miss.** The global `config` is built at **import time** and does
  read `.env`, which `tests/conftest.py`'s session-scoped `hermetic_settings` cannot reach — so a
  failing assertion in this session printed a real provider key into the terminal. Exactly the
  incident that fixture was written for, in the one place it does not cover.
- **Verification is a real lookup**, because none of the three offers a "is this key valid?"
  endpoint. It therefore counts against the budget and may spend a credit, and the response says so
  rather than presenting it as free. The default domain is `example.com`, reserved by RFC 2606, so
  the check does not spend a credit on a real company nobody asked about.
- **The shapes are from documentation, not from calls.** `docs/CONTACT_PROVIDERS.md` is a table of
  what is verified and what is disputed — the Apollo path and the Snov flow both have sources that
  disagree. Each provider's response is read in exactly one function, so a wrong one is one
  function and one test.
- A `.test` domain cannot be imported: the importer's address validator refuses reserved TLDs,
  correctly. Tests use `example.com`, as hard rule 9 requires anyway.

### Rejected

- **A deployment-wide key with per-workspace quotas.** Cheaper for the user and it recreates every
  problem ADR-020 and ADR-023 were written to remove, plus a new one: hard rule 8's one-account
  rule would be broken by the architecture rather than by a user.
- **Verifying a key on save, silently.** A write with a cost nobody asked for. It is a button.
- **Enriching automatically after a feed poll.** The obvious next feature, and it is a background
  process spending the user's money on companies they have not looked at yet.
- **Storing the raw provider payload for debugging.** See above.
- **A second `Lead(...)` insert for imported contacts.** Shorter, and it would eventually mail
  somebody who had opted out.
- **Buying an account to live-test the three endpoints.** Hard rule 5. The honest alternative is the
  `mock` provider plus a table saying which shapes are assumed, and that is what shipped.

## ADR-042 — The referral finder reads a file and never a profile

**Date:** 2026-11-02
**Status:** Accepted
**Relates to:** F10, slice S13.6; applies hard rule 4 and ADR-038

### Context

The strongest thing a job applicant has is somebody on the inside. LinkedIn knows who that is,
and every tool in this category gets it by scraping — which hard rule 4 has forbidden since the
first commit, and which ADR-038 restated: LinkedIn data enters this system only through a file
the user uploads.

So the feature is: the user downloads their own connections export, uploads it, and the system
says who they already know at a company and suggests something to say.

### Decision

**Nothing in this package can fetch anything.** Not "does not" — cannot: no module imports
`httpx`, `requests`, `urllib`, `aiohttp` or `socket`, and `tests/unit/test_referrals_never_fetch.py`
asserts that with `ast` over the package and its router, along with the absence of any LinkedIn
hostname in any form. The `profile_url` that reaches a response came out of the user's own file
and is shown for them to open.

**Nothing here sends anything either.** A referral ask is a LinkedIn message the user sends under
their own name. The tree-wide send guard already covers the package; this adds the narrower claim
that nothing here names the outbox, the message builder or the send loop.

**The export's `Email Address` column is dropped before anything is written.** This is the
decision the feature turns on. A connections export is a few thousand people who agreed to
connect with *the user* and have never heard of us; keeping their addresses would make this a
contact database with a different name, and it is the one dataset in this system that could be
pooled or sold by somebody who came after us. Three independent guards: the parser drops the
column, `connections` has no column to put it in, and a test parses a file containing an address
and checks the stored row for it.

**The note is built by rules, not by a model.** Every substitution is a value from a row: the
connection's own name, their own company and title as *they* wrote them, the posting's title and
link, and one **confirmed** `profile_facts` entry when there is one. Hard rule 6 forbids
inventing facts about the sender; here the reader is a friend who would notice, and the cost is
not a weak email but the user telling somebody something untrue about themselves. A test asserts
no model is reachable from the package at all.

**The export begins with a preamble, and the header row is found rather than assumed.** LinkedIn
writes a `Notes:` block and a blank line before the headers. A parser taking line 1 as the header
reads the whole file as one unnamed column and reports **zero connections** — indistinguishable
from an empty network, and the same silent wrong answer as the ATS feed that returned no
postings. A file with no recognisable header is an error with a sentence, **never** an empty
result: "we read your file and you know nobody" is the one answer this must not give to somebody
who uploaded the wrong CSV.

**Company names are normalised, and the normalisation refuses to empty a name.** The export says
whatever the connection typed into their own profile, so "Acme, Inc.", "Acme Pvt Ltd" and "ACME"
have to be one key. Suffixes are stripped repeatedly — but **stripping stops at the last word**,
because "Technology Group Ltd" is somebody's actual company and reducing it to nothing would
match it against every other all-suffix name. That would be the worst kind of match: confident,
wrong, and about a person the user is about to message.

**Each match carries its reason in words.** Exact, or containment in either direction, said
plainly — the user is the one deciding whether a subsidiary or a similarly named company counts,
and a bare list gives them nothing to judge with.

**A second export updates rather than inserts.** A re-export is mostly the same people, and
inserting would show each of them twice in a shortlist, which reads as broken rather than long.
The newer row wins on title and company, because a note quoting a job somebody left two years ago
is worse than one with no title at all.

**`DELETE /connections` is a real delete.** Unlike a suppression, forgetting a connection cannot
cause anybody to be contacted, so there is nothing worth a tombstone — and the person who
uploaded a few thousand strangers must be able to take them out completely.

**`GET /connections` returns a count, not a list.** A route that paged through thousands of named
people would be the contact database this is carefully not. The question somebody actually has is
"did the upload work?", and then they search it by company.

### Consequences

- The feature is only as current as the user's last export, and that is visible rather than
  hidden: the upload reports what it stored and updated, and nothing refreshes by itself.
- A connection whose profile has no company is stored and matches nothing. Dropping them would
  make a re-export look as though people had left.
- A row with no profile URL cannot be deduplicated, so a re-export inserts it again. Rare — the
  export has carried the URL for years — and a duplicate is better than dropping somebody.
- `sender_name` is an explicit parameter and is **not** `user.email`: signing a note with an
  address reads oddly and would put the user's address into text they paste into somebody else's
  message box. With no name the note ends "Thanks," and the person types their own.
- A test of mine failed on the router's own note saying addresses are discarded — prose about a
  rule breaking the rule's own guard. The check now ignores string constants, which is the same
  lesson as stripping comments before a structural assertion.

### Rejected

- **Fetching a profile to refresh a job title.** The whole rule, in one convenient exception.
- **Keeping the email column "in case we need it later".** That is how a referral finder becomes a
  contact database, and "later" is a decision nobody would be present for.
- **Asking a model to write the note.** It would read better and it would occasionally invent a
  shared project, in a message to somebody who would know it had not happened.
- **Fuzzy name matching (edit distance, token overlap).** It finds more and it proposes strangers
  at similarly named companies as though they were friends. Exact and containment, with the reason
  shown, keeps the judgement with the person.
- **Matching connections against leads automatically.** A background pass over the user's whole
  network, producing suggestions nobody asked for. The user names the company.

## ADR-043 — A tailored resume is an ordered selection of confirmed facts, never a rewrite

**Date:** 2026-11-02
**Status:** Accepted
**Relates to:** F20, slice S13.7; applies hard rules 5, 6 and 7, and ADR-037

### Context

Every tool in this category rewrites a resume with a model, and the output reads well. The cost
is invisible until an interview: a number the candidate cannot account for, a tool they have
never used, an employer they did not work at. Hard rule 6 forbids inventing facts about the
sender, and this is the sharpest case of it in the whole system — the reader is a hiring manager
who will ask, and the person answering is the user.

ADR-037 already put the machinery in place: `profile_facts` holds what the candidate can
truthfully say, and a fact is unusable until they have confirmed it, because the person who gets
asked about a claim is the person who has to have agreed to it.

### Decision

**A resume is an ordered selection of confirmed facts, and nothing else can get a line into it.**
The model proposes **edit operations** — `include` a fact in a section, optionally reworded, or
`drop` it with a reason — and the order of the `include` ops *is* the order of the document. There
is no operation that writes a heading, and no field anywhere for a header: `ProposedEdits` has
`kind`, `fact_id`, `section`, `text` and `reason`, and the sections are a fixed tuple. The same
device as `propose_reply` having no `subject` argument (ADR-030) — the mistake is not expressible
rather than caught.

**A rewording may not assert anything the fact does not.** Two checks against the fact being
reworded:

1. **Numbers**, compared after stripping thousands separators and a trailing `%` or `+`, so
   "2,000" against "2000" is one quantity and "42%" out of nowhere is refused.
2. **Named things** — tokens that are capitalised, all-capitals, or carry an internal digit or
   `+`, `#` or `.`: `AWS`, `Python`, `C++`, `k8s`, `Node.js`, `Acme`. Between them those cover the
   three inventions that matter on a resume: a skill, a product and an employer.

Lower-case connective rewording is free. So a rewording may use the fact's own vocabulary plus
lower-case words, which is a real constraint — it must begin with a word the fact already uses.
**Position in the sentence is deliberately not used.** Exempting the first token is the obvious
way to allow a capital letter at the start, and it leaves unchecked the one bullet somebody would
actually write: the one opening on a technology they want to be seen to have.

**What this honestly cannot catch**, stated here rather than discovered later: a lower-case
exaggeration — "led a team" from a fact about writing code. No deterministic check could. That is
why a version is shown to the person before anything is attached, and why `created_by` records
whether a model or the person produced it.

**Validated twice, and the second one is the point.** `validate_ops` checks the plan;
`check_rendered` checks the finished document line by line, and `store.create_version` runs it on
the text it is about to write **whatever produced that text**. It looks redundant because `render`
builds the input to it, and it is the check that holds when a second producer appears — this
project's dominant defect, now at nine instances, is a correct producer and a correct consumer
with nothing checking that they agreed.

**Only confirmed facts are shown to the model at all**, which makes the rule true one step
earlier: a model cannot choose a fact it was never offered. The validator still checks, because a
rule enforced in one place is a rule with one bug — and it refuses a proposal **by name**, so
"pp3 is still a proposal" is actionable where "no such fact" sends somebody looking for a typo.

**One retry, with the refusals fed back, then a recorded refusal.** The commonest failure is one
invented number in one bullet, and a model told which bullet and which number returns the fact
unchanged. A second refusal is recorded as a `resume.tailor_refused` event and **the job
succeeds**: nothing went wrong with the work, and a dead job is where somebody looks for a fault.

**`resume_versions` is immutable, by a trigger rather than by convention.** An `applications` row
points at the exact document that was sent, so the question somebody asks a month later — what did
they actually read? — has no answer if the row can be edited afterwards. A change makes a new row.
The escape hatch is `app.allow_resume_version_delete`, exactly as `app.events` has one, so an
accidental `DELETE FROM` fails loudly while a lawful erasure stays possible and obvious in the
code that performs it.

**The PDF is a separate table, and the immutability is why.** A rendering happens after the
version exists, so there is nowhere on an unupdatable row to put it — and `bytea` on the version
would travel with every row of the versions list, the same reasoning as ADR-016 keeping an
attachment off `campaigns`. A re-render replaces; a failed render leaves the previous bytes in
place, because a document somebody is about to attach is worse missing than stale.

**The PDF writer is ours, and there is no new dependency.** `reportlab` and `weasyprint` are large
trees whose purpose is laying out images, tables and flowing HTML, none of which a plain-text
resume has. What this needs is a page of short lines in Helvetica, which is one of the fourteen
fonts a PDF reader is required to provide — so nothing is embedded and nothing is licensed. The
caps (`MAX_CHARS`, `MAX_LINES`) are the memory limit the implementation plan asked for on a 2 GB
host, checked before anything is allocated, and the renderer gets a concurrency of one.

**`(`, `)` and `\` are escaped**, which is a defect rather than a nicety: they are structural in a
PDF string literal, and a resume says "(remote)" often enough that an unescaped writer produces a
file that opens as garbage — looking like our bug and being a quoting one.

**Only a person marks an application applied.** The routes are on the dashboard door, and ADR-039
refuses an `ext:suggest` token everywhere but `/ext/v1`; nothing on `/ext/v1` names these tables,
asserted structurally over both the gateway and the browser package. The reason is not tidiness:
an extension that could mark an application sent would have the tracker assert the person did
something they did not do, and they would stop trusting it — which destroys the only thing a
tracker is for.

**`applied_at` and the status cannot disagree**, by a check constraint rather than by the route
remembering both — the same device as `ck_lead_outcomes_censoring`. And an applied row **cannot go
back to `planned`**: the one thing a tracker must not do is forget that somebody sent a document
to a company.

### Consequences

- A rewording must begin with a word the fact already uses. Refused reworkings cost one edit, or
  the person includes the fact verbatim; the refusal names the offending word.
- "2M" against a fact saying "2 million" is refused. The conservative direction, stated rather
  than smoothed over: a refused correct rewording is recoverable and an invented number is not.
- A resume with no facts in it is refused rather than stored, since an empty version in the list
  is one that cannot be sent anywhere.
- Deleting an org would now fail on the immutability trigger, exactly as it already would on
  `app.events`. Nothing in the tree deletes one.
- `created_by` distinguishes a version the person assembled from one a proposal produced, so the
  dataset (ADR-026) never counts a machine selection as the person's own choice.
- There is no master-resume upload and no resume parsing. ADR-037 settled that: the candidate
  writes their own facts, which is the only version in which the no-invention rule means
  anything, and a parser would reintroduce unconfirmed claims at the one point they are hardest
  to notice.

### Rejected

- **Letting a model write the document and validating the prose afterwards.** The validator would
  have to decide whether a sentence is supported, which is the judgement the model just made. The
  operations exist so that "which fact is this line?" is answerable by construction.
- **A free-text heading or summary field.** One line of unanchored prose is all an invention
  needs, and a summary is exactly where somebody would put "8 years of experience".
- **Exempting the first word of a bullet from the named-things check.** It is the position a new
  skill would occupy.
- **Mutable versions with an edit history.** The history *is* the versions. A row somebody can
  edit is a row an application can silently stop describing.
- **`reportlab` for the PDF.** A dependency tree to audit for the life of the project, to lay out
  something that is a list of lines.
- **Rendering the PDF in the request.** It is the one piece of this feature with a memory profile
  worth bounding, on a host with about 1 GB free beside Postgres and the worker.

## ADR-044 — A campaign sends from a pool, and a lead keeps the mailbox that first wrote to it

**Date:** 2026-11-03
**Status:** Accepted
**Relates to:** F7, slice S14.1; extends PR #67's cap reasoning and ADR-034

### Context

One campaign on one mailbox is capped at that mailbox's daily limit, and the limit exists to
protect a sending reputation. A workspace that wants to send more has to spread the work across
several addresses — which is what every tool in this category does, and what PR #67 already
settled half of: the cap belongs to the **mailbox**, because reputation attaches to the address
and not to how many campaigns somebody split the work into.

The other half is the one that goes wrong. Rotating addresses naively means a recipient gets the
first email from one person and the follow-up from another, which threads separately in their
client whatever `References` says, and reads as two strangers writing about one thing.

### Decision

**`campaigns.mailbox_id` stays the campaign's primary mailbox, and is always a member of its own
pool.** `campaign_mailboxes` holds the additional members, and a row for the primary is allowed —
that is how its weight becomes anything but 1.

**The rule is computed in exactly one function**, `services/mailboxes/pool.pool_for`, with its
inverse `campaign_ids_for` beside it because the send loop works mailbox by mailbox and therefore
asks the question the other way round. Both in one module, so a change to the rule is a change to
both. Each caller joining the table itself is this project's dominant defect waiting to happen:
the first condition added to the rule would reach one caller and not another, and the symptom
would be a campaign sending from a mailbox the screen says it does not use.

**Making `campaign_mailboxes` the sole authority was rejected.** It would need a backfill *and* a
change to every path that attaches a mailbox — `POST /campaigns`, `PATCH /campaigns/{id}`, and
whatever comes next — and the path that forgot would stop sending with nothing on screen saying
why. ADR-010 and ADR-017 both took the same line: the migration that quietly changes who can send
is the worst kind this project can ship. As it stands the migration is one empty table and one
nullable column, and a campaign created tomorrow by code that knows nothing about pools still
sends.

**`leads.mailbox_id` is the sticky sender, written by `claim_send` under the lead's row lock and
nowhere else.** That is the only point where the assignment and the outbox row that justifies it
land in one transaction, so two mailboxes that both believed an unassigned lead was theirs are
serialised and the second one's `why_not_sendable` refuses it. Deciding it where the lead is
*selected* would be the lazy-`Candidate.persona` mistake again — a rule evaluated where its
failure cannot be contained, which once crash-looped every mailbox in the deployment.

It is **never changed once set**. A send from a different member is refused by name rather than
silently redirected: the caller asked for a specific mailbox, and quietly using another is how a
cap is exceeded on a mailbox nobody was watching.

**Selection is by lowest weighted load, with no stored cursor.** Today's count per member comes
from `messages`, which is already the only source for the cap (§9.2) — so the rotation and the cap
can never tell different stories about how much a mailbox has sent, and there is no second number
to keep right. The member with the smallest `sent_today / weight` goes next; **ties break on the
lowest mailbox id**, because a tie broken at random would hand the same unassigned lead to two
mailboxes in one pass and the claim would have to refuse one of them for no reason.

**An unusable member is skipped, not fatal.** Paused, unconnected or missing, the pool sends from
whoever is left. Degrading to a smaller pool is the whole point of having one, and ADR-034 pauses
a mailbox that is bouncing badly — a pool that stopped because one member was paused would turn
that safety signal into an outage.

**A partly broken pool is reported without being called blocked.** The campaign's blockers gain
`pool_member_unavailable`, which names the affected addresses and says how many can still send;
`mailbox_unavailable` is kept for the case where none can. A campaign reporting itself blocked
because one of three mailboxes needs reconnecting would have somebody stop a campaign that is
sending perfectly well.

**Removing a mailbox from a pool leaves its leads on it**, and the response says so. Reassigning
them would do the exact thing the sticky sender exists to prevent, to the people furthest along in
a conversation. Removal stops a mailbox taking *new* leads; the threads it already owns finish on
it.

**The cap stays per mailbox.** A pool spreads a campaign's sends across several reputations; it
does not raise any one of them. Two mailboxes capped at 100 are not one campaign sending 200 from
addresses that each promised 100.

**Weights are bounded 1–100 by a check constraint built from the constants in `models.py`**, so
the constraint and the route that validates a weight cannot disagree about what a weight is — the
cure this project adopted after `reason="one-click unsubscribe"` made every opt-out a 500.

**Adding or reweighting a member is `admin`; reading the pool is not.** Who sends is a rule about
the workspace, the same line ADR-023 drew for the allowlist. Understanding a campaign is part of
reviewing for it.

### Consequences

- Every existing campaign has a pool of one and behaves exactly as it did, asserted by
  `TestNothingChangedForAPoolOfOne` rather than assumed.
- Every existing lead has `mailbox_id` NULL, which is the truth: those sends happened when the
  campaign had one mailbox, and `messages.mailbox_id` already records which address each message
  went from. A NULL lead takes the pool's choice on its next send, which with a pool of one is the
  mailbox it has always used. A test asserts a NULL lead is still sendable, because treating NULL
  as "not yet eligible" would have stopped an in-flight campaign at deploy.
- `ON DELETE SET NULL` rather than CASCADE: deleting a mailbox must not delete the leads it wrote
  to. They become unassigned and the pool picks again — the address they knew is gone either way,
  and the record of what was sent to whom must remain attributable.
- A claim that is then blocked by `SEND_MODE`, or fails at SMTP, still leaves the lead assigned.
  Correct: the claim committed a `sending` row, and ADR-002 is explicit that such a message may
  still have left.
- The rotation is evaluated once per campaign per send pass rather than per lead, which costs one
  count per pool member and is skipped entirely for a pool of one.
- A downgrade collapses every pool to its primary and loses the assignments, so a lead
  mid-conversation could have its next follow-up sent from a different address. That is a visible
  consequence for a recipient, and the migration says so.

### Rejected

- **Pooling the cap.** It is the one number that protects an address, and it is per address.
- **A stored round-robin cursor.** A second count to keep right, and §9.2's whole argument is that
  send counts are recomputed rather than stored. A crash between the send and the increment leaves
  a cursor wrong for ever and nothing notices.
- **Random selection among members.** Simpler, and it makes the share of a weighted pool a
  question nobody can answer for a given day, and it hands one lead to two mailboxes in one pass.
- **Reassigning a lead when its mailbox leaves the pool.** The failure the sticky sender exists to
  prevent, inflicted on the people furthest along.
- **Rotating on every message instead of per lead.** It is the thing that looks like a pool and is
  actually a broken thread.

## ADR-045 — A signed booking webhook is the second producer of `MEETING_BOOKED`, and the last

**Date:** 2026-11-03
**Status:** Accepted
**Relates to:** F15, slice S14.2; amends the locked product rule and the state machine

### Context

`MEETING_BOOKED` has had exactly one producer since Phase 6: a person clicking it. That is a
locked product rule and the reason for it is sound — the prototype inferred a meeting from a
model's reading of a reply (issue B4), and a system that does that inflates every funnel it ever
reports.

But the outcome this whole system exists to produce is somebody clicking a booking link and
taking a slot, and nothing told us when that happened. The operator had to notice a calendar
invite and go and click a button, which means the funnel was only ever as good as somebody's
diligence.

### Decision

**A booking is reported, not inferred, and that distinction is the whole ADR.** A third party says
a calendar slot was taken, signed with a secret this workspace generated and pasted into its own
booking tool. A reply is somebody's words being interpreted; a webhook is a fact about a calendar.
So the locked rule stands as written — the state is never inferred — and gains a second producer
that is not an inference.

**Two independent proofs, and neither alone is enough.**

1. **The URL names a workspace, provably.** `/hooks/booking/{token}` carries an HMAC over the org
   id under a key *derived* from `CREDENTIAL_KEY` — the same device as the one-click unsubscribe
   link (ADR-033), and for the same reason: a public route must never take an `org_id` from a
   caller (ADR-004). The token says whose secret to check with; it is not the authorisation.
2. **The body is signed with that workspace's own secret**, HMAC-SHA256 over the **raw bytes**,
   in a header. A URL-only scheme makes the URL the credential, and that URL ends up in a third
   party's settings page, their delivery logs and anybody's screenshot of either. A leaked URL
   alone books nothing.

**The raw bytes, never a re-serialised object.** Verifying against JSON we parsed and dumped again
is the classic way a correct-looking implementation accepts a forged body: key order, spacing and
number formatting all change under the round trip, so the only way to make it work is to be lax,
and lax is what the signature was for.

**Deliberately the scheme Cal.com already implements.** `X-Cal-Signature-256` *is* "HMAC-SHA256 of
the raw body with your secret", so the commonest tool needs no adapter — and nothing here is
Cal.com-specific. `X-Booking-Signature` is the generic alternative, an `sha256=` prefix is
accepted because refusing it would be a refusal nobody could debug from the sender's side, and a
provider-specific signature scheme (Calendly signs with *its* secret, not ours) is a translation
layer for later rather than a second trust path now.

**Replay is bounded by a signed timestamp; idempotency is a database constraint.** The timestamp
is part of the signed payload so it cannot be edited forward, and a delivery outside fifteen
minutes is refused. A delivery with **no** timestamp is still accepted, which is a stated
compromise rather than an oversight: Cal.com signs the body alone, so requiring one would mean
refusing the tool this is built for. What such a delivery loses is the replay bound, not the
secret. Retries — which every provider does, some even on a 2xx — are handled by
`uq_booking_events_org_provider_ext`, so a second delivery lands on the row it already wrote.

**Every refusal answers 204, having done nothing.** An unknown workspace, a bad signature, an
unreadable body: all the same. A public endpoint that distinguishes them is an oracle for all
three, and a provider reads any non-2xx as "retry", so answering 401 to a body we will never
accept buys a retry storm as well. The symptom an operator sees instead is the dashboard saying
no booking has ever arrived — which is the thing they can act on, and why `last_delivery_at`
exists: it is the cheapest way to tell "no meetings yet" from "this was never wired up".

**There is no `GET` on the public path**, and that is a safety property rather than an omission.
The unsubscribe endpoint has one because a human clicks that link. Nobody clicks a webhook URL,
and the things that *do* fetch URLs found in text — scanners, link previews, a provider's own
test button — would otherwise reach a write path.

**The state machine gained the edges it was always missing, and that is the most important thing
in this slice.** `MEETING_BOOKED` was reachable only from `REPLIED` and `IN_CONVERSATION`. So the
commonest success this product has — somebody clicks the link in the *first* email, books, and
never replies — could not be recorded at all. The webhook's own test found it: the booking was
stored, matched the right lead, and the lead sat in `SENT`. `SENT`, `FOLLOWUP_SENT` and
`NO_RESPONSE` now allow it; `NEW`, `APPROVED`, `OPTED_OUT` and `BOUNCED` do not, and it still
never reopens.

**And the test that should have caught it asserted the wrong thing.**
`test_meeting_booked_is_never_reached_from_a_machine_state` checked that `SENT -> MEETING_BOOKED`
was illegal, as a proxy for "only a human may set it". The real property is about **who** may
declare a meeting, not where the lead happened to be. It is rewritten to assert that, plus a new
test that no reply label's policy names the state — the half that was actually about the rule.
The same shape as `test_mcp_safety`'s "every tool is read-only", which was a proxy for "nothing
can send or approve" and had to break the moment anything wrote.

**The state machine is the only authority on where a lead may be.** `intake` had a
`BOOKABLE_FROM` set first; it was deleted before it was committed, because it was a second copy
of `_ALLOWED` and the first thing it did was disagree with it — claiming `NEW` was bookable. The
move is attempted and `IllegalTransition` is caught, which is also how the missing edge was found
rather than papered over.

**A cancellation is recorded and does not move the lead.** `MEETING_BOOKED` is terminal with no
exits by construction. A meeting that was booked *was* booked, the funnel counts an event that
happened, and widening a terminal state is a change to a locked product rule rather than a side
effect of adding a webhook. The cancellation lands on the booking row and as an event, so
somebody can see it.

**Every verified booking is stored, matched or not.** A booking from an address in no campaign is
the interesting case rather than the error case — the lead replied from a second address, or
forwarded the link — and dropping it would repeat the job-post intake's expensive lesson: an empty
result indistinguishable from nothing happening. `GET /bookings` exists mostly for those.

**It never creates a lead**, which would put somebody into a sending pipeline because they booked
a meeting — the opposite of what they asked for. And nothing in the package can send: a test
asserts it imports neither the outbox, the message builder nor the send loop.

**Matching is on the address, with the conversation breaking ties.** When the same person is in
several campaigns — the ordinary case, and the reason the per-person cool-down exists — the lead
we most recently *wrote to* wins. Taking the oldest or newest lead instead would attribute the
meeting to a campaign that never contacted them. `matched_by` records which rule applied, in
words, because a funnel somebody cannot audit is a funnel they will not trust.

**There is no deployment-wide booking secret**, for ADR-023's reason, and **no workspace gains a
webhook by being migrated**: an admin generates a secret, which is shown exactly once. A second
live secret per workspace is unexpressible by a partial unique index, so revoking means revoked.

### Consequences

- The public surface grows by one unauthenticated write, and that is now the complete list: the
  one-click unsubscribe and this.
- Lead erasure reaches a booking: the row survives (`ON DELETE SET NULL`), because erasing
  somebody must not erase the record that a meeting happened, and the invitee's address and name
  are cleared. Without that the erasure would be decorative for exactly the leads who got
  furthest.
- A cancellation after the fact leaves a lead in `MEETING_BOOKED`. Stated as a limit; changing it
  means deciding what a cancelled meeting's state *is*, which is a product question.
- A workspace whose booking tool signs with its own secret (Calendly) cannot use this yet. The
  shape of the fix is a per-provider verifier behind the same route, and the URL token already
  identifies the workspace it would need.
- `PUBLIC_API_URL` is required for the URL to be shown at all, exactly as ADR-033 requires it for
  the unsubscribe header. An unset base shows no URL rather than one built on a guess.

### Rejected

- **Accepting the URL alone as authorisation.** The URL is not a secret; it is pasted into a third
  party's settings page.
- **Verifying against re-serialised JSON.** The forgery-shaped mistake.
- **A provider allowlist.** Any tool that can sign a body with a secret you give it should work;
  an allowlist would be a list to maintain and a reason to refuse a tool that works.
- **Returning 401 or 400 on a refusal.** An oracle, and a retry storm.
- **Inferring a meeting from a positive reply.** The rule this ADR is careful not to weaken.
- **Letting a cancellation reopen a lead.** A separate decision, and not one a webhook gets to
  make.
- **Creating a lead for an unmatched booking.** It puts somebody into a sending pipeline for
  having asked to talk.

## ADR-046 — Search is an ordered chain of the workspace's own keys, and a cached answer costs nothing

**Date:** 2026-11-04
**Status:** Accepted
**Relates to:** F17, slice S14.3; extends ADR-041 (the user's own provider keys) and ADR-038
(what may be pooled across workspaces)

### Context

Research reads a company's own pages first, because that is better evidence and costs nothing,
and searches only when those pages said too little. The search half was built in Phase 3 behind
a `SearchProvider` interface with one implementation, Tavily, on a key in the environment.

**It had never run.** `research_lead` has accepted a `search` and a `budget` since Phase 3,
`_try_search` was correct and tested, `build_provider` and `SearchBudget` existed and were
tested — and `handle_research_lead`, the only caller in the system, passed neither. So `search`
was None on every real research job, and the entire fallback was exercised only by tests that
passed a provider in by hand.

That is this project's signature defect in its purest form, and the eleventh instance of it: a
correct producer, a correct consumer, and nothing joining them. The same shape as research never
queuing a draft, as nothing ever writing a `Draft(kind="followup")`, as the two `draft_reply`
payload shapes that matched nothing, and as every outbound message going out with no
`In-Reply-To`. Every one was invisible to a suite that exercised both halves separately, and
every one was found by looking at the join rather than at either side.

So F17 is two changes: wire the thing up, and make what gets wired up worth having.

### Decision

**A chain of providers, tried in order, stopping at the first that answers.** The shape is
deliberately `LLMRouter`'s, because the problem is the same one — several providers, each with a
free tier, any of which may be down or out of credit, tried in an order the operator chose.
Reusing that shape rather than inventing a second one means there is one answer in this system to
"what happens when a provider fails?"

**The order is the feature, and it has two edges.** A hit short-circuits, so the second member is
not called and therefore not billed; and a **cache hit calls nobody at all**. Those are the
slice's two acceptance criteria and they are the only two properties here that cost money when
they are wrong.

**Every key is the workspace's own**, read from `provider_credentials` exactly as F9's contact
lookups are. ADR-041's reasoning carries over unchanged: there is no shared key, so every
question about who may use one has the same answer, and a workspace with none degrades to the
company's own pages rather than failing.

**The deployment's `TAVILY_API_KEY` survives as the last member, and is named as shared.** This
is the one deliberate compromise. ADR-023 removed the deployment's mailbox precisely because an
identity living in the environment makes "who may use it?" a rule somebody has to maintain — and
every argument there applies here. It stays because removing it would stop the live deployment
searching at all, and it is contained three ways: it is always last, so a workspace's own key
shadows it; it is recorded in `search_usage` as `tavily:deployment` rather than `tavily`, so the
ledger can say which account a credit came out of; and the screen says in words that its free
tier is shared by every workspace using it. A workspace reading "search works" without being told
whose credits it spends has been told something misleading.

**A failure is not an empty answer**, and this is the change F17 forced in the provider interface.
With one provider the two were the same thing: `search()` returned `[]` on an outage, which was a
correct way of saying "degrade, do not fail the lead". With a chain they are opposite answers:

- **"Nobody writes about this company"** stops nothing — the next member is tried, because a
  provider that does not index a small Indian company is the ordinary case this exists for — and
  once every member has answered, the empty result **is cached**, so the next lead at that company
  does not pay to be told the same silence.
- **"This provider is down"** moves to the next member and is **never cached**. Storing it would
  let one bad afternoon stop a workspace searching for a week.

A provider that reports an outage as zero results makes those indistinguishable. That is the
job-post intake's expensive lesson in a more costly place: an unknown source with a plausible
payload returned zero postings and read as an empty board. So `search()` raises, and `SearchChain`
catches both kinds and degrades exactly as the single provider did.

**A refused key is told apart from an outage**, by two exception classes rather than one. A 401
means stop using this key until somebody fixes it, and the credential records that so the next
research job leaves the member out instead of paying a request to be refused again. A 429 or a
503 means try the next member and this one again tomorrow, and the key is untouched — being out
of credit is not being wrong. Collapsing the two is how a perfectly good key gets recorded as
dead because a provider had a bad afternoon.

**The cache is read before any ceiling is checked.** A cached answer costs nobody a credit, so a
workspace that has spent its month still gets the answer it has already paid for. Checking the
budget first would refuse to read a row we own.

**The budget is per provider, and the number is ours rather than theirs.** Each member is a
separate account with its own allowance, so a pooled total would refuse a workspace's second key
for credits its first key spent. We cannot read anybody's plan, so 1,000 a month is a ceiling
that stops a loop spending somebody's month in an afternoon — the same job `DAILY_LOOKUP_BUDGET`
does for contact lookups, and stated as that rather than as a claim about a free tier.

**The cache is per workspace.** ADR-038 made `company_profiles` the one table in this schema with
no `org_id` and drew the line precisely: a name, a careers URL and an ATS slug are public facts
worth pooling, while findings a draft goes on to cite were paid for by one workspace out of its
own credits and are not ours to pool. A search answer is on the second side of that line — it is
a provider's billable response to a question one workspace asked — so `search_cache` carries
`org_id` like `domain_research` and unlike `company_profiles`. One exception to ADR-004 is a
decision; two would be a pattern.

**Seven days, and the number is doing a different job from the research cache's ninety.** That one
governs findings verified against text we fetched. This one governs somebody else's index of what
a company has been up to lately, which is the most perishable thing in this system —
`PERISHABLE_AFTER_DAYS` already drops the hooks that cite it after thirty days, and a cache
serving month-old news would be feeding exactly the hook that embarrasses us first.

**Expired answers are deleted by the hourly sweep**, unlike `domain_research`, which is only ever
blanked. The asymmetry is the point: those rows hold findings a sent email cited and are part of
the record of what we said, while a search answer nobody may serve any more is a copy of somebody
else's index with no audit value at all.

**Two provider families, one table, disjoint names.** `provider_credentials` now holds contact
keys and search keys, because there should be one answer to "where are the secrets and how are
they protected?" and one key to rotate. The check constraint is built from
`ALL_PROVIDERS = CONTACT_PROVIDERS + SEARCH_PROVIDERS`, so adding a provider is one tuple and one
migration. But the constraint accepts either family's names, so it **cannot** be what stops a
Tavily key being handed to a contact adapter — which would post a search credential to somebody
else's endpoint. Each door therefore narrows to its own family explicitly, and the two name sets
are asserted disjoint.

**The verify button costs a credit and says so.** None of these providers offers an "is this key
valid?" endpoint, so verification is a real search. ADR-021's lesson is why it exists at all: a
key pasted with a trailing space, a green tick, and a fallback that quietly never runs is the
failure that took a whole slice to notice the first time. It runs one member rather than the
chain, because the question is whether *this* key works and a chain would answer it by falling
through to a different provider and reporting success.

### Consequences

- The search fallback runs for the first time. A deployment with `TAVILY_API_KEY` set will start
  spending credits on leads whose own pages said too little, which is what that key was for and
  has not happened before.
- A workspace with no search key gets a sentence saying so, with something to do about it, instead
  of silence.
- `SearchProvider.search` raising is a behaviour change for any future caller that treated `[]` as
  "could not search". There is one caller, and it is the chain.
- Reverting the migration **deletes** search keys. Revoking them would not do: the narrowed
  constraint looks at `provider` and not at `revoked_at`, so a revoked `tavily` row violates it
  exactly as a live one does. The downgrade says that in those words rather than hiding behind a
  tidier verb.
- A per-workspace chain *order* is not built. With at most three members the order that matters is
  "mine before the shared one", and that is the one implemented; a column and a screen for the
  rest is a later decision.
- Brave and Serper have **never been called**. Each response is read in exactly one function and
  `docs/SEARCH_PROVIDERS.md` marks which shapes were exercised and which were read off a page, in
  the spirit of `CONTACT_PROVIDERS.md`.

### Rejected

- **Pooling search answers across workspaces.** It is ADR-038's line, on the wrong side of it: a
  billable answer to one workspace's question, paid for out of its credits.
- **One pooled monthly budget across providers.** It refuses a workspace's second key for credits
  its first key spent.
- **Keeping `[]` as the failure signal.** It makes an outage indistinguishable from a company
  nobody writes about, which is the one distinction the cache depends on.
- **Caching an answer when every member failed.** One bad afternoon would stop a workspace
  searching for a week.
- **Marking a key invalid on a 429.** Being out of credit is not being wrong, and next month it
  works again.
- **A `mock` search provider.** `CONTACT_PROVIDERS` has one because the owner holds no paid
  contact key and the feature's first run would otherwise be against a real account. Search needs
  none: "no key" is already a supported, honest configuration here, and research degrades to the
  company's own pages — so a fourth name in the constraint would buy a demo and a thing to
  explain.
- **Letting the deployment key be first, or the only one.** It is one free tier shared by every
  workspace, with no row a route could fix when it breaks.

## ADR-047 — A job board's host is not a company's domain

**Date:** 2026-11-04
**Status:** Accepted
**Relates to:** F5 defect, slice S15.0; restates ADR-011 and gates F11's join key

### Context

`job_posts.company_domain` was filled with `urlsplit(apply_url).hostname`. For a posting the user
pasted from a company's own careers page that is right. For **every posting that arrived through
a feed it is the applicant tracking system's host** — `job-boards.greenhouse.io`,
`jobs.lever.co`, `jobs.ashbyhq.com`.

This was found while designing F11's watchlist, which needs to join a signal to a company and
would have joined on that column.

The evidence had been in the repository since F5 landed. `tests/fixtures/feeds/` holds three real
trimmed responses, and every `absolute_url`, `hostedUrl` and `jobUrl` in them is the board's. The
parser was tested against those fixtures and the storage was tested; nothing tested what the
stored value *meant*. That is the same gap as the platform router comparing
`direction == "outbound"` against a column holding `'out'` — a value that is wrong rather than
missing, in a column whose name says it is right.

**Three callers read it and all three acted on it.**

1. **A contact lookup searched the ATS vendor.** `lookup.run` took `post.company_domain`, so
   asking who is hiring at a company spent one of the user's **own provider credits** looking for
   people who work at Greenhouse. F9's whole design is about not billing somebody for work they
   did not ask for, and this billed them for work nobody could want.
2. **An imported contact became a lead whose `company_url` was the vendor**, which the research
   pipeline then fetched, synthesised and cited in a draft. And `cache_key()` reduces that host to
   `greenhouse.io`, so **every feed-sourced lead in a workspace shared one `domain_research`
   row** and the second was served the first's findings. That is ADR-011 exactly — the defect it
   was written to fix, arriving through a door that did not exist when it was written.
3. **The referral matcher looked for connections at "boards"**, taking the host's first label, so
   it matched anybody whose company name contains that word and proposed asking them for an
   introduction.

### Decision

**The vendor host is never stored as a company's domain. The column is NULL instead.** NULL makes
a caller ask; `job-boards.greenhouse.io` makes three callers confident and wrong. One empty column
is a feature request, and one wrong column is three bugs that each spend something.

**The slug is stored, because that is what a feed actually tells us about the employer.**
`vercel`, `leverdemo`, `ramp` — the first path segment of the apply URL, which is the company's
own identifier on that board and is already the key `watched_boards` uses. Read from the three
real fixtures rather than from documentation.

**One function decides what a company domain is**, `boards.company_domain_of`, and
`intake.domain_of` now delegates to it. Three callers spending three different things on this
answer is exactly the shape that produces three copies of a rule and then three different bugs.

**One function answers which company a posting is for**, `company.resolve`, in three steps and
none of them networked: the posting's own domain; then `company_profiles` by `(ats_type,
ats_slug)`, which is F19's record of exactly that mapping and is a fact some workspace
established from a careers page; then nothing, with the slug as a readable label.

**It resolves or it refuses. There is no guess from the slug.** `vercel` is not `vercel.com`
reliably, `ramp` is not `ramp.com` for every Ramp, and a guess here is a credit spent on the wrong
company and an email written about them. Hard rule 6 forbids inventing facts about the sender;
this is the same rule pointed at the recipient, and the contact import already refuses to derive a
trading name from a domain for the same reason.

**`CompanyRef` keeps `domain` and `label` as separate fields**, so a caller cannot use the
readable one as the domain by accident. `label` is always something a screen can print; `domain`
is None whenever we do not know it.

**Each of the three callers now refuses with a sentence rather than proceeding.** The lookup names
the board and says to type the company's domain; the import refuses a candidate recorded against a
board and says that importing it would research the board; the referral matcher falls back to the
slug, which is a fact the feed gave us, instead of to the host's first label, which we
manufactured.

**The lookup also refuses a board domain typed by hand.** That path is reachable by pasting the
posting's own link into the box, and it is the one that would otherwise succeed and bill for it.

**`BOARD_HOSTS` is deliberately longer than the three feeds we poll.** The question it answers is
not "can we poll this?" but "would storing this host as a company's domain be a lie?" — and for a
pasted Naukri, LinkedIn or Workday posting it would be. Nothing fetches those; the list exists so
the column stays honest whatever a user pastes.

**The migration rewrites rows**, which no migration in this project has done before. These values
are not stale, they are wrong, and every use of one spends money or says something untrue. Leaving
them would mean the fix applied to postings fetched after the deploy and not to those already
stored, so one screen would behave two ways depending on when a row arrived.

**The backfill's host list is narrower than the application's**, covering only hosts the old code
could have written. Matching the full list in SQL would risk blanking a domain that is genuinely a
company's, for a vendor nothing ever stored.

**The backfill imports `NOT_A_SLUG` from `boards`** rather than restating it. The backfill is SQL
and the parser is Python applied to the same strings; two copies would be this project's signature
defect with its halves in two languages. The SQL expression was run against the real URL shapes on
the development database and produces the same answers as the parser.

**`contact_candidates.company_domain` is not rewritten.** Those rows are proposals a person may
have read already, and some came from a domain somebody typed rather than from a posting, so
nothing in SQL can tell a wrong one from a right one. The import route refuses a board host
instead, which fails loudly at the one moment it matters.

### Consequences

- A feed posting now has no company domain until something resolves one. The contact lookup and
  the contact import say so rather than guessing, which is a visible refusal where there used to
  be a silent wrong answer.
- `company_profiles` becomes load-bearing for the job-hunt side: it is the only thing that turns a
  board slug into a domain. A workspace that has never researched a company will be asked for the
  domain once.
- The downgrade does **not** restore the vendor hosts. That is a deliberate one-way door: putting
  them back re-arms a lookup that bills for contacts at Greenhouse and a research run that drafts
  an email about a job board.
- The test harness gained `company_profiles` and `platform_operators` in its truncate list. Both
  are deliberate exceptions to ADR-004 and therefore have no foreign key for
  `TRUNCATE app.orgs CASCADE` to follow, so both survived every test and leaked into the next.
  Found by a test writing one shared company row twice.

### Rejected

- **Deriving a domain from the slug.** `<slug>.com` is wrong often enough to spend a credit on
  the wrong company and draft an email about them.
- **Keeping the host and filtering at each caller.** Three callers, three copies of the rule, and
  the fourth caller added later would not have it.
- **Fetching the board page to find the company's site.** A network call on a read path, against
  a board whose terms we have not checked, to recover a fact `company_profiles` already records
  for any company somebody has researched.
- **Leaving the stored rows alone.** The same screen would behave two ways depending on when a
  posting arrived.
- **Restoring the hosts on downgrade.** It re-arms two paths that spend money and invent facts.

## ADR-048 — A feature with no screen is not shipped, and a guard says so

**Date:** 2026-11-04
**Status:** Accepted
**Relates to:** F4, F5, F6, F7, F9, F10, F15, F17, F20; restates Phase 8's lesson

### Context

The owner said they could not see any of what had been built. The count is the finding: the API
served **90 paths and the dashboard called about 30**.

Everything in Phases 12 and 13 had no screen — job postings and the daily board poll (F5), the
candidate's confirmed facts and answer bank (F6), hiring-contact lookups on the user's own key
(F9), referrals from their LinkedIn export (F10), tailored resumes and the application tracker
(F20) — and so did F4's deliverability report, F7's mailbox pool, F15's booking webhook and F17's
search keys. Every one of them was reachable only with curl.

**This is exactly Phase 8's failure at ten times the size**, and these notes already record it:
the deployed dashboard signed in, said "No campaigns yet", and offered no way to make one, so
import, research, drafting and sending were all gated behind a record that could not exist. The
sentence written then was *the system was deployed and unusable, and only looking at it would
have shown that.* Eleven slices later nobody looked.

Three of the invisible features were not inconvenient but **inert**:

- A booking webhook whose URL and secret cannot be read cannot be pasted into a booking tool, so
  it receives nothing, and the symptom is a funnel reporting no meetings — indistinguishable from
  nobody booking one.
- A tailored resume that cannot be read cannot be reviewed, and ADR-043 rests on a person reading
  it: the validator catches an invented number or skill and cannot catch a lower-case
  exaggeration.
- A dead job with no requeue button is one somebody fixes with SQL against production, which has
  already happened here three times in one deploy.

### Decision

**Three screens, built around the product rules rather than around the endpoints.** `JobHunt.jsx`
(postings, facts, resume and applications, referrals, contacts), `Sending.jsx` (pool,
deliverability, booking webhook, search keys) and `Operations.jsx` (worker, alerts, dead jobs,
model usage).

**What a screen may not offer is the design.** Nothing on the job-hunt screen contacts anybody: a
referral note is text to copy, a contact lookup writes a proposal, and marking an application
applied records what the person did. A fact stays a proposal until they confirm it. A lookup
spends their own credit, so it is a button press and never a load, a refresh or a tab change —
the rule that keeps `GET /mailboxes` non-networked, which matters more here because a refresh
would spend money. The applied-to-planned move is not offered at all, because the API refuses it
and a button that always fails is worse than no button.

**The deployment's shared search key is labelled as shared, with its usage beside it.** A
workspace told "search works" without being told whose credits it spends has been told something
misleading.

**A guard, because a count that nobody measures goes back to 30.**
`tests/unit/test_dashboard_calls_real_routes.py` reads every call site in `frontend/src/*.jsx`,
normalises the template holes, and compares them against the live OpenAPI paths. It cannot insist
a feature has a screen — that is a judgement — but it can insist the screens we have ask for
something real, which is the half that fails silently. The alternative, a browser test, is a
dependency tree to audit for the life of the project to assert a URL string, and that bargain was
already made for `test_connect_sends_mailbox_id.py`.

**`test_dashboard_state_is_declared` is widened to every component file.** It read `App.jsx` only,
so neither new screen was checked — and it is the guard written *after* a button shipped throwing
`ReferenceError`. A guard scoped to one file stops applying the moment somebody splits a component
out, which is precisely when a new identifier gets typed.

**Four classes added to `app.css` from the existing tokens**, not a second vocabulary. I had
written `panel`, `rows`, `row`, `row-actions`, `stack`, `field-label`, `field-hint` and `small`,
none of which exist — so those elements would have rendered unstyled with a green build. Tabs
genuinely did not exist, so `.tabs`, `.tab`, `.list` and `.list-row` are new; everything else uses
`card`, `btn-row`, `form-label` and `hint`.

**`fetchBlob` and `saveAs` are new in `api.js`**, because the resume PDF cannot go through
`request`, which insists on a JSON content type — and an `<a href>` cannot fetch it either: the
route is authenticated and a plain link arrives with no `Authorization` header and is answered
401. That is the shape of bug this file exists to prevent, so it is fixed in this file.

### Consequences

- Route coverage goes from 30 of 90 to 82 of 84. The two remaining duplicate routes that do have
  a screen.
- A new route with no caller is now a visible gap rather than an invisible one, since the guard
  prints the paths a screen asks for and the OpenAPI set is one call away.
- **These screens have not been rendered in a browser against a live API**, which needs Supabase
  auth and a running backend. Vite does not resolve identifiers and the frontend suite renders
  nothing — the two structural guards are a substitute for looking, and a substitute is what they
  are. The first operator to open them should expect to find something.
- `App.jsx` stays large but stops growing: three screens went into their own files, and the state
  guard now covers all of them.

### Rejected

- **A browser test.** It proves the most and costs a browser download in CI plus a dependency
  tree, to assert that a fetch went to the right URL. Same bargain as F8's scanner, taken the
  same way.
- **Waiting for a UI slice in the plan.** There is no such slice, which is how this happened.
  Eleven slices of backend each ended with a green CI run and nothing a person could open.
- **One screen per endpoint.** The endpoints are not the product; `JobHunt` is one subject with
  five tabs because that is how somebody uses it.
- **Putting the operational panels on the existing Admin screen.** Admin answers "who may do
  what in this workspace"; a dead job and a worker heartbeat are a different question, and
  merging them would make the one screen somebody opens in an incident the longest one.

## ADR-049 — A signal proposes a reason to write, and never a person to write to

**Date:** 2026-11-05
**Status:** Accepted
**Relates to:** F11, slice S15.1; built on ADR-047's company key and ADR-041's proposal shape

### Context

Cold email works when it has a reason to be sent today. A company that has just opened three
engineering roles is a company with a budget and a problem, and writing to them that week is a
different proposition from writing to them at random.

The system already fetches exactly that: the daily ATS poll (F5) reads public boards for the
job-hunt side and stores the postings. Nothing read them for the outreach side.

### Decision

**A watchlist entry is a company; `watched_boards` is a board.** One fetch serves both: a
candidate looking for a job and an agency looking for a client want the same HTTP response for
different reasons. Adding a watch makes sure the board is polled and does not add a second poll
— `_ensure_board` looks before it inserts, because `uq_watched_boards_org_source_slug` would
otherwise raise an integrity error from a path nobody expects one from.

**The key is the registrable domain**, the key `domain_research` uses, for ADR-011's reason: it
is the company's own identity, where `leads.domain` is whatever address a founder writes from
and is routinely `gmail.com`. A URL typed into the box is reduced to it, because people paste a
careers page as often as a domain and both name one company.

**The board slug is stored on the watch rather than resolved each time.** A feed posting carries
the board's slug and **no company domain at all** (ADR-047), so matching a posting to a watch
through `company_profiles` would mean a watch only works once some workspace had researched that
company. Storing it makes the match direct, and lets somebody say "watch Acme's Lever board" in
one step. The domain stays the identity; the slug is how the signal arrives. **This slice is the
reason ADR-047 was worth stopping for** — the join key it fixed is this join key.

**A signal proposes, and never creates a lead.** This is the rule the table exists under, and it
is `contact_candidates`' rule: the send loop reads `leads`, and nothing here is in that table. A
company hiring is a reason to write to somebody there — it is not consent, not an address and
not a person. A signal that created a lead would put somebody into a sending pipeline because
their employer posted a job.

**And nothing here drafts or sends.** `approval_mode = review_all` already means a person writes
and approves every first email, and a signal does not get to be an exception. ADR-034's rule
applies in spirit: a thing that acts on its own may only ever *restrict*, and this one only ever
adds a row to a list somebody reads. `tests/unit/test_signals_only_propose.py` asserts that over
the package and its router — no send import, no `save_draft`, no `Lead(...)`, no `transition` —
with comments and string constants stripped first, because the prose explaining each rule names
every symbol it forbids.

**A lead is matched, never inferred.** A signal attaches to a lead the workspace already has at
that domain, resolved through the same `cache_key`. No fuzzy name matching: the cost of a wrong
match is a reason-to-write attached to the wrong person, and the person sending the email would
have no way to see it was wrong. The SQL narrows with a `LIKE` and Python decides, so the query
does not load every lead and the `LIKE` is not trusted to be right.

**Terminal leads are excluded from matching**, which is the decision inside that function. A
person who opted out or bounced is not somebody a new reason to write makes approachable again;
attaching a signal to them would put a suggestion on screen beside somebody we have been told to
leave alone. For those companies, "no lead here yet" is the honest answer.

**A signal with no lead is the interesting case, not the error case.** It is a company worth
approaching that this workspace has no contact at, which is exactly what F9's lookup button is
for — and the list reports the count, because that number is the reason to open the screen.

**The producer ships in this slice.** `from_postings` turns a watched company's postings into
hiring signals, in the same transaction as the postings that justify them. A table with no
writer is how search came to be wired to nothing for three phases and how a
`Draft(kind="followup")` was never once created; `signal_events` therefore ships with the one
producer the system can already feed it rather than waiting for S15.5's richer poll.

**Updated postings count, not only new ones.** The first poll after a watch is a poll of rows
already stored, so a producer that only looked at newly-created postings would produce nothing
until that company posted something new — which for a quiet company is never. The signal's own
unique key is what stops that becoming duplicates.

**`(org_id, kind, source, external_id)` is unique**, the booking webhook's device for the same
reason: the daily poll sees yesterday's postings again every time it runs, so without it every
morning would add a fresh copy of every open role and the list would be unusable inside a week.

**Forgetting a company keeps its signals.** `signal_events.watchlist_id` is `ON DELETE SET NULL`
and each signal carries its own `domain`, so deleting the record that a company was hiring in
March cannot quietly rewrite a funnel. The board stays in the poll too: it may be there for job
hunting, and a watch is not its owner.

**There is no way back to `new`.** "I have looked at this" is not something somebody un-does,
and a list that can be reset is one nobody trusts to shrink.

**The screen ships with the tables**, which is the standing rule from here on after eleven
slices shipped with none (ADR-048).

### Consequences

- A workspace that watches a company with a board gets its new roles once a day, as a list. No
  workspace gets anything by being migrated.
- `signal_events` has one producer and three kinds with none. `news`, `launch` and `funding` are
  in the check constraint because a vocabulary belongs there and a producer added later should
  not need a migration — stated, as `mailbox_health.complaint_rate` states its own absence.
- The poll's behaviour for the job-hunt side is unchanged, and a test asserts the postings it
  stores are identical whether or not anybody is watching.
- `IntakeResult` now carries the rows it touched. The alternative was querying them back by
  their keys, which would be a second place deciding what "the postings from this poll" means.

### Rejected

- **Creating a lead from a signal.** It puts somebody into a sending pipeline because their
  employer posted a job.
- **Drafting from a signal.** `review_all` means a person writes the first email; an automatic
  draft queued by a background poll is the one thing this system has never done.
- **Fuzzy company-name matching.** A reason-to-write attached to the wrong person, invisibly.
- **Resolving the slug through `company_profiles` instead of storing it.** A watch would work
  only for companies somebody had already researched, which is the opposite of the case it is
  for.
- **A second poll for watched companies.** One fetch, two readers. A second poll would double
  every board's traffic and give two answers about what a company has posted.
- **Letting a signal move a lead's state.** Where a lead stands is decided by what was sent and
  what came back, and `domain/state.transition` is the one writer of it.

## ADR-050 — An expired grant is a reason to be unavailable, and one row may not contradict itself

**Date:** 2026-11-05
**Status:** Accepted
**Relates to:** ADR-018 (the unpublished client), ADR-021 (grant status); corrects both

### Context

Reported by the owner: the alerts said the Gmail connection had expired, and the campaign screen
and the sending-account panel both still said **connected**.

Both were right about what they were computing, which is why this survived. `invalid_reason` is
set only once a provider has *refused* a credential. A grant that lapsed on Friday and has not
been used since carries no reason at all — so `unavailable_reason` returned None, and every
reader of it takes None to mean "this mailbox works". Meanwhile `grant_status` separately knew it
had nothing left, and the hourly sweep was raising `mailbox_grant_expiring` about the same row.

Two correct functions answering adjacent questions, with the screens asking the wrong one. The
same shape as the pool blocker that reported nothing for a *paused* mailbox —
`unavailable_reason` answers whether a credential can log in — and as the platform router
counting `direction == "outbound"` against a column holding `'out'`.

**`GET /mailboxes` made it worse by disagreeing with itself in one row**: `connected: true`,
`usable: true`, `days_left: 0`, `expiry_notice: "This Gmail connection has expired."` Four
fields, two questions, and whoever reads a row like that believes the reassuring one.

### Decision

**`unavailable_reason` consults `grant_status`.** It is the one function the campaign blockers,
the mailbox list and the send loop's pause reason all ask, precisely so they cannot disagree —
and the fix therefore lands in all three at once, which is the whole argument for having it.

**Reported from the arithmetic, not from Google.** `login_for` still asks Google, because Google
is the authority on whether a token works. But "should this person be told to reconnect?" is
answerable without a network call, and the honest answer the moment a seven-day grant is seven
days old is yes. A read path that minted a token to find out is the side effect ADR-021 refused.

**It reuses `GrantStatus.notice`** — the sentence the dashboard and the alert already use —
because a person who sees three wordings for one state assumes three problems. That is
`_not_connected`'s stated reason, applied to the state next to it.

**`connected` and `needs_reconnect` are two views of one answer**, derived from a single call
rather than computed separately. A row cannot contradict itself when there is only one thing
being asked, and the test asserts the property across four grant ages rather than the single
instance that was reported.

**And `connection_status` no longer returns `invalid_reason`.** It carried the column verbatim,
which can quote a provider's own reply into a browser, a screenshot and anything that logs a
page. That rule is kept everywhere else in this system — `GET /mailboxes` carries a boolean and
has a test asserting the stored text is absent — and this route was the one place still echoing
it. Found while reading the function, not reported.

### Consequences

- A workspace whose grant has lapsed now sees Reconnect everywhere instead of "connected"
  everywhere, and its campaigns report a blocker rather than silently sending nothing.
- The reported state is now **more conservative than reality** for a few hours either side of
  expiry: `days_left` rounds up, so a connection with eleven hours left is not expired, but a
  clock skew or a wrong `granted_at` could ask somebody to reconnect a connection that still
  works. Reconnecting costs one click and the opposite error costs a silent outage, so that is
  the right direction to be wrong in.
- `GOOGLE_GRANT_DAYS = 0` still turns all of it off, which is what publishing the OAuth client
  means. A test asserts that, so a published client cannot inherit a refusal we invented.
- An app password is unaffected: only an unpublished client's refresh token lapses.

### Rejected

- **Refusing inside `login_for` on the same arithmetic.** Google is the authority on whether a
  token works, and refusing locally would turn our own clock into a reason a working mailbox
  stopped.
- **A separate `expired` flag on the row for the screens to combine.** That is the defect: three
  fields the reader has to reconcile, and a reader who gets it wrong believes the reassuring one.
- **Leaving `connected` alone and fixing only `usable`.** The campaign panel reads `connected`,
  which is where it was reported from.

## ADR-051 — An edge with no source is refused by the database

**Date:** 2026-11-05
**Status:** Accepted
**Relates to:** F12, slice S15.2; feeds S15.3's brief

### Context

A meeting brief is a set of sentences somebody repeats to a stranger. The prototype's failure
mode was confident paragraphs about funding stage and traction for a company nobody had looked
at (issues A1, A2), and hard rule 6 exists because of it. The graph those sentences will be
built from is the place to make that impossible rather than discouraged.

### Decision

**`ck_edges_provenance` requires a `source_url` or a `message_id`.** "We know Acme is hiring"
cannot be stored without the thing that says so.

**A check constraint rather than a rule in a service**, and that is the whole slice. A service
can be bypassed by the next writer, and the next writer is always somebody in a hurry with a
reasonable-sounding exception — "just this one, we'll add the source later". A constraint has no
sympathy. The test for it writes raw SQL, because testing it through the service would only
prove the service is careful today.

**The service refuses first anyway.** `Claim.__post_init__` raises if provenance is missing, so
a caller that forgets is told which claim it was rather than handed an `IntegrityError` three
frames away from a worker pass that was storing postings at the time. The constraint is the last
line of defence, not the first.

**`object_id` or `object_text`, never both and never neither.** A row that did both would be two
claims wearing one id, and a brief citing it could not say which it meant.

**A model may phrase an edge and may never add one.** Nothing writes here from a model's output:
the producers are a job posting, whose apply URL is the source, and an inbound message, whose id
is. S15.3's brief reads these rows and may reword them; it cannot introduce a claim, because
there would be nothing to put in the provenance columns. That is a stronger guarantee than
asking a model not to — it is the schema making the sentence unsayable.

**The source travels with every line, not beside the set.** `about()` returns each statement
with its own source, which is what makes the rule checkable from outside: a sentence that
arrives without one did not come from this table.

**`uq_edges_claim` is `NULLS NOT DISTINCT`, and that detail is load-bearing.** Three of its seven
columns are null in any given row — an edge names an object *or* a text, and carries a URL *or* a
message id — and under Postgres's default rule two NULLs never collide, so the index would have
deduplicated **nothing at all**. The daily poll sees yesterday's postings again every morning,
so an index silently not working means a duplicate claim a day until a brief cites the same
sentence six times. Postgres 15+; this project runs 17 in development and in production, checked
rather than assumed.

**Entities are per workspace**, like `domain_research` and unlike `company_profiles` (ADR-038).
A company's name and ATS slug are public facts worth pooling; what one workspace has worked out
about a company, from its own sources, is not.

**`entities.lead_id` is `SET NULL` and `edges.message_id` is `CASCADE`**, which looks
inconsistent and is the point. Erasing a lead must not delete what we learned about their
*employer*; but an edge whose only provenance is a message must go with that message, because a
claim that outlived its source is exactly the claim this table refuses to store.

**One normaliser for "what company is this?".** Four things key on it — the research cache, the
watchlist, the graph and the signal producer — and they were about to be three implementations:
the watchlist grew its own, and the graph then handed a whole URL to `registrable_domain`, which
wants a host and quietly returned something that was not a domain, so `https://www.acme.example/x`
and `acme.example` became two nodes for one company. Found by a test, fixed by moving the rule to
`net.company_domain` where `registrable_domain` already lived.

### Consequences

- The graph can only ever contain claims with sources, so a brief built from it can cite every
  line or has no line to cite.
- The vocabulary is deliberately small: four entity kinds and four predicates. A kind nobody can
  cite and a predicate nobody can phrase make the graph bigger without making it more useful,
  and both are check constraints built from tuples in `models.py`.
- `uses` and `works_at` have no producer yet. Stated, as `signal_events`' unused kinds are and
  as `mailbox_health.complaint_rate` is: a vocabulary belongs in the constraint, and a producer
  added later should not need a migration.
- Reading a company's graph is one query and no network call, so the screen showing it costs
  nothing to refresh.

### Rejected

- **Provenance as a service-level rule.** The next writer bypasses it, and the failure is a
  sentence somebody repeats to a stranger.
- **A nullable `source` with a "trusted" flag.** Every claim would eventually be trusted.
- **Letting a model write edges.** The guarantee here is that it cannot, which is worth more than
  any prompt.
- **Pooling entities across workspaces.** ADR-038's line: public facts about a company are
  shared, one workspace's conclusions are not.
- **A `confidence` column.** It would be a number nobody could justify, and a sentence either has
  a source or does not.

## ADR-052 — A brief line carries its own source, and a model may only reword it

**Date:** 2026-11-07
**Status:** Accepted
**Relates to:** F12, slice S15.3; builds on ADR-051; reuses ADR-043's rewording check

### Context

A brief is the set of sentences somebody repeats to a stranger on a call. The prototype's worst
output was a confident paragraph about funding stage and traction for a company nobody had
looked at (issues A1, A2), and hard rule 6 exists because of it. ADR-051 made the storage layer
refuse a claim with no source; this is the layer that reads those rows back and hands them to a
person — and, if asked, to a model.

The brief that existed was an MCP tool that concatenated the lead summary and the research
summary. Useful, and it had no structure, no sections, and no way to tell a line that came from
a page we read from a line that came from a model's sense of what companies are like.

### Decision

**Five sections, fixed as a tuple**: who they are, what they are doing, what they use, what we
found, the conversation so far. A section a model could invent is a section with nothing to put
in it — the same device as `ProposedEdits` having no field for a heading (ADR-043) and
`propose_reply` having no `subject` (ADR-030). `Line.__post_init__` refuses a section outside
the five, so the vocabulary is enforced where the line is built rather than where it is drawn.

**Every line carries a `source_url` or a `message_id`, and `Line.__post_init__` refuses one with
neither.** `ck_edges_provenance` one layer down; the same reasoning, in the reader.

**The brief is built from the three things that carry a source per item** — graph edges,
research hooks (already verified against the page they were read from), and messages.

**`domain_research.findings` is deliberately excluded, and that is the decision in this ADR
somebody will want to reverse.** `what_they_do` is good prose and it is a synthesis of several
pages, so no single page says it. A line citing "one of these four URLs" is a line whose source
cannot be checked, and somebody would repeat it. So the synthesis is reported in `gaps` as
something we hold and cannot cite, with the reader sent to the research panel. The rule being
protected is not "cite something" but "a line and the page that says it".

**The header is not a claim.** Who the lead is comes from the operator's own spreadsheet, and it
says so. A brief is addressed *about* somebody; naming them is not a statement about the world
that needs a footnote. Putting it in a section would have forced a fake citation for the one
line that genuinely does not need one, which is how a citation becomes decorative.

**The fifth thing a brief owes its reader is what it does not know**, so `gaps` names each empty
section, a missing company domain, thin research, and the synthesis above. A brief with four
thin sections invites whoever reads it to fill in the rest from memory; a model asked to "write
a brief" fills it in from nothing. Naming the hole is the only honest version, and it doubles as
the list of which button to press next.

**A model may phrase a line and may never add one.** The contract is `{line_id: text}` in and
`{line_id: text}` out:

- It cannot add a line, because an invented claim has no `line_id` and a made-up id matches
  nothing. The attempt is reported rather than silently dropped — a model returning an id it was
  never given is a model trying to add a claim, and that is worth seeing.
- It cannot change what a line says, because `domain.wording.invented` refuses a number or a
  named thing the original does not contain.
- It cannot drop a line, because a line it does not mention keeps its own wording. Silence is
  not deletion: omitting the one awkward line would be the quietest way to mislead.
- It never sees a source, so it cannot move one. `replace(line, text=...)` changes the words and
  nothing else.

**The check is `domain.wording`, extracted rather than copied.** ADR-043 wrote it to stop a model
inventing a skill while rewording a resume fact; this is the same question about the same kind of
edit. Two copies would be the signature defect in the place where a wrong answer is a sentence
somebody says out loud. `resume/validate.py` now delegates to it.

**A model failure is not a brief failure.** The deterministic text is already correct, so an
outage, an exhausted chain or an unparseable answer leaves the brief as it was with a note. The
opposite trade from drafting, where a failed model means no email: here the work is already done
and the model is polish.

**The route that reads is free; the one that spends is a press.** `GET /leads/{id}/brief` calls
no model, so a refresh cannot spend anybody's token budget — the rule that keeps `GET /mailboxes`
from minting an OAuth token and a contact lookup from spending a credit on a page load.
`POST /leads/{id}/brief/phrasing` is the button, and a test asserts the GET writes no `llm_usage`
row.

**The MCP tool reads the same builder.** A brief assembled twice is two readers of one table
that will eventually disagree — and here they would disagree about what we can *prove*, which is
the one thing a brief is for.

### Consequences

- A brief can be audited line by line by whoever is about to make the call.
- It will often be short, and the gaps section says why. That is the intended behaviour: a thin
  brief for a company nobody has researched is the truth about that company.
- `phrase_brief` joins the task registry on the **fast** tier with `has_pii=True`: a line quotes
  a message subject and names a company, so it must not reach an `allow_pii=false` provider.
- A rewording must begin with a word the original already uses, because position in the sentence
  is not exempted (ADR-043's reasoning, unchanged). A real constraint in the harmless direction.
- What the check cannot catch is a lower-case exaggeration, exactly as on a resume. Stated here
  rather than discovered later; what makes it acceptable is that a person reads the brief, and
  that the source is beside every line so they can check the one that matters.

### Rejected

- **Letting a model write the brief from the rows.** It would produce a better-reading document
  and there would be no way to tell which sentences were in the rows.
- **Citing a synthesis to its first source.** The citation would be checkable and wrong, which is
  worse than absent.
- **A footnote block at the end.** A list of sources after the text is exactly how a line gets
  separated from the thing that justified it.
- **A confidence score per line.** ADR-051 rejected it for edges for the same reason: a line
  either has a source or does not.
- **Phrasing on load.** A read that spends is the pattern this project refuses everywhere else.

## ADR-053 — An arm is stored on the lead, and every arm has a floor under it

**Date:** 2026-11-07
**Status:** Accepted
**Relates to:** F13, slice S15.4; supersedes the assignment half of ADR-026

### Context

`messages.variant` and `messages.warmup_day` have existed since ADR-026 and **nothing has ever
written either of them**. `experiments` has existed just as long with no producer at all, so it
holds no rows. The export script reads all three and has therefore been exporting NULLs. That is
this project's signature shape once more — a correct table, a correct reader, and nothing in
between — and it is the twelfth instance recorded here.

### Decision

**The arm is stored on the lead (`leads.variant`), written once when its first draft is written,
and never recomputed.**

**ADR-026 specified stickiness as a deterministic hash of (lead, experiment), and building it
showed that cannot work.** A hash has to be taken modulo the number of arms, so adding a third
arm to a running experiment moves most leads already in flight from one arm to the other —
silently, retroactively, and after their first email has gone out. Every message already sent
would then be attributed to an arm its draft did not come from, and the funnel would look fine.
One stored fact beats a derivation that can change under rows that already exist. There is an
integration test named after exactly that scenario.

**Assignment happens at drafting time**, because that is the only moment the arm can still
affect the email. Deciding it at send time would record a label on a draft the arm had no hand
in. It is idempotent per lead, so a regeneration, a follow-up and a reply all get the same
answer — an A first email followed by a B follow-up measures neither, and the recipient is the
one person who can tell. The same reasoning as the sticky sending mailbox (ADR-044).

**Allocation is random, not round-robin.** Round-robin looks fairer and correlates the arm with
arrival order: a sheet sorted by company size, or an import done in two halves on two days, puts
systematically different people in each arm, and the difference then shows up in the results as
though the *email* caused it. The rule is in `app/domain/experiment.py` with an injected
`Random`, so the distribution is asserted with a seed rather than hoped for.

**A fixed exploration share is always held back, and it has a floor in the database.** Weights
exist so an operator can send most of the volume through the arm they believe in. A weighted
allocator with nothing held back can drive an arm to zero — and that is exactly when it stops
producing the data that would show the belief was wrong. So `EXPLORATION_SHARE` of assignments
ignore the weights and pick uniformly, which floors every arm at `share / len(arms)`, and
`ck_experiments_exploration_floor` refuses a row below 0.1. A floor, not a correction: nothing
here learns from outcomes, and F16 is the slice that would.

**At most one active experiment per campaign**, by a partial unique index rather than by the
route remembering. Two would both allocate, the second assignment would overwrite the first, and
every message would carry whichever ran last.

**An arm may vary exactly one thing: a style instruction handed to the drafter.** It cannot
widen what may be said — the draft goes through the same `validate_draft` and the same claim
check as every other draft, so an arm asking for a statistic the evidence does not support fails
validation rather than sending one. That is hard rule 6 holding whatever an operator types into
a form. The instruction is placed **before** the rejection feedback in the prompt, because a
reviewer's objection about this specific text is more recent and more concrete and must be the
last thing a model reads.

**Stopping is `active = false`, never a delete.** The arms are what the labels on already-sent
messages *mean*; deleting them would leave a column of letters nobody can interpret. Replacing
an experiment stops the previous one for the same reason rather than editing its arms in place,
which would retroactively change the description of emails that have gone out. `stopped_at` is
recorded because messages sent afterwards carry no arm, and without the date that looks like an
allocator that quietly broke.

**The funnel counts from `messages` and `lead_outcomes`, and a lead still inside its wait window
is `pending`.** ADR-026's whole design rests on that distinction: counting silence-so-far as a
negative would make every arm look worse the sooner somebody looked, which is the one way a
funnel actively misleads. `censored` is the leads watched to the end of their window, which is an
absence rather than a rejection, and rates are over settled leads only. Counted rather than kept
in a counter, because a counter incremented at send time drifts and a crash between the send and
the increment leaves it wrong for ever with nothing to notice — the mailbox-health rollup's
choice, for the same reason.

**The outcome vocabulary is read from `Outcome`, never restated.** A bucket reports each
outcome's own value plus `encouraging`, which is `Outcome.is_encouraging`. Writing those strings
out is this project's most repeated defect: `direction == "outbound"` against a column holding
`'out'`, `outcome != "ok"` where success is `"success"`, `reason="one-click unsubscribe"` against
a four-value constraint.

### Consequences

- `messages.variant` and `messages.warmup_day` now have a producer, so the ADR-026 export stops
  writing NULLs into its two most useful dimensions. `warmup_day_number` had been written for
  that column and had no caller.
- Every lead in the live database has `variant` NULL, which means "no experiment was running",
  which is a perfectly sendable lead. Treating NULL as "not yet eligible" would have stopped an
  in-flight campaign at deploy — the care ADR-044 took with `leads.mailbox_id`.
- The *effective* share is what the screen reports, not the weight an operator typed. The two
  differ once the floor applies, and being surprised by that is how somebody concludes the
  allocator is broken.
- Nothing here decides a winner. With one reply in thirty a difference worth acting on needs
  roughly a thousand sends per arm (`DATASET_DESIGN.md`), so the screen reports and does not
  recommend. F16 is where a recommendation would belong, and it is gated on that volume.

### Rejected

- **A deterministic hash for stickiness.** Adding an arm re-labels leads already in flight.
- **A per-experiment exploration share with no floor.** The operator who sets it to zero is the
  operator most sure of their preferred arm.
- **Round-robin allocation.** Correlates the arm with arrival order, which is exactly the
  confound an A/B test exists to remove.
- **Deleting a stopped experiment.** The arms are what the stored labels mean.
- **Letting an arm vary the persona, the cap or the window.** Those change who is emailed and
  how much, so a difference in results would not be a difference in the email. An arm varies
  wording and nothing else.
- **Declaring a winner on the screen.** At this volume it would be noise with a confidence
  interval nobody reads.

## ADR-054 — A poll that failed closes nothing, and a page may only say that it changed

**Date:** 2026-11-07
**Status:** Accepted
**Relates to:** F22, slice S15.5; builds on ADR-036 (job posts), ADR-049 (signals), ADR-051 (graph)

### Context

The daily ATS poll has existed since F5 and only ever *added*. A posting that left a board was
left exactly as it was, so `job_posts.status`, which has said `open | archived` since the first
migration, had never once held the second value — and nothing could answer "did they fill that
role?". Separately, most Indian companies this product cares about are on no ATS at all, so the
poll has nothing to read for them.

### Decision

**The diff is computed from the authoritative id set of a poll that parsed successfully, and
from nothing else.** `close_missing` archives every open posting of one board that the current
poll did not return. The safety argument is entirely about where it is called: `fetch_board`
raises `FeedError` on any failure long before, so this is never reached with an accidentally
empty set — which would archive a company's entire board on one bad afternoon and report it as
every role being filled. The job-post intake has already paid for the neighbouring version of
this: an unknown source with a plausible payload returned zero postings, which read as an empty
board.

**A genuinely empty board is an empty board.** A company that closed its last role is a real and
interesting event, and refusing to believe it would be second-guessing the only evidence we have.

**Archived, never deleted.** `applications` may point at a posting the candidate applied to, and
the record of what they applied to must survive the role being filled.

**A relisted role reopens rather than becoming a second posting**, because the id is the board's
own. Treating it as new would double-count a company's hiring in any funnel built on these rows.

**A careers page may be watched by its hash, and may only ever say that it changed.** For a
company with no feed: fetch once a day, hash the visible text, and record a `signal_events` row
when the hash moves.

**That produces a signal and deliberately no graph edge.** "The text of this page is different
today" is evidence that something happened and no evidence of what, so there is nothing to put in
`ck_edges_provenance`'s columns beyond a URL — and an edge saying a company is hiring because
their careers page changed is exactly the unsupported claim ADR-051 exists to make unstorable.
The signal says what we actually know: look at this page.

**Scripts and styles are stripped before hashing.** A build id or an analytics nonce in a
`<script>` changes on every deploy, so hashing it would report a change every single day — which
is the same as reporting nothing, except that somebody would act on the first few.

**The hash and never the text.** Storing the page would make this a crawler with a cache —
somebody else's copy of their own site, in our database, for every company any workspace watches
— and `PRIVACY.md` could no longer say we keep no page text. There is no column for it.

**The first fetch never reports a change**, because `page_hash` is NULL until there is one and
"different from nothing" is not a change. A watch added today would otherwise announce a change
tomorrow for every company on it, which is how somebody learns to ignore the list.

**`robots.txt` is the permission model for a page**, and it is the only statement of intent
available: a board's terms are a document somebody read in advance, and a company's careers page
has no such document. A file we cannot read is permission, which is the convention the standard
describes and the only workable reading — most sites serve no such file. A file that disallows us
is honoured, the page is **not requested at all**, and `page_refused_reason` is stored so the
screen says why nothing arrives rather than showing a watch that silently never updates.

**Two failure classes, not one.** `PageRefused` is permanent until the site changes its mind;
`PageUnavailable` is worth trying again tomorrow. Collapsing them is how a site that said no gets
asked again every day, and how one bad afternoon permanently stops a watch.

**The page watch lives on the watchlist row, not in a table of its own.** Found while wiring the
producer: `signals.intake.record` takes a `WatchlistEntry`, because that is what carries the
pause, the company's domain and the lead match. A second table would have needed its own copy of
all three and produced two answers to "which companies do we watch".

**Which sources may be polled is a recorded human judgement, in `terms.py`.** One entry per
source saying what was read, when, and what it permits. **`UNKNOWN` is a refusal here**, which is
the opposite of how the deliverability report treats it and deliberately so: there, "we could not
tell" goes to a person who then decides, and here the next step is an automated fetch repeated
daily for ever. The check is made at the door *and* in the poll handler, because a row written by
hand or before the check existed would otherwise be polled for ever on nobody's authority.

**`watchlist.POLLABLE` now asks that module.** It used to be "every source in `SOURCES` except
`pasted`" — a guess about what we may poll, dressed as a fact, which would have admitted any
source added later on nobody's authority. Hard rule 8 is a promise to a provider, so the basis
for each one is shown on the screen, where the person making the promise can read it.

### Consequences

- `job_posts.status` finally holds `archived`, and the poll log line reports `closed` and
  `reopened`.
- A company with no ATS can be watched, and what that buys is weaker than a feed. The screen
  says so in the field's own hint rather than letting somebody discover it after a week.
- A page change arrives as a `hiring` signal whose title names the page rather than a role. That
  is the honest shape, and it means the Signals screen needs no new state.
- Changing a watch's careers URL clears its hash, so the next poll is a first fetch. Keeping the
  old hash would compare one page against another and announce a change that is only the operator
  having pointed us somewhere else.
- A board URL typed into the careers-page field is refused, with the thing to do instead: it
  would be hashed as a page, giving a daily "something changed" about a feed we could have read
  properly, with the roles thrown away.

### Rejected

- **Inferring a diff from `fetched_at` being stale.** A poll that failed leaves every posting
  stale, so this would archive a board whenever a fetch broke — the failure mode the whole ADR
  is about, reached by a different route.
- **Guessing what changed on a page.** It would need the previous text, which is the thing we
  deliberately do not keep, and a model's description of a diff is a claim with no source.
- **Writing a graph edge from a hash change.** There is nothing true to put in it.
- **Deriving "pollable" from the source list.** That is what was there, and it is a guess.
- **A `watched_pages` table.** Two answers to which companies we watch.
- **Scraping a careers page for the roles themselves.** Hard rule 4's neighbour: the ATS feeds
  are offered for this and a company's own page is not. A hash asks for nothing a visitor does
  not ask for, and a parser pretending to be one would.

## ADR-055 — The fit score is computed on a press and read for free, and the connector only reads

**Date:** 2026-11-07
**Status:** Accepted
**Relates to:** F6, F14, slice S16.1; builds on ADR-037, ADR-028, ADR-030, ADR-041

### Context

The connector shipped with seven read tools over campaigns, leads, research and replies. Four
phases of work have landed since — job postings, the candidate's own facts, the knowledge graph,
signals — and none of it was reachable from an agent, which is the same gap ADR-048 found between
the API and the dashboard, one layer out.

Writing the fit-score tool found something else. **`fit.score_fit` had no caller anywhere.** F6
built the requirements extractor, the scorer and the dataclasses, with tests for each, and nothing
ever ran them against a posting: a grep returned the definition and one docstring that mentions
it. The thirteenth instance of this project's signature shape.

### Decision

**Four new read tools**: `list_job_posts`, `get_fit_score`, `get_entity_edges`, `list_signals`.
Nothing new about the trust model — `org_id` from the membership and never from an argument, the
untrusted wrapper on anything somebody else wrote, the same catalogue containment.

**The fit score is computed on a press and read for free, and they are different routes.**
Extracting a posting's requirements calls a model, so `POST /job-posts/{id}/fit` is a button;
`GET` of the same path and the MCP tool both read the stored row. A read path that called a model
would spend somebody's token budget whenever an agent looked or a screen refreshed, which is the
rule that keeps `GET /mailboxes` from minting an OAuth token and a contact lookup from spending a
credit on a page load.

**So the score is stored**, in `fit_scores`, one row per posting by a unique key. Two rows would
let the screen and the tool show different answers depending on which they read first.

**There is no `compute_fit_score` tool, and that absence is the design.** A tool an agent can call
that spends the user's model quota is the thing F9's whole design refuses (ADR-041), and a
catalogue is where the refusal is checkable rather than remembered. A test asserts the name is
absent, along with the contact-lookup names.

**A score never travels without `matched` and `missing`.** A number with nothing under it invites
whoever reads it to repeat it as a judgement about themselves. Every match names the fact id and
the words that made it one, and the note beside it says what the fraction is of.

**Only confirmed facts are offered to the scorer**, so ADR-037's rule holds one step before the
comparison: a model cannot match against a fact the candidate has not agreed to say. An empty
profile is refused with a sentence rather than scored zero, because zero reads as a bad fit.

**`list_job_posts` defaults to the open postings.** The question an agent is nearly always
answering is "what could I apply to", and an archived posting is a role somebody else filled —
which only became a distinguishable thing in S15.5.

**`get_entity_edges` returns each statement with its own source**, which is what makes "a model
may phrase, never add" checkable from the other side of the connector: a sentence arriving without
one did not come from that table, so an agent cannot launder an invention through this tool.

### A defect found by writing the first caller

`fit._terms` allows `.` inside a token so that `node.js` and `3.11` survive — and that meant **a
word ending a sentence kept its full stop**, so `postgres skills.` in a fact shared only one term
with `strong postgres skills` in a requirement. One shared term is below `MIN_OVERLAP`, so it was
a miss. The whole feature under-matched, and nothing noticed because there was no caller. A
trailing dot is stripped and an internal one kept — the same rule `domain.wording.tokens` applies,
for the same reason.

### Consequences

- `MIN_OVERLAP = 2` means a one-word requirement never matches. That is conservative on purpose
  and now asserted: a confident match on one common word is the worst kind of wrong here, because
  what it is wrong about is the person's own credibility.
- The catalogue is eleven tools, nine read and two propose. `tools/list` is still scope-filtered
  and `required_scope` still derived, so none of this needed a new gate.
- `fit_scores` is the second place a model's output is stored rather than used immediately. It is
  safe to cache because the requirements are a reading of a posting that does not change, and
  re-scoring replaces rather than appends.

### Rejected

- **A `compute_fit_score` tool.** An agent that can spend somebody's quota.
- **Computing the score inside the read path.** Every refresh would be a model call.
- **Returning the score alone.** A bare number is a judgement nobody can check.
- **Scoring against unconfirmed facts.** ADR-037 exists because the person asked about a claim in
  an interview is the person who has to have agreed to it.
- **Appending each score as a new row.** Two answers to one question.

## ADR-056 — Four scopes, one vocabulary, and an answer that is not ours to pass on

**Date:** 2026-11-07
**Status:** Accepted
**Relates to:** F24, slice S16.2; builds on ADR-028, ADR-030, ADR-037, ADR-039, ADR-051

### Context

A connector token was all-or-nothing in one direction: `mcp:read` opened everything readable in
the workspace, and `mcp:propose` added the write. ADR-030 built half the machinery for finer
scopes — `tools/list` is scope-filtered and `required_scope` is derived — and F24 is the other
half.

Writing it found that **`ck_api_tokens_scopes` in `models.py` had said
`scopes <@ ARRAY['mcp:read']` since the table was created, while two migrations widened the live
constraint to three values.** The model had been stale for two slices. A model that disagrees with
the database is exactly what makes `alembic revision --autogenerate` write a migration dropping
something it cannot see, which has already happened once in this repository with an index.

### Decision

**One vocabulary, in `models.API_TOKEN_SCOPES`, and the check constraint is generated from it.**
The cure adopted after `reason="one-click unsubscribe"` made every opt-out a 500: a literal that
has to match a database constraint is read from the thing that defines it. `protocol.SCOPES` and
`api_tokens.ALL_SCOPES` both read that tuple now, so the constraint, the minting route and each
tool's gate cannot disagree.

**The split is by what the holder may see, not by how much we trust them.** That is what makes a
tick box answerable:

- `mcp:read` — the workspace's own work: campaigns, leads, replies, postings, signals, the graph.
- `mcp:profile` — the **person's** own confirmed facts. A separate scope because it is a different
  kind of data: a token minted to help with outreach has no business reading somebody's career
  facts, and the holder of one is often not the person those facts are about.
- `mcp:notes` — may record one note about a company. The narrowest write available.
- `mcp:propose` — may propose a draft into the review queue (ADR-030, unchanged).

**`required_scope` is still derived, and a tool may only narrow it.** A tool that declares nothing
gets the strict answer from whether it writes, so there is no permissive default to forget and a
tool added later is gated whether or not anybody gates it. What F24 adds is that a tool may ask
for something *narrower* than the derivation would give it.

**The answer bank returns its questions and never its answers, and the exclusion is the tool.**
`answer_bank` holds expected pay, notice period and work authorisation: answers somebody gives to
a form **they** are filling in, on a page they opened, through the extension. A model drafting a
cover letter has no use for the number and every opportunity to put it somewhere it should not be.
So the question is returned, the value is not, and the sentence on the screen says so. Knowing
which questions are already handled is the useful half.

**Only confirmed facts are returned, with no option to ask for the proposals.** A proposed fact is
one nobody has agreed to say yet, and the person asked about a claim in an interview is the person
who has to have agreed to it (ADR-037). An argument that could request the unconfirmed ones would
route around the only check that rule has.

**A note is a graph edge, so the source is required by the database.** `propose_company_note`
writes an edge with `predicate = "mentioned"`, which `ck_edges_provenance` refuses without a
`source_url` — so a note an agent cannot point at cannot be stored, by the constraint rather than
by the handler being careful. This is the one path in the system where the thing offering a claim
is a model, which is where ADR-051 earns the most.

**A note is not a research finding.** A hook in `domain_research` was verified against text we
fetched ourselves and may be cited by the drafter. An edge is not in the evidence corpus, so a
draft still cannot cite it, and a brief that quotes it shows the agent's URL beside the line.

**The test is the acceptance criterion, and it tests the mechanism.** For each scope, mint a token
carrying only that scope and assert `tools/list` returns exactly the tools whose `required_scope`
is in it. A hand-written map of names to scopes would be a second copy of the catalogue, and the
first time the two disagreed the test would be describing the wrong one. Two further tests close
the loop in both directions: every tool's scope is one the deployment can grant, and every
grantable scope has at least one tool — a scope with no tool is a tick box that does nothing,
which is worse than no tick box, because somebody ticks it and believes they granted something.

### Two guards had to be restated, and the restatement is the point

- `test_a_tool_that_writes_requires_the_propose_scope` asserted the exact derivation, which was a
  proxy for the rule rather than the rule. What matters is that a write is never reachable with a
  read-only credential. It now asserts that, plus that an undeclared tool still gets the strict
  derivation. The same correction as "every tool is read-only", which was a proxy for "nothing can
  send or approve" and had to break the moment anything wrote. **That is the eighth test in this
  project found to be encoding something other than the rule it was named for.**
- `test_the_scopes_are_read_and_propose_and_nothing_else` pinned the list, so it would fail
  whenever a scope was added for a good reason and said nothing about the thing that matters. It
  now asserts that no scope names a send, an approval, a mode or an allowlist.

And a third guard fired correctly on my own code: `list_answer_questions` used `"approved"` as a
dict key, which `test_nothing_in_the_package_names_an_approved_status` reserves so that nothing in
this package can compare a draft's status to it. The field is `ready_to_use`. **Renaming the field
was cheaper than weakening the guard**, and the guard owns the word.

### Consequences

- Twelve tools across four scopes. A token minted with no boxes ticked reads the workspace's work
  and nothing else, which is what every existing token already has.
- The downgrade **revokes** a token carrying one of the two new scopes rather than stripping it,
  as ADR-030's did: a credential whose permissions change under it while it keeps working is worse
  than one that stops.
- `mcp:send` is still unexpressible, in the database as well as in the code, because the permitted
  set is an allowlist of five and there is no tool such a scope could authorise.

### Rejected

- **A single "full access" box.** The question a person can answer is "may this read my career
  facts?", not "do I trust it".
- **Returning the answers under the profile scope, or behind a fifth scope.** An answer goes to a
  form the person is filling in. There is no version of a model needing their salary number.
- **A `confidence` or `trusted` flag on a note.** ADR-051 rejected it for edges: a claim either
  has a source or does not.
- **Letting a note into `domain_research.hooks`.** A hook was verified against text we fetched;
  this was not, and putting it there would make it citable by the drafter.
- **Keeping the old scope guards and adding exceptions.** A guard with an exception list is a
  guard somebody edits instead of thinking.

## ADR-057 — Pages with addresses, colour in the chrome, and a front door

**Date:** 2026-11-08
**Status:** Accepted
**Relates to:** slice S17.1; builds on ADR-048

### Context

Asked for directly: make the product presentable, with colour and with pages rather than
buttons. The system behind it is complete and the screens worked; what they looked like was an
internal tool, and three specific things made them one.

**There were no addresses.** `view` was a `useState`, so every screen in the product was the same
URL. Nothing could be linked to, bookmarked, opened in a second tab or reached with the back
button, and a demo could not be resumed where it was left.

**There was no identity.** The mark was the letter `O` in a box, the accent was the same generic
blue as "this text is a link", and seven of the nine nav items shared three icons.

**The first screen anybody sees was a form on a grey page.**

### Decision

**Real paths, and a router written here.** `/review`, `/leads/:id`, `/campaigns/:id`, `/signals`.
`frontend/wrangler.jsonc` already sets `not_found_handling: "single-page-application"` — the
comment in that file says it is "what makes a refresh on /leads/123 work" — so History API
routing is safe on this deployment, and that configuration predates any router. Without it,
hash routing would have been the right answer instead.

The router is about a hundred lines and adds no dependency, which is the same bargain this
project already made for twelve hand-drawn icons: what a router must do here is parse a path,
match a pattern, push history and re-render, all of which the platform does. What `react-router`
adds beyond that — nested outlets, loaders, lazy splitting — is not in use on a thirteen-page
dashboard.

**The nav items are anchors, not buttons.** A real `href` is what makes middle-click, "copy
link" and the browser's own back button work, and those three are most of what "pages instead of
buttons" means to somebody using it.

**Colour in the chrome, calm in the content.** The way "add colour" goes wrong in an operational
tool is colouring the content: a red number here means a bounce and an amber one means a
deadline, so a decorative colour in the same space steals meaning from a semantic one. The brand
lives in the rail, the page headers, the funnel, the hero and the mark; a lead's row is
unchanged. Two brand hues rather than one, because a single accent has to be identity, primary
action and second chart series at once.

**The brand is deliberately not the semantic accent.** A brand that is the same colour as "this
is clickable" is not a brand, and that was the old rail's problem: "where I am" and "this is a
link" were one blue.

**One owner per class.** `index.css` owns tokens, `app.css` owns the shell and the controls that
existed, `chrome.css` owns what is new. Two files styling `.nav-item` is a cascade somebody has
to hold in their head, so the brand restyle of the existing rail is an edit to `app.css` rather
than an override from the new file.

**A front door rather than a login screen.** Every claim on it is checkable against the code and
each one names where: `approval_mode` defaults to `review_all`; the claim check refuses a draft
that asserts what no read page supports; `MAX_BOUNCE_RATE = 0.05` over `MIN_SENDS = 20`;
`daily_cap` defaults to 100 warming up from 20. **A landing page that overstates what the system
does is the same defect as a draft that invents a statistic, aimed at a different reader** — and
this is the page the product is pitched from.

**The name comes from the deployment's own domain.** `api.arkvane.in` is this system's API and
"Arkvane" is already the agency name in the live campaign's persona. It is one constant in
`Brand.jsx`.

**An overview that answers two questions and not more**: what needs me now, and is this working.
"Needs you now" shows a row only when its count is non-zero, and shows the all-clear only when
every request has come back — "nothing needs you" is a claim and needs the same evidence as
"three things do". The pipeline is a shape rather than seven numbers, because "1 reply" says
nothing about whether a campaign is working while the step from 38 sent to 6 replied says what
the problem is.

**One `useLoad`, where there were four.** Each screen had its own copy and they had started to
differ. The shared one adds `asked` — "we have heard back, including hearing an error" — which
is the distinction that twice made this dashboard state something false while it waited, and
which none of the four copies had.

### Three defects, all found by opening the page

The honest note at the end of ADR-048 said those screens had not been rendered in a browser and
that the structural guards were a substitute for looking rather than a proof. That was right
again here: the build was green, the frontend suite passed, all four guards passed, and three
things were broken.

1. **The unset theme was a mixture of both themes.** `:root[data-theme='light']` matches only
   once somebody has *used* the toggle. Before that, a visitor on a light system picks up the
   light base tokens from the `prefers-color-scheme` block and whatever `:root` says for
   everything else — so the new brand tokens, defined on `:root` and on `[data-theme='light']`
   but not in the media block, rendered a **dark hero gradient under dark navy text**. Invisible,
   with no error anywhere.
2. **`align-items: center` on the hero.** A centred grid item taller than its row overflows
   equally in *both* directions, so on a short window the headline was cut off above the top of
   the page with no way to scroll up to it.
3. **`<SignIn embedded />` was passed a prop that did not exist**, so the form brought its own
   full-height centred panel inside the hero card — two cards and 80vh of whitespace.

### A fourth guard, for a class the other three could not see

**Six `var(--x)` references named tokens that do not exist**, in three files, and `var()` with an
unknown name and no fallback resolves to nothing: the declaration is dropped and the element
inherits. The sign-in page's muted paragraphs and the settings tick-boxes had been rendering in
body colour rather than muted colour. `index.css` states the rule the drift broke — a component
never uses a raw colour, and a shade it needs belongs in the token file — and the rule *was*
followed. What drifted was the names, which produces the same symptom and is harder to see.

`tests/unit/test_dashboard_tokens_resolve.py` asserts that every reference resolves, that dark
and light define the same names, and that **every token with a light value also appears in the
`prefers-color-scheme` block** — which is the guard for defect 1 above, written after it rather
than before, and run against the bug to watch it name `--grad-hero`.

### Consequences

- Thirteen addressable pages. A lead is a link: `/leads/:id` opens the drawer on load, and the
  drawer stays, because replacing the list was what made comparing two leads mean navigating
  twice and losing the scroll position both times.
- Campaign-scoped screens keep the top-bar picker rather than nesting under `/campaigns/:id/...`.
  Moving six screens under a campaign's path changes how every one of them gets its data; stated
  here because the nesting is the obvious next step, not because it is wrong to want it.
- The bundle grew about 9 KB gzipped, which is the router, the landing and the overview.
- Both themes and the router were verified in a browser, not inferred from a green build.

### Rejected

- **`react-router`.** A dependency tree for what the platform already does.
- **Hash routing.** The host is already configured for real paths.
- **Colouring the content.** It would cost the semantic colours their meaning.
- **Inventing a product name.** The deployment's own domain already had one.
- **Writing marketing claims the code does not support.** The whole product is about not doing
  that.

## ADR-058 — An OAuth 2.1 server of our own, and an access token that is an ordinary credential

**Date:** 2026-11-09
**Status:** Accepted
**Relates to:** F14, slice S16.3; completes ADR-027's Phase 2; builds on ADR-028, ADR-030, ADR-056

### Context

Connecting your own Claude has meant minting a token in Settings and pasting it into a client.
That works and it is the thing people complain about, which is the gate ADR-027 set for building
this: *"do it when pasting tokens becomes the complaint, not before."*

ADR-028 established why we cannot delegate: **Supabase accepts the RFC 8707 `resource` parameter
and ignores it**, issuing `aud: "authenticated"` whatever was requested (supabase/auth#2610). A
token whose audience is a constant cannot be shown to have been minted *for* this endpoint, and
accepting one means accepting a credential obtained for something else — the confused-deputy
problem resource indicators exist to prevent. So the choice was our own authorization server or
no OAuth at all.

### Decision

**An access token is a row in `api_tokens`**, with two new columns saying which client holds it
and which resource it is for. This is the decision everything else rests on. The tool catalogue,
the scope gate, the untrusted-text wrapper and — most importantly — the re-resolution of the
membership on every single call are *untouched*. OAuth is a different way to **obtain** the same
credential, not a second kind of credential with its own parallel rules. A separate token table
would be a second set of rules to keep in step, and the first time they drifted the difference
would be a credential outliving the membership behind it.

**PKCE with S256, and `plain` is refused outright** — in the validator and in a check constraint,
so a row written by hand cannot ask for it. A challenge equal to its verifier protects against
nothing and exists in the spec for clients that could not hash, which is no client we will see.

**The `resource` parameter is required, checked at authorize, checked again at exchange, stored
on the token, and checked at `/mcp`.** A missing one is refused rather than defaulted: defaulting
it means a client that never asked for an audience gets one anyway, and its token becomes
indistinguishable from one that did — which is precisely Supabase's behaviour.

**Redirect URIs match exactly.** Not a prefix, not a host, not "ignoring the query". Every
loosening of this rule is a published attack, because the redirect is where the code goes.

**A refresh token rotates, and reusing one revokes the whole chain.** Somebody presenting an
already-exchanged token is a stale client or a stolen copy; the safe answer to both is the same.
Letting the real client carry on means a stolen token works for ninety days and nothing notices.

**Clients are registered by hand and belong to a workspace**, and the second half of that was not
the first design. Dynamic Client Registration is deprecated in the 2026-07-28 revision and was
always wrong here — it lets anything that can reach the server become a client, and what a client
asks for is somebody's whole workspace. The table began with **no `org_id`**, because a client is
Claude's connector and that felt deployment-wide. A test failure showed the cost: any owner could
register a client with a plausible name and their own redirect URI, and *another* workspace's
user would be shown a consent screen saying "allow this". **A consent screen is a phishing
surface**, and the name on it must come from somebody the person being asked already trusts. So
each workspace chooses which applications may ask it for anything — which is how every
OAuth-consuming product does per-tenant app allowlists, and keeps this table ordinary under
ADR-004 rather than a third exception to it.

**The consent screen says only what our own rows say.** The client's name is read back by
`client_id`; the scopes are described in the same words the tick-boxes in Settings use. A screen
that renders a name the caller supplied is a phishing page with our domain in the address bar.
Every parameter is validated *before* it draws, so a request that could never succeed is refused
on the screen rather than after somebody presses Allow.

**Issuing a code is a POST and never a GET.** The browser arrives at the dashboard's
`/oauth/authorize` page by redirect, and that page writes nothing. A GET that issued a code would
mean any page that could make a browser follow a link could obtain one.

**The 401 at `/mcp` carries `resource_metadata`** (RFC 9728). Without it the only way in is a
pasted token, which is the thing this slice exists to stop being the answer.

**Errors are coarse on purpose.** Unknown, expired, replayed, wrong client, wrong redirect and
wrong verifier are six mistakes and one answer, `invalid_grant`, because distinguishing them
tells somebody guessing which half of their guess was right.

### Consequences

- A hand-minted token keeps working, unchanged: its `resource` is NULL, it is only ever presented
  here, and it is the credential this endpoint has always taken. A test asserts it.
- `PUBLIC_API_URL` becomes required for OAuth. Without it the server refuses with a 500 and says
  why rather than guessing an audience — a guessed audience is no audience.
- `mint_secret` was promoted from `_mint`, because an access token generated a second way would
  be a second thing to keep right: the same prefix, the same entropy, the same hash.
- The downgrade revokes every OAuth-issued token rather than leaving them with the audience
  column removed. A token whose audience can no longer be checked would be accepted here on the
  strength of being a row, which is the thing the check exists to prevent.

### Rejected

- **Supabase as the authorization server.** It ignores `resource`; see above and ADR-028.
- **Dynamic Client Registration.** Deprecated, and it would let anything ask your people for
  their workspace.
- **A client secret.** These are public clients; a secret a desktop application cannot keep is a
  secret in a log pretending to be security, and PKCE is what actually proves the exchange.
- **A separate OAuth token table.** Two sets of rules to keep in step with the membership check.
- **Deployment-wide clients.** One workspace's owner could name a consent screen shown to
  another's user.
- **`client_credentials`.** Every token here acts for a person and must resolve to a membership.

## ADR-059 — The system may notice what reviewers change, and may not act on it

**Date:** 2026-11-09
**Status:** Accepted
**Relates to:** F16, slice S16.4; uses ADR-026's `drafts.derived_from_id`; same shape as ADR-053

### Context

`drafts` creates a new version on an edit rather than mutating, and `derived_from_id` links a
human rewrite back to the model draft it came from. ADR-026 added that column precisely because
version numbers cannot express it — a regeneration after a rejection also makes a new version
and is a *different event*. So the preference data has been accumulating since Phase 9, and
nothing has ever read it.

**This is the only feature in the system where the product's own behaviour would change on the
strength of what it observed**, which is what makes it different from everything else here.

### Decision

**Describe, then compare, then summarise for a person — and stop.** Each step is only safe given
the one before it.

**The describing is arithmetic, not a model**, and that is the decision this ADR is. A model
asked "what did the human change?" produces a fluent sentence that is sometimes about a change
that did not happen. The output of this feature goes *into a drafting prompt*, so a hallucinated
observation becomes a standing instruction and the system has learned something nobody did.
Everything `domain/edits.py` reports is computable from the two texts and checkable by reading
them: a removed sentence, a subject that got shorter, a body that lost more than fifteen per
cent of its words.

**What it can see is narrower than what a person would notice, and that is the trade.** A short
list of things that are definitely true beats a long list that is mostly true. It cannot see
tone, it cannot see that a claim was softened, and it reports only removals rather than changes
because a changed sentence might be a typo.

**Counted per draft, never per occurrence.** One reviewer deleting the same line three times
while editing one email is one opinion. Counting it three times would let a single session
invent a rule for everybody.

**Three separate drafts before anything is proposed.** Once is a Tuesday. This is not a
significance threshold and is not presented as one — `DATASET_DESIGN.md`'s ~1,050 sends per arm
is about A/B outcomes, which is a different question. This is "more than once is a pattern".

**Nothing reaches a prompt until a person approves it.** A lesson is `proposed`, and only
`approved` is read by the drafter. A loop that closed itself would be the system teaching itself
from its own output with nobody reading the lesson, and one bad afternoon of edits would become
a rule. The gate is a person rather than a threshold because no threshold distinguishes "our
reviewers prefer this" from "one reviewer was in a hurry on Thursday".

**A dismissed lesson is never proposed again**, by a unique key on `(org_id, kind)` where the
kind is stable across recomputations. Somebody looked and said no; asking every week trains them
to stop reading.

**The screen shows the instruction before it is approved, not after.** Approving a *summary* of
a change is not the same as approving the *instruction* that summary becomes, and only one of
those is what actually happens.

**A lesson is advice about how to write and never about what is true.** It travels through the
same prompt parameter as an A/B arm's instruction (ADR-053) and is held to the same rule: the
draft it produces goes through the same `validate_draft` and the same claim check, so an
approved lesson cannot widen what may be asserted. Hard rule 6 is untouched by anything here. A
test asserts no generated instruction contains the words a factual claim would need.

**The evidence travels with the pattern.** `evidence` holds the draft ids it was counted from,
because a pattern without its evidence is an assertion — and this one is about to become an
instruction to a model. Somebody deciding can open them.

### Consequences

- The feature is free and non-networked: counting edits is arithmetic over rows the workspace
  already has, and no model is involved at any point on this path.
- A workspace that has never edited a draft sees an empty list, which is the honest answer.
- `build_draft_prompt` gains one more optional section. The order is lessons, then the arm's
  instruction, then the reviewer's rejection feedback — most general to most specific, with the
  objection about *this* text last, because it is the one that must not be overridden.
- The producer is wired in the same slice as the table, and a test asserts the handler passes
  the lessons. Five things in this project have been built and never called; a lesson nobody
  reads would be the sixth.

### Rejected

- **Describing the diff with a model.** A hallucinated observation becomes a standing
  instruction.
- **Applying a pattern automatically above some count.** No count distinguishes a preference
  from a bad afternoon.
- **Re-proposing dismissed lessons.** It trains people to stop reading the list.
- **Letting a lesson state a fact.** The claim check exists so a draft asserts only what the
  evidence supports, and a lesson that could add to the evidence would route around it.
- **Waiting for ~1,050 sends per arm.** That is the A/B significance number and a different
  question; this one needs three edits, not statistical power.
