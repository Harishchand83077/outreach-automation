# How this system behaves: a question-by-question answer

Every answer here is traceable to a file. Where an answer and the code disagree, the code is
right and this document is wrong — say so and it gets fixed. Things that are **not** implemented
are marked **[gap]** with where they are tracked, rather than described as if they worked; this
project has produced sixteen production defects and almost all of them were a document or a
comment describing an intention that no code carried out.

`ARCHITECTURE_TARGET.md` §27 is the architectural companion to this. This file is the operator's
version: what happens when, and what to do about it.

---

## 1. Workers

### What are the workers and where do they run?

One process, `python -m app.worker`, in a container on the EC2 instance beside the API and
Postgres. It runs **five asyncio tasks in one event loop**, not five processes:

| Task | Does | Cadence |
|---|---|---|
| `JobRunner` | Claims and runs queued jobs (research, drafting, reply drafting) | Continuous, 10s idle wait |
| `SendLoop` | At most one send per cycle, per mailbox | Paced; see §4 |
| `ImapLoop` | Polls INBOX and the junk folder, ingests and classifies | 3 min busy / 15 min quiet |
| `Scheduler` | Reaps leases, recovers stuck sends, queues follow-ups, closes silent leads, retention | 1 min |
| `Heartbeat` | Writes the liveness row `/ready` reads | 30 s |

They share one process because at this volume the work is almost entirely I/O-bound waiting on
SMTP, IMAP and HTTP. Five processes would mean five connection pools against a database with
100 connections and 2 GB of RAM to fit everything in.

### How does a worker pick up a job — push or pull, and why?

**Pull.** The runner polls:

```sql
SELECT ... FROM app.jobs
 WHERE status = 'queued' AND run_after <= now() AND kind = ANY(:kinds)
 ORDER BY priority, run_after
 FOR UPDATE SKIP LOCKED
 LIMIT :batch
```

`FOR UPDATE SKIP LOCKED` is the whole trick: a second worker trying to claim the same row
**skips it** instead of blocking, so workers never contend and adding a second worker is safe
without any coordination.

Pull rather than push, deliberately:

- **Back-pressure is free.** A worker takes work only when it has capacity. A push system has to
  model the consumer's capacity itself and gets it wrong under load.
- **The claim and the work are in one transactional world.** Pushing means the message leaves the
  database, and then a crash loses it unless the broker and the database agree — which they
  cannot, having no shared transaction (ADR-014).
- **Restart is trivial.** There is no in-flight delivery to recover. Whatever is `queued` is
  queued; whatever is `running` with an expired lease is reaped.

The cost is polling latency — up to 10 seconds before a new job is noticed. At this volume that
is irrelevant; `LISTEN`/`NOTIFY` would remove it for free if it ever mattered.

### What happens when a worker crashes mid-job?

The claim sets `status='running'`, `locked_by`, and `locked_until` (a **lease**). The task
extends the lease every 30 seconds while it works. If the process dies, nothing extends it, and
the `Scheduler`'s reaper finds every job whose `locked_until` has passed and puts it back to
`queued` — or marks it `dead` once it has spent `max_attempts` (5).

So a crash costs one lease period, not the job. Jobs are written to be idempotent so a re-run is
safe: research upserts its domain row, `save_draft` supersedes the previous version rather than
appending blindly, and the send path is protected separately (§2).

### Parallel or sequential?

Both, deliberately:

- **Across leads: parallel**, bounded per kind — `RESEARCH_LEAD: 3`, `DRAFT_LEAD: 2`,
  `DRAFT_FOLLOWUP: 2`, `DRAFT_REPLY: 2` (`worker/runner.py`). The caps exist because these call
  an LLM on a free tier; the limit that binds is the provider's, not ours.
- **Within one lead: sequential**, and enforced by the state machine rather than by locking —
  research must finish before drafting is queued, because drafting is queued *by* research in the
  same transaction.
- **Sending: strictly sequential, one message per cycle per mailbox.** Not for safety but for
  deliverability: 100 emails in ten minutes is the signature of a compromised account.

---

## 2. Crashes, duplicates and the send path

### If the sheet upload crashes halfway, does re-uploading duplicate?

No, twice over:

1. **A crash before the commit imports nothing.** The whole import is one transaction, so a
   half-imported sheet cannot exist.
2. **A re-upload adds only what is new.** `UNIQUE(campaign_id, email_norm)` means the same person
   cannot be imported twice into the same campaign; the preview reports them as duplicates.

**[gap, tracked as B-1 in §27.8]** That single transaction is also the weakness at 5,000 rows:
one long write, and a statement timeout loses everything rather than part. Chunked commits are
the fix.

### If a mail is sent and the process dies before recording it, is it sent twice?

**No. The guarantee is at-most-once, and that is a deliberate choice over exactly-once.**

The sequence is: claim → send → record. The claim writes a `messages` row with
`status='sending'`, a **pre-generated `Message-ID`** and a UNIQUE `idempotency_key` of
`{lead}:{kind}:{sequence}` — *before* the socket opens. So:

- Crash before SMTP: the row is `sending`, the next claim is refused by the unique key.
- Crash after SMTP, before the update: the row is `sending` and **the mail is out**. This is the
  dangerous case and it is the one that actually happened in production.

After 10 minutes (`outbox.STUCK_AFTER`) the `Scheduler` runs ADR-002's recovery: it asks IMAP
whether that exact `Message-ID` is in the Sent folder.

- **Found** → the message really left → `sent`, and the lead moves on.
- **Not found** → `unknown`, and a human decides. **It never auto-resends.**

That asymmetry is the policy you described, and it is the right way round: a cold email that
arrives twice is worse for the recipient and for the sending reputation than one that needs a
person to confirm.

### Does our provider save SMTP-sent mail to the Sent folder?

**Yes, verified on the live system.** Gmail copies SMTP-relayed mail into `[Gmail]/Sent Mail`,
and the recovery pass has found real messages there by `Message-ID` — it self-healed a bug
before anybody had found that bug. So `BCC_SELF` stays **off**.

ADR-002 made `BCC_SELF` optional precisely so this could be checked rather than assumed. A
provider that does **not** save SMTP-sent mail needs it turned on, and that check belongs in the
go-live list before switching provider.

### How are deadlocks handled?

By never creating the conditions for one, rather than by retrying.

Three places take locks, always in the same order:

1. `jobs` — `FOR UPDATE SKIP LOCKED`. The second worker skips rather than waits, so this lock
   can never be part of a wait cycle.
2. `leads` — `transition()` locks exactly one lead row, always *after* the job row, and writes
   the state and the `events` row in the same transaction.
3. `messages` — arbitration is by UNIQUE index, not by a lock.

No code path locks two leads at once, so there is no cycle to form. The one long-running
operation, the retention sweep, deletes in **batches** specifically so it cannot hold a
table-wide lock.

### Where do failed jobs go, and can I see them?

`jobs.status = 'dead'`, with `last_error` and `attempts` on the row — a dead-letter table rather
than a dead-letter queue. The `Scheduler` raises an alert (`n job(s) exhausted their retries and
are dead`) and the `alerts` table records it.

**[gap, C-1]** There is no dashboard view of dead jobs and no button to requeue one. Three dead
jobs were found during a deploy and requeued with SQL. An admin route plus a panel is the fix.

---

## 3. LLM failures, rate limits and budget

### How are multiple concurrent requests to a free-tier model handled?

Three mechanisms, in order:

1. **Concurrency caps per job kind** (`runner.py`) — at most 3 research and 2 drafting jobs at
   once, so we never have more calls in flight than the free tiers tolerate.
2. **An ordered chain, not one model.** `LLM_CHAIN` lists entries in preference order. A failure
   or a 429 moves to the next entry for that call — so a rate limit is a *fallback*, not an error.
3. **Cool-downs with backoff.** A failing entry is parked for a computed delay (longer on each
   consecutive failure) and is not tried again until it expires. Asking an entry we know is
   limited costs a round trip and burns goodwill with the provider.

There is also a **pre-flight check**: before calling, the router asks whether the entry is within
its `daily_token_cap`, from the `llm_usage` table. Being told by a provider is the expensive way
to learn a limit.

### What if every model is exhausted?

`CapacityExhausted` is raised, and it is handled deliberately differently from other errors:
research **pauses the lead rather than guessing**. The comment in `research/service.py` is the
rule — *"a lead with no research is recoverable; a lead with invented research is an email we
cannot take back."* The job is retried later, the lead stays where it is, and nothing is drafted
from nothing.

Already-approved mail keeps sending: the send loop does not call a model.

### Is there an alert for tokens remaining?

**[gap]** `llm_usage` records every call's tokens and the router enforces per-entry daily caps,
so the data exists and the limit is respected — but **nothing alerts on approaching it**. The
first sign today is work slowing down. A threshold alert (say 80% of the daily cap) is cheap and
is the right next addition.

### If the model answers and we crash before saving, is it called again?

Yes, and that is the correct trade. The job is retried, the model is called again, and the money
(or quota) spent on the first attempt is lost. The alternative — recording "we called the model"
before having the answer — would risk a lead stuck believing it had a draft that was never
stored.

The cost is bounded: the LLM step is at most a few seconds, so the window is small, and
`max_attempts` caps how often it can repeat.

### Can an LLM failure be mistaken for success and sent?

**No, and this is structurally enforced, not merely checked.**

- A draft that fails validation is stored with `status='validation_failed'`, which is **not** a
  sendable status. The send loop only ever selects `status='approved'`.
- Error text can never become email content: the generator raises rather than returning the
  exception as a draft body (issue B2 and NEW-1, both prototype defects, with
  `test_drafting_generator.py::TestFailureHandling` holding the line).
- A draft with no text at all is refused by `save_draft` rather than stored as an empty email.
- Bulk approval **skips** failed drafts entirely.

---

## 4. Throttling, rate limiting and scheduling

### How is sending throttled?

Four levels, each with a different job:

| Level | Mechanism | Why |
|---|---|---|
| Per send | `pacing_gap` = remaining window ÷ remaining capacity, jittered | A send exactly every 270 seconds is obviously a machine |
| Per day per mailbox | `daily_cap` (100), counted from `messages`, never a stored counter | A counter drifts the first time a crash lands between the send and the increment |
| Per new mailbox | Warm-up: 20/day, +10 every 2 business days | A new address sending 100 on day one is a spam signal |
| Per window | Mon–Fri 10:00–17:30 IST per campaign, weekend roll-forward | Product rule; weekend cold email reads as spam |

`sending` rows count against the cap too — a claimed but unresolved message is capacity already
committed, and not counting it would hand back allowance the mailbox has really used.

### How is the API rate limited?

Per-user and per-address token buckets (`api/ratelimit.py`), with the client address taken from
`X-Forwarded-For` **only** when the request came from the Compose network — Caddy strips any
header a caller sent. Without that pair the address-keyed limit is an honour system.

### Can we schedule a campaign?

**[gap, B-5]** Not yet. A campaign is `running` or it is not; there is no "start Monday", no end
date, no "stop after N sends". `starts_at`/`ends_at` checked in `find_candidates` beside the
window is the fix, and it is the one item on this list that is a feature rather than a repair.

What *is* scheduled: the send window, the follow-up date, the silent-close date, retention, and
the warm-up ramp.

---

## 5. Replies, threads, bounces and opt-outs

### How is a reply tied to a lead?

**Threading-first.** Three strategies, strongest first (`reply/service.py::match_lead`):

1. **`In-Reply-To` / `References` against our own stored `Message-ID`.** We generate the
   `Message-ID` before sending and store it on the `messages` row, so this is exact: this message
   claims to answer a specific email we sent.
2. **A DSN's `Final-Recipient`** for a bounce, which names the address that failed even when the
   threading headers are lost inside the wrapper.
3. **Sender plus normalised subject within 30 days** — a bounded fallback, used only when the
   first two fail, because beyond a month a coincidence is likelier than a real match.

Quoted text **is** stripped before classification (`strip_quoted`), so our own words coming back
in their reply cannot be classified as theirs.

> The description "matching is by sender address only, no threading, no stored `Message-ID`"
> describes the **prototype** (issues B10 and B11). It has not been true since Phase 6.

Our replies also carry `In-Reply-To` and `References`, so they land in the same thread in the
recipient's client.

### How does the IMAP poller work?

Per `(mailbox, folder)` UID cursors stored in `imap_cursors`, `BODY.PEEK[]` so nothing is ever
marked read, `UIDVALIDITY` checked so a server-side renumbering triggers a bounded rescan rather
than silently skipping mail, and at most 200 messages per poll so a neglected mailbox cannot
produce an unbounded batch.

Folders: INBOX **and the server's junk folder**, discovered via `LIST` and RFC 6154's `\Junk`
attribute rather than hardcoded — Gmail calls it `[Gmail]/Spam` in English and translates it
otherwise. This was added because the first real reply this system received went to Spam and the
reply engine could not see it.

### Bounces and opt-outs

| Signal | Behaviour |
|---|---|
| Hard bounce (5.x) | Suppressed permanently, lead → `BOUNCED`, follow-ups cancelled in the same transaction |
| **Soft bounce (4.x)** | **Not** suppressed — deferred a day. A full mailbox is not a reason to give up on somebody for ever |
| Opt-out | Suppressed immediately, lead → `OPTED_OUT`, and **no acknowledgement is sent** — that would be another unwanted email |
| Opt-out phrases | Matched deterministically *before* any model is consulted, including Hinglish forms. A model never decides whether somebody opted out |
| Reply to a closed lead | Recorded, but `OPTED_OUT` and `BOUNCED` never reopen |

### Is there a cross-campaign table of bad addresses?

Yes — `suppressions`, and it has been org-wide since Phase 6. Keyed by `email`, `domain` or
`email_hash`, checked **at import** (a suppressed address never becomes a lead) and **again
before every send** (one added since the approval still wins). A privacy erasure leaves a
SHA-256 digest, so forgetting somebody cannot make them contactable again.

### Were clear refusals routed away from the review queue (ADR-008)?

**Yes, applied.** `SENT -> NOT_INTERESTED` and `FOLLOWUP_SENT -> NOT_INTERESTED` are both legal
transitions in `domain/state.py`, so an unambiguous refusal closes the lead without passing
through `REPLIED`. Routing it through `REPLIED` would briefly mark it as needing a human, and
there is nothing for a person to decide about *"we already have a vendor for this"*. The state is
recoverable: `NOT_INTERESTED -> REPLIED` is allowed if they write again.

---

## 6. The same person in two campaigns

This was the sharpest question asked and it was a real defect.

`UNIQUE(campaign_id, email_norm)` is **per campaign on purpose** — the same person may
legitimately appear in a later campaign, and the prototype's *global* unique key made
re-importing a corrected sheet silently do nothing (issue B16). The cost is that two campaigns
can hold the same person, and if both run, both send.

**The guard is now per person per org**, in `claim_send` beside the suppression check, and it
applies to first emails only:

- Somebody emailed from **another lead** within `CONTACT_COOLDOWN_DAYS` (90) is not cold-approached
  again. This covers "campaign A sent, A was paused, B runs with the same person" — pausing a
  campaign does not unsend its email.
- Somebody with a **live conversation anywhere in the org** (`SENT`, `FOLLOWUP_SENT`, `REPLIED`,
  `IN_CONVERSATION`, `MEETING_BOOKED`) is never cold-approached, whatever the date says. That is
  the reply-mid-switch case: answering a reply in one campaign while opening a pitch in another
  is the worst version of this.

Follow-ups and replies are deliberately **not** checked: they belong to a thread already open
with that person, and blocking them would break the conversation the rule exists to protect.

It is a **cool-down, not a suppression**: a second approach a year later reads as a fresh one.

---

## 7. Data, concurrency and Postgres

### WAL, isolation, and read/write concurrency

Postgres 17 with default settings: WAL on (it is not optional — it is how Postgres is crash-safe
and how `fsync` durability is achieved), `READ COMMITTED` isolation, and MVCC, which means
**readers never block writers and writers never block readers**. A dashboard query during a send
sees a consistent snapshot without waiting.

Where we need more than `READ COMMITTED` gives, we take an explicit row lock rather than raising
the isolation level: `transition()` locks the lead, `claim_send` relies on a unique index.
Raising the isolation level globally would trade a clear, local guarantee for serialisation
failures that would have to be retried everywhere.

**[gap, D-3]** `messages(message_id_hdr)` has no index, and the reply matcher queries it for
every inbound message. Fine at thousands of rows; a sequential scan at a million.

### Connection pool

`DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=2`, `pool_pre_ping=True`, `pool_recycle=1800`
(`db/engine.py`). So at most 7 connections per process, 14 across the API and worker, against
Postgres's default 100 — comfortable headroom, and deliberately small because each idle
connection costs memory on a 2 GB box.

`pool_pre_ping` matters more than it looks: it catches a connection the database closed while
idle, which is otherwise an error on the first query after a quiet period.

### Are rows ever overwritten?

Almost nowhere, by design:

- **Drafts** are never updated. An edit or a regeneration writes a **new version** and supersedes
  the old, so what the model produced survives beside what the human decided to send.
- **Events** are append-only, enforced by a database trigger, not by convention.
- **Messages** are updated only along their own lifecycle (`sending` → `sent`/`failed`), never
  rewritten.
- **Leads** are the exception: `state` is mutable — but only `transition()` may write it, it
  takes the row lock, and it writes an `events` row in the same transaction, so the history is
  reconstructable even though the current value is overwritten.

### Orphaned inbound messages

Inbound mail matching no lead is stored with `lead_id = NULL` — deliberate, so a reply we failed
to match is not lost. The cost is accumulation: 1,400 unrelated messages were ingested from a
personal inbox in one run.

**[gap, C-3]** Nothing prunes them. They are small (~3.4 KB each) so this is a privacy surface
before it is a storage one — somebody's unrelated mail sitting in our database. The fix is a
retention rule in the existing hourly sweep: delete orphans older than N days, since an inbound
message that has not matched a lead within a few days never will.

---

## 8. Scale: what happens with 1,000 leads at once

### Database load at import

5,000 rows is the hard cap (`MAX_ROWS`), 5 MB the file cap. The insert is one transaction, so
1,000 leads is one write of about 1 MB — not a load problem for Postgres. **[gap, B-1]** The
problem is the transaction's *length*, not its size.

### Research and drafting fan-out

**[gap, B-2 — the real one.]** Importing 1,000 leads into a research campaign queues 1,000
`RESEARCH_LEAD` jobs, all runnable immediately. Per-kind concurrency stops the worker melting,
but nothing paces against the **daily** free-tier limits. The chain's quota would be spent in
minutes, every subsequent job would fail into a cool-down, and the dashboard would show a stalled
pipeline with no obvious cause.

The fix is staggering at enqueue time, derived from the campaign's daily cap: there is no value in
researching 1,000 leads today when at most 100 can be emailed. Research slightly more than the
next day's worth, each day.

**Template mode does not have this problem** — rendering is local and instant, so 1,000 drafts
appear in seconds and cost nothing. They land in a paginated review queue, which is why the queue
was given a cursor and a `total` rather than returning everything.

---

## 9. Observability, backups and roles

### Worker liveness (ADR-009)

The API cannot see another process's memory, so the worker writes a `worker_heartbeats` row every
30 seconds and `/ready` fails on that row's **age**. Before this, an API serving perfectly while
the worker was dead reported itself ready — and a dead worker is invisible from outside for hours,
because the symptom is that nothing gets researched, drafted or sent.

Point an external monitor at `/ready`, never `/health`.

`worker_heartbeats` carries no `org_id` and `alerts.org_id` is nullable, both deliberate
deviations from the tenancy rule (ADR-009): a process is not tenant data, and a stalled worker
stops every org's work at once — attributing it to one org would hide it from the others.

**To improve it further:** the heartbeat says the worker is alive, not that it is *working*. A
task stuck in a loop that never raises would still beat. Per-task `cycles` counters are already in
the heartbeat payload; alerting on a counter that stops advancing would close that gap.

### Single failure domain — is there a solution?

The honest answer: **yes, but each costs money, and the current posture is a deliberate trade.**

ADR-001 moved Postgres onto the VM, which removed five problems and introduced exactly one: the
database dies with the instance. Today's mitigation is a nightly `pg_dump`, 14 days retained,
uploaded to S3, with a restore drill that has actually been run (row counts matched). Worst case
is ~24 hours of loss.

Options, cheapest first:

1. **More frequent dumps** — hourly instead of nightly. Free, cuts worst-case loss to an hour,
   costs a few hundred MB of S3. **This is the recommended next step.**
2. **WAL archiving to S3** (`archive_command`) — point-in-time recovery to the last few minutes.
   Free in software, a few dollars a year in S3, and a genuine step up in guarantee. The cost is
   operational complexity: a broken `archive_command` silently fills the disk.
3. **A managed Postgres with its own backups** — removes the problem and reintroduces the five
   ADR-001 removed, plus a monthly bill.
4. **A replica** — real HA, double the cost, and overkill for a system sending 100 emails a day.

At this scale 1 is right now and 2 is right before the data becomes irreplaceable.

### Roles

Three exist and are enforced: `owner` > `admin` > `member` (`api/deps.py::ROLE_ORDER`).

| Capability | member | admin | owner |
|---|---|---|---|
| Read the dashboard, lead detail, timeline | ✅ | ✅ | ✅ |
| Judge drafts — approve, reject, bulk-approve | ✅ | ✅ | ✅ |
| Add a suppression | ✅ | ✅ | ✅ |
| Create or edit a campaign, persona, templates | ❌ | ✅ | ✅ |
| Create a mailbox, set a campaign running | ❌ | ✅ | ✅ |
| **Remove** a suppression | ❌ | ✅ | ✅ |
| Erase a lead, undo an import | ❌ | ✅ | ✅ |
| Read or change the **send mode** | ❌ | ❌ | ✅ |

The split is "the rules versus the work": a `member` reviews drafts but cannot change the rules
underneath them. Removing a suppression is admin because it is the one action there that can lead
to mailing somebody who asked us to stop; *adding* one is protective and open to anyone. Only an
`owner` may set `live`, and only in production, with a typed confirmation and an audit row
(ADR-010).

**Should more be added?** Not yet. A fourth role worth having eventually is a **reviewer-only**
account below `member` — judge drafts, see nothing else — for when review is outsourced. Until
somebody outsources review it would be a role with no occupant.

---

## 10. Throughput and latency, and where the headroom is

Current end-to-end latency for one lead, measured on the live system:

| Stage | Time | Bound by |
|---|---|---|
| Import → research queued | instant | same transaction |
| Research | 10–40 s | page fetches, then one LLM call |
| Research → draft queued | instant | same transaction |
| Drafting | 5–20 s | one LLM call, plus retries on validation failure |
| Draft → review queue | instant | |
| **Approval** | **minutes to hours** | **a human** |
| Approval → sent | up to the pacing gap (minutes) | deliberate |

**The throughput ceiling is not technical.** It is the human review step, exactly as
`ARCHITECTURE_TARGET.md` §20 predicted: 50–100 minutes a day of review at full volume. Everything
downstream is deliberately slowed — the cap, the window, the jitter — because sending faster is
the one thing that would get the mailbox suspended.

Where real headroom exists, in order of value:

1. **Template mode removes the LLM from first emails entirely** — a thousand drafts in seconds.
   This is the biggest throughput change available and it is already built.
2. **Bulk approval** cuts the human step from per-draft to per-batch, with the weak ones held
   back so the batch is still read.
3. **Staggered research (B-2)** does not increase throughput but stops the free tier collapsing
   under a large import, which today looks like a throughput problem.
4. A second worker process — safe, free, and pointless until the LLM quota stops binding.

Latency improvements that would be *wrong*: shortening the pacing gap, widening the window,
raising the cap above 100, or auto-sending. Each trades the sending reputation for a number that
only looks better.
