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
