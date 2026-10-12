# Progress

Updated 2026-10-11 (Phase 9 slice 1). Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are. Phases 0 through 9 slice 1 are merged, and there is now a server.** 1134 tests
pass; ruff, ruff format and mypy strict are clean. **57 of 59 issues are resolved.**

**IT IS RUNNING.** `https://api.arkvane.in` serves the API over a valid Let's Encrypt
certificate, and `/ready` answers:

```json
{"ready":true,"checks":{"database":"ok","worker":"ok","worker_seen_seconds_ago":1}}
```

All four containers healthy, all seven migrations applied, all five worker tasks cycling, logs
as one JSON object per line. **`SEND_MODE=disabled`, verified inside the running container.**
Nothing has ever been sent.

**Phase 9 slice 1 (this session): the system can be deployed, and it can be watched.**

Everything before this ran on one laptop. The hosted dashboard was pointing at an API that
existed nowhere else, which is why it showed nothing.

- **`Dockerfile`** — one image, two commands. The api and the worker are the same code, so they
  cannot drift apart: a deploy updates both or neither, and a schema the api expects is a schema
  the worker has. Non-root (uid 10001), multi-stage, dependencies from `uv.lock` with `--locked`
  so a build in six months installs the same packages. Built and smoke-tested locally **and** in
  CI, because a Dockerfile that is only exercised on the VM is a Dockerfile first tested during a
  deploy.
- **`deploy/compose.prod.yml`** — Postgres, api, worker, Caddy. Postgres publishes **no port at
  all**; it is reachable only on the Compose network, and a dump is taken by running `pg_dump`
  inside that network. Memory limits on all four, because on a small VM the alternative to a
  limit is the kernel choosing what to kill. Log rotation on all four, because a full disk stops
  Postgres rather than just the logging.
- **`deploy/Caddyfile`** — TLS obtained and renewed by Caddy itself, no certbot to forget. It
  strips any `X-Forwarded-For` a caller sent, and the api trusts the header only from the Compose
  network: without that pair, the address-keyed rate limit is an honour system.
- **`deploy/deploy.sh`** — refuses a dirty tree, migrates *before* starting the new containers,
  waits for `/health`, and puts the previous image back if the api never answers. **It cannot
  touch `SEND_MODE` and cannot run `down -v`**, and CI asserts both of those by grep.
- **`deploy/backup.sh`** — nightly dump, 14 days, verified readable, written to a temporary name
  and renamed only on success. ADR-001 put Postgres on the VM, which removed five problems and
  introduced one: the database dies with the box. The weekly off-box copy is the mitigation, and
  it is manual.
- **`docs/DEPLOY.md`** — the runbook. Rewritten after the fact against the AWS instance that now
  exists, so §1 records what was actually done and found rather than what was planned.

**Structured logging (E7).** `app/core/logging.py`: one JSON object per line, human-readable in
development. The decision worth recording is that **context travels in `contextvars`, not in
arguments** — the api binds `request_id` once per request, the runner binds `job_id`, `org_id`
and `lead_id` once per job, and every line written underneath carries them without the emitting
code knowing they exist. A request id that has to be passed down in order to be logged is a
request id that will be missing from the one line that mattered.

Secrets are scrubbed from every rendered line as a *second* defence. The first is not logging
them; this one exists because secrets arrive in logs by accident, inside the text of an exception
raised by a driver that was handed a URL with a password in it. The tests pass a secret through
every path it would really travel: the message, a format argument, an `extra` field, and an
exception.

**`/ready` now fails when the worker is dead (ADR-009).** It checked only the database until now,
so **an api serving perfectly while the worker was dead reported itself ready** — and the symptom
of a dead worker is that nothing is researched, drafted or sent, which is invisible from outside
for hours. The worker upserts a `worker_heartbeats` row once a minute and `/ready` fails on its
**age**. `health.py`'s argument for keeping health in memory still holds; it is about the liveness
signal, and the api simply cannot see another process's memory. Point an external monitor at
`/ready`, never `/health`.

**Alerts (`alerts` table plus an optional webhook).** The table is the record, the webhook is the
nudge, in that order — an alert that exists only inside a failed HTTP request is an alert nobody
will ever see, and a push failure never propagates, or the system stops working *because* it
tried to say it was in trouble. One hour's cool-down per kind; acknowledging re-arms it. The
worker raises `worker_task_stalled` and `worker_task_failing:<task>` about itself, after **three**
consecutive failures rather than one, because alerting on blips trains whoever receives them to
ignore them.

**Two deliberate deviations from ADR-004**, both recorded in ADR-009: `worker_heartbeats` carries
no `org_id`, and `alerts.org_id` is nullable, meaning "the system". A process is not tenant data,
and a stalled worker stops every org's work at once — attributing it to one org would hide it
from the others.

**A near miss worth recording, because it is a hard-rule-2 incident.** A test built a bare
`Settings()`, which loaded the real `.env`, and a failing assertion then printed every configured
API key into the terminal. The test was about something else entirely and had not asked for them.
Removing the variables from `os.environ` is not enough, because `pydantic-settings` reads the file
itself — so `tests/conftest.py` now has an **autouse, session-scoped `hermetic_settings`** fixture
that detaches `Settings` from `.env` for the whole run. A test that forgets `_env_file=None` is
hermetic anyway. The stale `settings_factory` fixture, which still imported the deleted
`config.config`, was fixed at the same time.

**C6 and D5 close here.** ADR-006 had already settled the account — paid Google Workspace on a
dedicated domain, where SMTP relay is supported and ~2000/day leaves real headroom over our 100 —
and `DEPLOY.md` §6 is the runbook it promised: the SPF, DKIM and DMARC records as GoDaddy's panel
wants them (the apex is `@`, and the DKIM name is `google._domainkey`, not the full hostname), the
two steps everyone forgets (press **Start authentication**, and never add a second SPF record),
and a verify-as-received gate before the first send.

**Six dependencies deleted.** `langchain`, `langchain-core`, `langchain-groq`, `langgraph`, `rich`
and `aiosqlite` went with the prototype in Phase 7 but stayed in `pyproject.toml`. Nothing in
`app/` imports them. They are gone now rather than later because an image that ships six unused
dependency trees ships their vulnerabilities too. The suite is green afterwards.

**The server, as provisioned this session.** Full detail in `docs/DEPLOY.md` §1; the facts worth
having in front of you:

- `t3.small`, Ubuntu 24.04.4, 2 GB RAM, 30 GiB, Elastic IP **3.25.23.191**, SSH as `ubuntu`.
- **`api.arkvane.in` → 3.25.23.191**, an A record at GoDaddy with a 600-second TTL, resolving on
  both Google and Cloudflare public resolvers. The domain is **arkvane.in** — an earlier draft
  guessed `athenaedu.in` from the Message-ID in ADR-002's addendum, which was a test mailbox.
- **`arkvane.in`'s mail records were already correct and were not touched**: Google Workspace MX,
  exactly one SPF record, a 2048-bit DKIM key, and DMARC at `p=none` with reporting. So D5's
  runbook (`DEPLOY.md` §6) is now a verification step rather than a setup step. The zone went
  from 16 records to 17 and every mail record was re-checked afterwards.
- **Sydney, not Mumbai, and that was forced.** The AWS Free Plan pins a project to one region and
  this account's is `ap-southeast-2`; the console refuses Mumbai outright. Reaching Mumbai means
  activating advanced features, which changes billing, so it was not taken. The cost is ~150–200 ms
  of latency that only the dashboard notices.
- **Outbound 587 and 993 are open; 25 is blocked.** This was the one thing that could have stopped
  the whole deployment, and it was checked on the instance rather than assumed. We send on 587.
- **`t3.small` is not free forever** — roughly $19/month in Sydney once the plan's credits run
  out. A €4–5 Hetzner box is the better long-term home and nothing here is AWS-specific.
- A read-only, repository-scoped **deploy key** was added to the repo (Settings → Deploy keys) so
  the private repository can be cloned without a personal token. Revoking it is one click.
- A **$5 monthly budget alarm** (`outreach-automation-5usd`) emails at 85%, 100% and forecast-100%.

**What the first real deploy cost, and what it found.** Nothing in Phase 9 had ever been run
against a server, and six defects surfaced in a row. Every one of them was invisible locally and
in CI, which is the point worth keeping:

1. **`env_file` marked `required: false`** — so a missing secrets file was a *silent* success.
   Added so CI could validate without a `.env`; it turned the one failure that must be loud into
   the one that was quietest. CI now writes a throwaway `.env` and then asserts the variables
   **reach the container**, because a config that merely parses proves nothing.
2. **Migrations ran with `--no-deps`**, before Postgres existed, and died on `name resolution` —
   which reads like a DNS fault and is a startup-order one. The compose file already declared
   `depends_on: service_healthy`; the script was overriding it.
3. **Caddy crash-looped**: `log` inside a `handle` block is not an ordered handler, so Caddy
   refused the whole config. Access logging moved to site level, `log_skip` on the health paths.
4. **`ACME_EMAIL` never reached Caddy** — its `environment:` block simply did not list it — so
   Caddy used its `admin@localhost` fallback and Let's Encrypt refused it: *"contact email has
   invalid domain"*. No certificate was issued and every HTTPS request failed with a TLS internal
   error, which looks nothing like a missing variable. The fallback is gone and compose now
   requires the value.
5. **`deploy.sh` polled the api on the host**, which publishes no port — only Caddy does. It
   reported a perfectly healthy stack as a failure and rolled it back while the api logged
   "Application startup complete". Health is now checked inside the container.
6. **`/openapi.json` was open in production** while `/docs` was closed — the same map of every
   route and schema, in a more convenient format, unauthenticated.

**And two of my own diagnoses were wrong before the right one.** I twice "fixed" the `env_file`
path, in opposite directions, on partial evidence. The rule is simply that every relative path in
a compose file resolves against the **project directory**, which defaults to the compose file's
own directory — so `../.env` and `context: ..` both already meant the repository root, and
passing `--project-directory` to force it broke both at once. The compose file now says so, and
says not to add the flag back.

**Phase 9 slice 2 — privacy and retention (D6) — done.** The last issue outside Phase 8.

- **`DELETE /api/v1/leads/{id}`** erases a lead with its drafts, messages, research and events.
  Admin-only: it is the single irreversible action in the API.
- **It leaves a hashed suppression behind**, and that is the decision worth knowing about. A lead
  who opted out and then asks to be erased is a conflict: honouring the erasure literally deletes
  the record of their opt-out, and the next import of the same sheet mails them again — a privacy
  request producing the exact harm it was meant to prevent. So the address is kept as a SHA-256
  digest: enough to recognise it if offered again, not enough to read out of the database. The
  send-time check learned the hashed form in the same change, or the whole thing would be
  decorative.
- **Honest about what that buys.** A hash is pseudonymisation, not anonymisation — an email
  address has little entropy, so anyone with this table and a candidate address can confirm a
  match. `PRIVACY.md` says exactly that rather than implying more.
- **Retention** runs hourly in the `Scheduler`: terminal leads erased after
  `LEAD_RETENTION_MONTHS` (12, Q7's default), research page text cleared after 30 days, findings
  and sources kept. Leads still in conversation are never aged out — retention removes finished
  work, and somebody mid-thread is not finished.
- **`RETENTION_ENABLED` is deliberately excluded from the empty-value-means-false validator.**
  For the other flags "unset means off" is the safe reading; for this one it would quietly stop
  the system forgetting anything.
- **`PRIVACY.md` rewritten** against what the code does — the PII routing rule and its limits,
  the processor list, and the two gaps that remain: no postal address, and erasure does not reach
  backups already taken. Anyone promising otherwise is describing a system that takes no backups.

**A mistake worth recording.** Half of this work was written, then destroyed by a `git reset
--hard` after I switched branches with it uncommitted. Untracked files survived; edits to tracked
files did not. It was redone from the same reasoning, but the lesson is cheap to write down and
expensive to relearn: commit before switching, even mid-task.

**Phase 8: a campaign can now be created.** Found by opening the deployed dashboard — it signed
in correctly, said "No campaigns yet", and offered no way to make one. There was no `POST` route
for a campaign or a mailbox anywhere, and no script either, so import, research, drafting and
sending were all gated behind a record that could not exist. **The system was deployed and
unusable, and only looking at it would have shown that.**

`POST /campaigns` validates the persona at the door rather than at drafting time, because the
persona is the list of claims a draft may make about us and a malformed one would quietly produce
a weaker draft. `POST /mailboxes` stores policy and never credentials. Four bugs came out of
building it, each caught by its own test: a 422 answering 500 because Pydantic's raw error dicts
are not JSON-serialisable; `daily_cap or 100` turning an explicit 0 into 100; both endpoints
flushing without committing; and `api.js` flattening a structured error into `[object Object]`,
discarding the field list the form exists to show.

**The pipeline runs end to end on the live system.** Four imported leads went
import -> research -> draft -> review queue, and the dashboard shows real grounded drafts with
their evidence beside them. Getting there took **six** defects, every one invisible to the test
suite and to CI, and all found by watching real leads fail to move:

1. **`parse_chain` returned an empty chain when `LLM_CHAIN` was unset** — the ordinary case, and
   the one `settings.py` documents as using the default. `default_chain()` existed and was never
   called. The system had no providers at all while every key was correctly set. *The existing
   test asserted the broken behaviour.*
2. **Every model id in the default chain had been retired.** Groq's two Llama models, both
   OpenRouter `:free` slugs and `gemini-2.0-flash` all answered 404. Replaced with ids read from
   each provider's own model list and verified to return content.
3. **`NEW -> RESEARCHED` is not a legal transition** and nothing ever set `ENRICHING`, so every
   research job did all its work and died on the final line.
4. **Research never queued drafting.** `DRAFT_LEAD` was a registered handler with a concurrency
   cap, enqueued by nobody. The pipeline stopped with everything looking healthy.
5. **Strict structured output needs `additionalProperties: false` on every object *and*
   `required` naming every property.** Pydantic emits neither for nested models, so both Groq
   entries were disabled with a 400 on every structured call. A simplified schema worked, which
   is exactly why this took two rounds to see — the real schema had to be posted.
6. **`RESEARCHED -> AWAITING_APPROVAL` is not legal either**, so drafting failed *after* paying
   for the model and storing the draft.

**Three of those six were the same shape: a correct table and a correct caller, with nothing
checking that they agreed.** Each component was individually tested and individually right. The
lifecycle test now walks the whole path — `NEW -> ENRICHING -> RESEARCHED -> DRAFTED ->
AWAITING_APPROVAL -> APPROVED -> SENT` — and research is asserted to queue drafting, so the next
gap of that kind fails in CI instead of on a server.

**A provider fix worth keeping in mind:** one free model answers a strict `json_schema` request
with correct JSON wrapped in a markdown fence. That was being treated as the model failing, which
cooled down an entry that had done the right thing.

**Phase 9 (this session): the first attempt to send stopped on three things, none of which the
test suite could have caught.** All three were state in the production database, not code.

1. **`campaigns.settings` was edited by hand and the whole send loop died.** A widened send
   window was applied with `send_days` as `["mon","tue",...]`, where `CampaignSettings` wants
   integers (0 = Monday). `model_validate` raised inside `SendLoop._send_for`, the error escaped
   `tick()`, and `WorkerTask.run` recorded a failure and backed off — then failed again every
   cycle. **Nothing sent from any mailbox**, and the only evidence was a traceback in the worker
   log. The dashboard showed four healthy leads and no reason at all.

   Fixed in PR #52: validation happens inside a `try`, a failure becomes *that mailbox's* reason
   naming the offending fields, and the other mailboxes still get their turn. Field names only,
   never the stored values — a reason string reaches logs and `/admin`, and echoing stored
   content into either is how one eventually leaks.

2. **The campaign was still `draft`.** `status` defaults to `draft` on creation and
   `find_candidates` requires `running`, so an attached mailbox and two approved drafts sent
   nothing. The UI *printed* the status and said the campaign "can send once you set it
   running" — and offered no way to set it. The only route was raw SQL against production. PR
   #52 adds the Set running / Pause sending control, and an explicit line while a campaign is
   still a draft.

3. **The mailbox's `from_email` is not the authenticated SMTP account.** The row says
   `rahul@arkvane.in`; SMTP authenticates as a personal Gmail, and `aiosmtplib.send` takes the
   envelope sender from the `From` header. Probed against Gmail (conversation stopped at
   `RCPT TO`, no `DATA`, nothing delivered): **Gmail accepts the mismatched envelope**, so
   there is no rejection and no risk of `_bounce_lead` suppressing a real address. But Gmail
   will not sign `arkvane.in` mail, so **the SPF and DKIM passes verified for `arkvane.in` do
   not apply to anything sent this way.** Fine for a workflow test to the owner's own addresses;
   it must be corrected before real outreach.

**What this says about the test suite.** 1,100 tests, four green CI jobs, and a deploy that
reports `{"ready":true}` — none of it could see any of the three, because all three were rows.
The pattern is the same one that produced the six defects of the previous session: a correct
table and a correct caller, with nothing checking that the data between them agreed. The fix
that generalises is the one in PR #52 — make the unreadable case *reportable* rather than fatal,
so the next malformed row costs one mailbox and says so, instead of costing everything silently.

**A deploy defect fixed on the way:** `/opt/outreach/.git/config` had
`core.sshCommand = ssh -i ~/.ssh/outreach_deploy`. `~` is `/root` under `sudo`, where no such
key exists, so `deploy/deploy.sh` could never pull — every deploy so far had been done by hand.
Now an absolute path with `IdentitiesOnly=yes` and an explicit `UserKnownHostsFile`. `deploy.sh`
runs end to end; the live image is `dbc92e2`.

**Still blocking the first send, and it needs the owner.** Two rows, and the permission guard
refuses to let a tool `UPDATE` the production database:

```sql
-- 1. correct the settings this session's own bad UPDATE wrote (0 = Monday)
UPDATE app.campaigns SET settings = settings ||
  '{"send_days":[0,1,2,3,4,5,6],"window_start":"00:00:00","window_end":"23:59:00"}'::jsonb
  WHERE name = 'test_campaign';
-- 2. or just drop it back to the product default: Mon-Fri 10:00-17:30 IST
--    UPDATE app.campaigns SET settings = '{}'::jsonb WHERE name = 'test_campaign';
```

Step 2 — setting the campaign `running` — no longer needs SQL. It is a button in the dashboard
once the Cloudflare build of `main` lands.

**Next up.**
1. ~~**Phase 9 slice 2 — privacy and retention (D6).**~~ Done, above. `DELETE /leads/{id}` as a real erasure, the
   retention sweep in the `Scheduler` (Q7's default is 12 months), and a `PRIVACY.md` that
   describes what this system actually does with a lead's data — including that public page text
   goes to providers marked `allow_pii=false` and a lead's name, address and reply never do.
   **D6 is the last issue outside Phase 8.**
2. **Phase 8 remainder:** TypeScript and TanStack Query (the target in `CLAUDE.md`; this stayed
   in plain JS to keep each change reviewable), persona *editing* (creation now exists), lead
   detail with its timeline, bulk actions, and filters. **E8** and **NEW-3** close with those.
   There is still no way to delete a campaign, which is why no test campaign was created on the
   live system.
3. **Phase 10:** rehearsal in `SEND_MODE=allowlist`, then the §12 go-live gate.

**Still outstanding for the owner.**
- **Set the Cloudflare build variables** so the dashboard can reach the API that is now live:
  `VITE_API_URL=https://api.arkvane.in`, plus `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.
  Then redeploy the Cloudflare project. Without `VITE_API_URL` the dashboard asks its own origin
  for `/api/v1`, gets `index.html` with a 200, and shows the `<!DOCTYPE` error.
- **Create the first `members` row** (`docs/DEPLOY.md` §4.1). Access is a database row: a valid
  Supabase token with no membership gets no org and therefore no data, by design. This needs the
  `sub` of your Supabase user, which only you can read from that dashboard.
- **Confirm DKIM is actually signing.** The record is published but DNS cannot show whether
  Google is using it — send one message from the arkvane.in mailbox to a Gmail address and check
  **Show original** for `DKIM: PASS`. If it fails, press **Start authentication** in the
  Workspace admin console. This gates the first real send.
- **`SMTP_EMAIL` must be an `@arkvane.in` address.** SPF, DKIM and DMARC protect the domain in
  the From header, and those records are on `arkvane.in`. If the sending mailbox is still the
  `@athenaedu.in` one used in testing, mail goes out unauthenticated against a domain with no
  matching records, which is worse than no setup at all.
- **Q2 — the postal address in the email footer. Answered 2026-10-11: proceed without it for now,
  the owner will supply one later.** So the footer ships without a postal address and
  the gap is carried in `QUESTIONS.md` Q2 until Phase 10's `COMPLIANCE.md` records it: no postal
  address weakens CAN-SPAM posture for any **US** recipient, and the audience is India-only, which
  is why it is tolerable meanwhile. Revisit before the audience widens.
- **API keys will be rotated by the owner later** (after the `.env` exposure recorded above). Not
  blocking anything; worth doing before go-live.
- Set `VITE_API_URL`, `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY` as Cloudflare **build**
  variables, then redeploy. Without `VITE_API_URL` the dashboard asks its own origin for
  `/api/v1`, gets `index.html` with a 200, and shows the `<!DOCTYPE` error.
- Dependabot PR **#4** (React 19 / Vite 8) left open deliberately — a major framework bump.
- **One** `.claude/settings.json` fix in `QUESTIONS.md` **Q-0**: the `curl` deny entry, confirmed
  by test to enforce nothing.
- Open questions for go-live: **Q2** (footer address), **Q5** (Indian holidays), **Q7** (retention
  — defaulting to 12 months in slice 2 unless told otherwise).

**Live environment notes.** Python 3.12 venv at `.venv`; dependencies from `pyproject.toml` plus
`uv.lock` (`uv sync --all-groups`). Postgres in Compose on **port 5433** (ADR-005), container
`outreach-pg`. Docker Desktop must be running for the 342 integration tests; without it they skip
rather than fail. `gh` is authenticated as `Harishchand83077`; the repo is private. `.env` is
populated and verified with `python -m app.scripts.check_env`, `SEND_MODE=disabled`. IMAP and SMTP
are both verified against the real mailbox.

**Note on local `main`.** Merges are squashed, so local `main` diverges from `origin/main`. Create
new branches from `origin/main`.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| §00 | Bootstrap, tooling, permissions | **Done** |
| §0 | Repo analysis, issue verification, baseline | **Done** |
| 0 | Safety patch + CI + docs | **Done** |
| 1 | Data layer, state machine, job queue, outbox | **Done** — 213 tests |
| 2 | Import, persona, campaigns | **Done** — 318 tests |
| 3 | LLM router, research engine | **Done** - 539 tests |
| 4 | Drafting, grounding, review queue | **Done** - 633 tests |
| 5 | Send engine | **Done** - 757 tests. The first allowlist send gate is now runnable. |
| 6 | Reply engine, follow-ups | **Done** - 952 tests. Poller, classifier, actions, reply drafter, ingest loop, ADR-002 recovery. |
| 6c | Worker process | **Done** - 980 tests. The five asyncio tasks of §4, `python -m app.worker`. |
| 7 | Auth, multi-tenancy | **Done** - 1026 tests. Auth, tenancy, CORS, rate limits, pagination, campaigns, leads, review, imports, suppressions, mailbox, admin. **The legacy `api_server.py` is deleted**, and with it nine issues that described it. |
| 8 | UI completion | **In progress** - dashboard rewired to `/api/v1` with Supabase magic-link login; 12 frontend tests. TypeScript, TanStack Query, campaign editing, lead detail and bulk actions remain (E8, NEW-3). |
| 9 | Ops: Docker, deploy, logs, alerts, backups, privacy | **Done** - 1134 tests. Dockerfile, production Compose, Caddy, `deploy.sh`, `backup.sh`, `docs/DEPLOY.md`, JSON logs with request and job ids, the `alerts` table and webhook, `/ready` wired to the worker heartbeat (ADR-009), and per-lead erasure with an hourly retention sweep. **Deployed and serving at `https://api.arkvane.in`.** Closes C6, D5, D6, E7. |
| 10 | Rehearsal, go-live gate | Not started |

## Issue register

**59 tracked issues**: 51 lettered IDs (A1–A8, B1–B18, C1–C8, D1–D6, E1–E8, F1–F3) plus NEW-1…8.
Every one must end `Done`, `Superseded-by-rewrite` (with a test proving the behaviour) or
`Won't fix` (with a reason). The counts below are computed from `ISSUES.md`, which holds the full
text, the evidence and the test that proves each resolution.

| Status | Count | IDs |
|---|---|---|
| **Superseded-by-rewrite** (each with a test) | 38 | A1-A8, B5, B7-B18, C1, C3, C4, C5, C7, D1-D4, E1-E5, NEW-4, NEW-6, NEW-7 |
| **Done** | 17 | B1, B2, B3, B4, B6, C2, C6, C8, D5, E6, E7, F1, F2, F3, NEW-1, NEW-2, NEW-5 |
| Partly | 1 | NEW-8 (`.xls` rejected and `.csv` accepted; the docstring went with the F1-F3 rewrite) |
| Verified, not yet fixed | 3 | D6, E8, NEW-3 |

**56 of 59 resolved.** What remains is one phase each: **D6** is Phase 9 slice 2 (per-lead
erasure, the retention sweep, an honest `PRIVACY.md`), and **E8** and **NEW-3** are Phase 8 polish.

**The safety guarantees, and where each one is proved now.** This table was rewritten in Phase 9
because PR #26 deleted the prototype *and* the three test files three of these rows cited —
`test_error_text_never_emailed.py`, `test_reply_never_autosends.py` and `test_upload_security.py`.
The guarantees survived the deletion; the citations did not. A guarantee whose test no longer
exists is a claim, so each row below was traced to a test that is in the suite today, and the one
that had no replacement — C2's — was written in Phase 9 rather than quietly dropped.

| Guarantee | Proved by |
|---|---|
| Nothing reaches SMTP unless one function permits it, and nothing else decides on `SEND_MODE` | `tests/unit/test_send_guard.py` (including the structural test over the whole tree) |
| `SEND_MODE=disabled` is the default and blocks every recipient, allowlisted or not | `test_send_guard.py::test_default_mode_blocks`, `::test_disabled_blocks_every_recipient` |
| An unlisted recipient is refused before a socket opens, and the SMTP server receives nothing | `test_send_guard.py::test_unlisted_recipient_is_blocked_and_nothing_is_received` |
| A blocked or failed send is never recorded as sent | `tests/integration/test_send_loop.py`, `tests/integration/test_send_recovery.py` |
| Placeholder credentials stop startup, and nothing reports itself configured when it is not | `tests/unit/test_settings.py::TestPlaceholderRejection` |
| A stale `DRY_RUN_EMAILS` in the environment stops startup rather than being ignored | `tests/unit/test_settings.py::test_dry_run_emails_in_env_is_rejected` |
| An LLM exception or a rejected draft can never become email content | `tests/unit/test_drafting_generator.py::TestFailureHandling`, `::TestTheClaimCheck` |
| A reply can never trigger a send, and `MEETING_BOOKED` is never inferred from one | `tests/unit/test_reply_rules.py::test_a_hostile_reply_cannot_reach_meeting_booked`, `tests/unit/test_state_machine.py::test_meeting_booked_is_never_reached_from_a_machine_state`, and the structural test that the review router imports nothing that can send |
| An uploaded filename can never reach the filesystem | `tests/integration/test_api_operations.py::test_a_traversing_filename_never_reaches_the_filesystem` (**written in Phase 9** to replace the deleted `test_upload_security.py`) |
| No test reads the developer's `.env`, so a failing assertion cannot print a real key | `tests/conftest.py::hermetic_settings` (autouse) |
| A worker that has died cannot report itself ready | `tests/integration/test_ops.py::TestReady` |

## PR log

| PR | Branch | Issues | Status |
|---|---|---|---|
| — | — | — | none opened yet |
