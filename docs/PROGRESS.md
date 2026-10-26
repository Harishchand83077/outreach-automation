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

**The first real emails were sent, and three more defects came out of it.** Three leads of
`test_campaign` went out on `SEND_MODE=allowlist` to the owner's own addresses, were delivered,
and are recorded `SENT`. Every defect below was invisible to the suite and to CI, and every one
was found by watching real leads.

**1. A delivered email was not recorded as sent (PR #54).** The worst of the three. The first
send arrived in the recipient's inbox and the row still said `sending`; the lead stayed
`APPROVED` and the dashboard's SENT filter was empty for a message somebody had already read.

`send_one` commits twice of its own accord -- once before the SMTP call, because a socket must
not be held open inside a transaction, and once after, because step 4 has to land whatever the
outcome was. `SendLoop._send_for` wrapped that call in `session.begin()`, and committing inside
that block ends it, so the next statement raised `InvalidRequestError: Can't operate on closed
transaction` **after** the mail had left.

- **At-most-once held.** The unique idempotency key refused the next cycle's claim, so nothing
  was sent twice. **ADR-002's recovery pass then earned its keep**: it found each message in
  `[Gmail]/Sent Mail` by `Message-ID` and resolved it to `sent`, 15 minutes later. The system
  self-healed a bug nobody had found yet, which is the entire argument for that design.
- **Why 1134 tests missed it:** every test in `test_send_loop.py` called `send_one` with the
  bare `db` fixture, which has no outer transaction -- so the one thing that differed between
  the test and the worker was the thing that was broken. `TestThroughTheRealTask` now drives
  `send_pass` and `tick`, the methods the worker actually calls.

**2. One unreadable campaign stopped every mailbox (PR #52).** A send window widened by hand
put `send_days` as `["mon","tue",...]` where `CampaignSettings` wants integers (0 = Monday).
`model_validate` raised inside `_send_for`, escaped `tick()`, and the loop crash-looped --
nothing sent from **any** mailbox, with the reason visible only as a traceback. Validation is
now per-mailbox and a failure is reported as that mailbox's reason, naming the offending fields.

**3. The dashboard could not start a campaign, or create a second one (PRs #52, #55).**
`status` defaults to `draft` and `find_candidates` requires `running`, so an attached mailbox
and two approved drafts sent nothing and said nothing. The UI printed the status and told the
operator to "set it running" -- with no control to do it. `NewCampaign` also rendered only when
no campaign was selected, so the moment a first campaign existed there was no way to make
another.

**The settings screen (PR #55), and the one thing it deliberately will not do.** Sending rules
were editable only by raw SQL against production, which is how defect 2 happened.
`PATCH /campaigns/{id}` now accepts `settings`, validated through `CampaignSettings` and merged
rather than replaced. A test asserts the property that matters: whatever the route accepts, the
send loop can read.

**`SEND_MODE` is reported and never editable.** `GET /admin/sending-config` answers "what
happens if I approve this draft?" from the same configuration object the send guard reads, and
there is no route that sets it -- it comes from the environment at process start and is checked
in one function (hard rule 1), so going live stays a deliberate act on the server rather than a
click a stolen session could make. A test asserts no route can change it. The allowlist is
reported as a **count**: the count proves the gate is configured, the addresses are personal
data that would end up in a browser cache and a screenshot.

**Three operational findings worth keeping.**

- **The mailbox's `from_email` is not the authenticated SMTP account.** The row says
  `rahul@arkvane.in`; SMTP authenticates as a personal Gmail. Probed against Gmail with the
  conversation stopped at `RCPT TO` -- no `DATA`, nothing delivered -- and **Gmail accepts the
  mismatched envelope**, so there is no rejection and no risk of `_bounce_lead` suppressing a
  real address. But Gmail will not sign `arkvane.in` mail, so **the SPF and DKIM passes verified
  for `arkvane.in` do not apply to anything sent this way.** Fine for a test to the owner's own
  inbox; it must be corrected before real outreach.
- **The IMAP loop ingested 1400 messages from the personal inbox, and classified none of them.**
  Zero matched a lead, so the `In-Reply-To`/`References` matching did its job and no model was
  called on unrelated mail -- 32 LLM calls in total. It is noise rather than cost, but 1400 rows
  of the owner's personal email now sit in the database, which is a reason to use a dedicated
  mailbox rather than a personal one.
- **`deploy.sh` could never pull.** `/opt/outreach/.git/config` set
  `core.sshCommand = ssh -i ~/.ssh/outreach_deploy`, and `~` is `/root` under `sudo`, where no
  such key exists -- so every deploy up to this point had been done by hand. Now an absolute
  path with `IdentitiesOnly=yes`. `deploy.sh` runs end to end.

**The reboot was not unattended-upgrades.** The journal says
`systemd-logind: Power key pressed short` -- an ACPI power-button event from the hypervisor,
which on EC2 means a Reboot or Stop issued from the console or API, most likely while the IAM
role was being attached (which does not require a restart). Every `Automatic-Reboot` directive
in `/etc/apt/apt.conf.d/` is commented out, so unattended-upgrades was not responsible and
nothing needed changing. Recovery already works and was not luck: `docker` and `containerd` are
`enabled` at boot and all four containers are `unless-stopped`, so the stack came back by itself
in about 60 seconds. `HandlePowerKey` is deliberately **left alone** -- ignoring it would turn a
graceful console shutdown into a hard stop, which is worse for a box holding a database.

**SSM Session Manager is live.** The instance profile `outreach-ssm-role` is attached and the
metadata service confirms it, so a shell is available from any network without an SSH rule.
Port 22 can now be closed; the dynamic residential IP that broke SSH three times stops
mattering.

**Phase 8: the dashboard is navigable, and the three things that made it unusable are fixed
(PR #57).** 1233 tests. The previous version was one long scroll of every section at once,
styled with inline objects. Each change below is a layout decision rather than a cosmetic one.

- **A reviewer cannot be handed a hundred drafts.** Each carries a subject, a body and its
  research hooks, so a hundred is a response measured in hundreds of kilobytes.
  `GET /review/drafts` now takes a cursor and returns **`total` as well as `next_cursor`** --
  the cursor answers "is there more", which is not the question a reviewer has; "47 waiting" is
  what decides whether somebody starts now. Ordered oldest first **with `id` breaking the tie**,
  or two drafts created in the same millisecond page unstably and one is served twice while the
  other is never seen. Judging a draft refills the page from the same cursor rather than
  advancing, because advancing would skip whatever moved up into the gap.
- **An operator with a thousand leads cannot scroll to find one.** Email is unique so it gets a
  search box; `name`, `company` and `state` are separate filters combining as AND. They were one
  combined `q`, which cannot tell you whether it matched a person or a company. **`%` and `_`
  are now escaped** -- unescaped, searching for `%` returns the whole table, and that is the
  first character somebody types when probing a search box.
- **Settings are gated by role, not by a hidden button.** `member` reads the dashboard and
  judges drafts but cannot change the rules underneath them. `GET /admin/sending-config` and
  `DELETE /suppressions/{id}` now require `admin`. Removing a suppression undoes somebody's
  opt-out, which is the one action there that can lead to mailing a person who asked not to be;
  *adding* is protective and stays open to anyone. A test asserts a member can still review --
  the gate is on the rules, not on the work.

`frontend/src/app.css` replaces the inline `style` objects. Inline styles cannot express a
hover, a focus ring, a media query or a disabled state, and the absence of those four was most
of what made the old screen feel unfinished. The sidebar collapses to a horizontal strip under
900px rather than becoming a drawer, because a drawer needs a focus trap and a strip needs
nothing.

**Outstanding and blocked: making `SEND_MODE` editable from the dashboard.** The owner asked for
it twice. It is designed and the migration is written, and it is **not applied**, because the
permission guard refuses to let a tool weaken the send gate on its own authority -- correctly,
given hard rule 1. The design, for whoever picks it up:

- `orgs.send_mode`, nullable, with a check constraint. NULL means "use the environment", so
  adding the column changes no org's behaviour.
- `effective_send_mode(settings, override)` in `app/services/mail/smtp.py` -- the *same module*
  as the gate, so the decision still resolves in one place and the structural test over the
  whole tree still holds.
- Raising it is gated where it is **written**, not where it is read: `owner` only, `live`
  refused unless `APP_ENV == "production"`, a typed confirmation, and an audit row naming who
  did it. A guard that answers differently depending on who is asking is a guard nobody can
  reason about.
- This needs an **ADR** and an edit to hard rule 1 in `CLAUDE.md`, because it is a deliberate
  deviation from what that rule currently says.

**The SSH problem is not IP rotation, or not only that.** This session's failure was the
*machine* switching to IPv6 (`2401:4900:...`) while the security group holds an IPv4 `/32`;
forcing `ssh -4` showed the IPv4 address had also rotated inside the ISP's `/20`
(`182.69.182.117` to `182.69.183.128`). **SSM Session Manager is attached and confirmed**
(`outreach-ssm-role`, verified through the instance metadata service), so the fix is to stop
using SSH: EC2 -> Connect -> Session Manager works from any address and any address family, and
port 22 can be closed entirely. `deploy/deploy.sh` runs the same way inside that session.

**So PR #57's API half is merged but not deployed.** The frontend went out through Cloudflare on
the merge to `main` and is live -- the published CSS hash matches the local build and the bundle
contains the new shell -- but the server is still running the previous image, which means the
new UI is calling `/review/drafts` without cursor support and the lead filters are being
ignored. **Deploying is one `sudo ./deploy/deploy.sh` through Session Manager.**

**The send mode is now settable from the dashboard, owner-only (PR #59, ADR-010).** Deployed as
`outreach:51d4ace`; migration `b4f1a7c93d52` applied and verified against the live database.

The mode came only from the environment and was read once at process start. Safe, and unusable
in the situation that matters most: stopping. Pausing an in-flight campaign meant an SSH
session, an editor and a container restart -- and **SSH broke four times during this project**,
three IP rotations on a residential line and once because the machine switched to IPv6 while the
security group held an IPv4 `/32`. A kill switch that depends on SSH being reachable is not a
kill switch.

- `orgs.send_mode`, nullable, with a check constraint. **NULL means "use the environment"**, so
  the migration changed no behaviour. A column with a default would have silently become the
  authority the moment it was added. Verified on the live database afterwards: `send_mode` is
  NULL, `SEND_MODE=allowlist`, `APP_ENV=production`.
- **The one-gate invariant is kept.** `effective_send_mode()` sits in the same module as
  `allowlist_permits`, so the decision still resolves in one place and the structural test over
  the whole tree still passes. An unknown value resolves to `disabled`; the check constraint
  makes that unreachable, which is why it must fail closed.
- **Every gate is on the write, not the read**, because a guard that answers differently
  depending on who is asking is a guard nobody can reason about: `owner` not `admin`,
  `APP_ENV == "production"` for `live`, a typed `confirm: "LIVE"`, and an audit row with a
  WARNING log line.
- **Enabling is harder than it was**; restricting is instant and needs no shell. Before, anybody
  with shell access and an editor could set `live` in `.env`.

`CLAUDE.md` hard rule 1 was edited, since this deliberately supersedes its second half. The
first half is unchanged.

**The nightly backup had never run, and that was the most serious finding of the session.** No
`backups/` directory, no cron entry -- `deploy/backup.sh` was written in Phase 9 and never
scheduled. ADR-001 put Postgres on the VM, which removed five problems and introduced exactly
one: the database dies with the instance. The dump was the mitigation, and it did not exist. Had
the box been terminated, everything would have gone with it.

Installed at 02:17 daily with 14 days retained, and run once to prove it: 5,092,979 bytes,
verified readable. **The off-box copy is still manual and still the part that matters** -- the
dumps sit on the same disk as the database, which protects against a bad migration and not
against losing the instance.

**Where the data is, recorded once so it stops being a question.** One Postgres, in Compose on
the EC2 instance, volume `outreach_pgdata` on the 29 GB root disk, **no published port** --
reachable only on the Compose network. 22 MB today. Supabase holds **no tables**: it issues a
JWT, we verify it locally against its JWKS and resolve it to our own `members` row. The local
Postgres on port 5433 is development and tests only.

**Deploys now go through SSM Session Manager**, not SSH. The instance profile `outreach-ssm-role`
is attached and verified through the metadata service, so a shell is available from any network
and any address family. **Port 22 can be closed.** Two things were fixed to make `deploy.sh` work
under it: `/opt/outreach` is now consistently root-owned (an earlier `sudo git fetch` had left
root-owned objects in `.git`, which broke git for `ubuntu`), and `core.sshCommand` already used
an absolute key path from the previous session.

**Phase 8 is complete and the issue register is closed: 59 of 59 (PR #61).** E8 and NEW-3 were
the last two.

**NEW-3 — the headline number fell as the campaign succeeded.** The prototype counted
`emails_sent` as the number of leads whose *current* status was `EMAIL_SENT`, so every lead that
then replied or booked a meeting silently decremented it. `GET /campaigns/{id}/stats` counts
from the record instead: `emails_sent` counts **messages** (two sends to one person are two
emails; a `failed` or `sending` row is neither), and `replied`, `bounced`, `opted_out` and
`meetings_booked` count leads that **ever reached** that state from the append-only `events`
table. `by_state` is the only snapshot and the dashboard labels it "In flight right now",
because where everything is and what has happened are different questions.

**E8 — bulk approval, pausing one lead, and the timeline.** `POST /review/bulk-approve` **skips**
any draft whose validation failed, whose research was thin, or whose confidence is below the
threshold, and returns the reason keyed by draft id so the UI marks those rows. That asymmetry
is the design: the failure mode of a bulk button is somebody clearing a queue of a hundred
without reading any, and the weak ones are exactly the ones worth reading. Capped at 200.
Pausing a lead is **a flag, never a state** — a `PAUSED` state would need a way back out that
guessed what the lead was doing before. Lead detail was already in the API and the UI had never
called it; a row is now clickable and shows every message and the full audit trail, one lead at
a time (C7).

**Capacity, decided from measurement rather than estimate.** Per-row costs on the live database:
a message is 3.4 KB, a draft 24 KB, an event 0.35 KB, a research row 28 KB (page text cleared
after 30 days). At 100 emails a day — 2,200 a month, ~1,100 new leads at two sends each — a lead
costs about **34 KB permanently**, so **37 MB a month and roughly 250–300 MB over six months**.

- **Postgres stays on the VM.** Supabase free is a 500 MB database, which that fills to 50–60%
  with no headroom, and a free project **pauses after a week of inactivity**, which would stop
  the worker mid-campaign. It would also re-add the session-pooler and IPv6 workarounds ADR-001
  removed. The instance has 23 GB free after a `docker system prune` reclaimed **6.6 GB** of
  stale images (disk went from 46% to 19%).
- **`t3.small` stays.** 877 MB of 1,905 MB in use, so a `t3.micro` would not fit.
- **The credits are the real constraint.** $118.08 of $120 left with $1.92 spent in three days —
  $0.64 a day, about $19 a month, **184 days of runway**. That lands almost exactly on the six
  months, so the margin is roughly $3 and nothing paid should be added.
- **The one free-tier service worth adding is S3 for the backups**, which still sit on the same
  disk as the database. 70 MB against a 5 GB free allowance. It needs an IAM policy, which is
  the owner's click.

**Four defects found by an operator using the dashboard, three of them in the same path
(PRs #65-68).** The pattern is now unmistakable and worth stating once more: every one was a
correct caller and a correct callee with nothing checking that they agreed.

**Rejecting a draft did nothing visible, and it took three fixes to actually work.** A reviewer
rejected a draft with "add one more line about athena". The audit trail recorded the feedback,
the lead went back to `DRAFTED`, the review queue went from 1 to 0 — and no new draft appeared.

1. **Nobody enqueued the regeneration.** `drafts.reject` marked the draft rejected, stored the
   feedback and returned the lead to `DRAFTED`; its docstring said "for regeneration". The route
   recorded the rejection. Neither wrote the next draft. The job is now enqueued **inside
   `reject`, in the same transaction** — a caller can forget, and a caller added later can
   forget again; here the rejection and the work it implies either both land or neither does.
2. **The feedback never reached the model.** `build_draft_prompt` has always had a `feedback`
   slot, filled only by the generator's own validation retries. A reviewer's objection was
   stored, shown in the timeline, and ignored — so they would reject the same draft twice and
   reasonably conclude the feedback box did nothing. `handle_draft_lead` now reads the newest
   rejection for the lead and seeds the prompt with it.
3. **The regeneration then died on the state machine.** A rejection puts the lead back into
   `DRAFTED`, and `handle_draft_lead` always transitioned *to* `DRAFTED`, so the replacement
   arrived with the lead already there: `DRAFTED -> DRAFTED is not a legal transition`, raised
   after the model had been paid for and the draft written.

   **The obvious fix would have been wrong.** Allowing that means allowing self-transitions, and
   the ban on those is deliberate — `SENT -> SENT` would be a second email. The handler skips
   the redundant transition instead, and a test now locks the rule it was tempting to relax.
   `save_draft` already writes a `draft.created` event per attempt, so the timeline loses
   nothing.

Verified on the live system afterwards: version 1 `rejected`, version 2 `pending_review`, lead
back in `AWAITING_APPROVAL` — and the new draft mentions Athena, so the feedback did reach the
model.

**Creating a second campaign returned a 500, and the error message blamed the wrong thing.**
`uq_mailboxes_org_from_email` already said two rows cannot share an address within an org;
`POST /mailboxes` did not, so the second attempt came back as an unhandled
`UniqueViolationError`. It is now idempotent on the address — the address *is* the mailbox's
identity, and a 409 would only make the caller ask for the id it just described.

**The misleading message is the part worth keeping.** FastAPI handles a bare `Exception` in
Starlette's `ServerErrorMiddleware`, which sits *outside* `CORSMiddleware`, so a 500 never
passes back through it and arrives with no `Access-Control-Allow-Origin`. The browser refuses to
expose it and `fetch` rejects — so the dashboard said *"the API did not answer with JSON; check
that the backend is running"* while the backend was running perfectly and had answered with
JSON. That pointed the investigation at CORS, DNS and connectivity when the fault was a
duplicate key three layers in. The handler now adds the header itself, reflected only for an
origin already on the allowlist.

**Two campaigns on one mailbox: whose email goes first, and whose name is on it (PR #67).**
Asked as a scheduling question; the answer uncovered a correctness bug. `find_candidates` has
always pulled leads from **every** running campaign on a mailbox, while `SendLoop` handed
`send_one` a single persona taken from the *oldest* campaign — so a lead belonging to campaign B
went out **signed by campaign A**, with the wrong sender name, agency and footer, under campaign
A's window and follow-up rules. Not an ordering problem: a sender-identity one, and the kind
nobody notices until a recipient replies to the wrong person.

A `Candidate` now carries its own campaign — its persona signs the message, its settings
schedule the follow-up, and its window decides eligibility, so a campaign whose window is shut
cannot block one whose window is open.

**The ordering decision.** The daily cap belongs to the **mailbox**, because the cap protects a
sending *reputation* and reputation attaches to the address, not to how many campaigns the work
was split into. So campaigns share one allowance: replies first across every campaign (somebody
who wrote to us is waiting, and which campaign they came from is our bookkeeping), then due
follow-ups, then first emails — and within a kind, **round-robin across campaigns**, oldest
approval first inside each. Round-robin rather than strict first-approved-first-sent, because
with one cap a campaign holding five hundred approved drafts would starve one holding five for
a working week.


**It is a product now: ten people can sign up, connect their own Gmail, and run their own
campaigns (PRs #96, #97, deployed as `outreach:436f8fa`).**

**Tenancy needed no work, and that is ADR-004 paying for itself three phases later.** `org_id`
went on every table in the first migration and `deps.py` has never taken an org from a request.
The two things that were genuinely single-tenant were *who may sign in* (one `members` row
inserted by hand with SQL) and *which mailbox sends* (`SMTP_EMAIL` from the environment). Those
are what changed; the campaigns, leads, drafts, research, suppressions and the send loop's
candidate query were already scoped and already tested for it.

- **A sign-up creates its own org, owned by that person** (ADR-017), provisioned on the first
  authenticated request rather than at a `POST /signup` — a separate endpoint is a second door
  into the same room, and anything that forgets to knock on it sees a 403 it cannot explain.
  `SIGNUP_MODE` defaults to **`closed`**, which is exactly the old behaviour. The reasoning is
  ADR-010's: a migration that quietly opened public sign-up on a system that sends email would
  be the worst default this project could ship.
- **Connect Gmail is an OAuth click** (ADR-018). Scope `https://mail.google.com/` over SMTP and
  IMAP with **XOAUTH2, not the Gmail API** — that scope authorises the two protocols we already
  speak, so the send loop, outbox, IMAP poller, reply matcher and ADR-002 recovery all keep
  working. The Gmail API cannot read the Sent folder, which is the thing ADR-002 depends on.
  One function changed: how the clients authenticate.
- **The scope costs something, and it is written down rather than discovered.** Google calls
  reading a mailbox a *restricted* scope — publishing needs an annual paid security assessment,
  and hard rule 5 says no. An **unpublished** client needs none: 100 test users, any scope, free.
  The price is refresh tokens that **expire every 7 days**. Handled as a product state: the
  mailbox pauses with a reason, the dashboard says Reconnect, and the copy says it is expected.
  `MULTI_TENANT_PLAN.md` §4 has both exits past ten users and neither is a rewrite.
- **Credentials are Fernet-encrypted**, key in the environment, in their own table — `mailboxes`
  is read every send-loop cycle and a secret should not ride along. No route returns one, no log
  line holds one, and `Sealed` and `MailLogin` both override `__repr__` so a traceback cannot
  print one. **A mailbox with no credential falls back to the environment**, which is what kept
  the live deployment sending through all of this and what keeps every existing test meaningful.
- **Campaign schedules** (ADR-019): `starts_at`/`ends_at`, with the `Scheduler` moving `status`
  rather than three separate queries learning to check a clock. `paused` is never overridden, and
  the end is checked before the start so a backdated window completes instead of briefly sending.

**"Why is this lead not sending?" is answerable now, and the story is the whole lesson of this
project in one defect.** A lead sat `APPROVED` for **eight days** on the live system showing
"next action: —", which reads exactly like "waiting its turn". The real reason was that another
campaign in the same org was already in conversation with that person — the per-person cooldown
working correctly — and `claim_send` had been computing that precise sentence once a minute and
writing it to a log nobody reads.

`outbox.why_not_sendable` is that answer, and **`claim_send` calls it**. The first version I
wrote was a second copy of the same rules for the dashboard to use, which would have been the
seventh instance of this project's signature defect; it was replaced before it was committed.
`tests/integration/test_send_blockers.py` parametrises every refusal and asserts both paths
return the same sentence, so a refusal added to one without the other fails CI.

**And the answer to "why did test campaign 2 send no follow-ups" was: nothing is broken.** Its
two leads are the same two people as `test_campaign`. One replied (a reply cancels follow-ups);
the other has never had a *first* email because the org is already in conversation with them. A
campaign with nobody new to email, and no screen that could say so.

**An attachment per campaign** (ADR-016, PR #96): one file, on every first email, unticked per
draft in the review queue. Bytes in Postgres in their own table; the extension allowlist is the
only control and the UI says so, because nothing here scans file contents.

**Still outstanding, and all of it needs a Google account signed in — `docs/OWNER_SETUP.md`:**
1. **A Google Cloud OAuth client**, then five values in `.env` (`CREDENTIAL_KEY`,
   `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `DASHBOARD_URL`) plus
   `SIGNUP_MODE`. **Until those exist, Connect Gmail is drawn as unavailable and sign-up stays
   closed** — the deploy that just landed changed nobody's behaviour.
2. **Google Postmaster Tools.** A TXT record and a sign-in. There is still no way to see whether
   mail is being filtered; the Spam discovery was luck.
3. **`SMTP_EMAIL` must become an `@arkvane.in` Workspace address.** Unchanged and still the
   biggest limit on deliverability.
4. **A booking link that books something.** The persona's URL opens the *reader's own* calendar.

1,586 tests, ruff and mypy strict clean, four CI jobs green, migration `b93d7e4a1c58` applied and
verified on the live database, zero errors in the worker log.


**Two gaps found by operating the deployed system, both the signature shape.**

**Connect Gmail aimed at the wrong mailbox, and only an org with more than one would ever have
noticed.** `/connect/google/status` and `/connect/google/start` have always accepted a
`mailbox_id`; `App.jsx` sent neither, so `_mailbox_for` fell back to `order_by(created_at)
.limit(1)` -- the org's **oldest** mailbox, regardless of which campaign was on screen. The live
org had three. The Sending-account panel therefore described a mailbox from a different campaign,
and connecting would have attached the credential to it.

The failure would have been near-undiagnosable. The callback refuses an account whose address
does not match the mailbox's own, so the only symptom is a mismatch message naming an address
the operator never chose -- which reads exactly like picking the wrong Google account on the
consent screen. A correct producer (routes that accept the id) and a correct consumer
(`_mailbox_for`, which honours it) with nothing checking the caller passed it.

The fallback is **kept**: a new workspace has one mailbox and no id to send, and removing it
would break the ordinary case to serve the rare one. What changed is that the dashboard now says
which mailbox it means, and the `useEffect` depends on `campaign.mailbox_id` so attaching a
different one re-asks instead of describing the previous one.

**The connect router had no tests at all.** It has nine now, in two halves, because testing only
one half is how this defect survived: `tests/integration/test_connect_mailbox_choice.py` proves
the server honours an id and that an id from another org resolves to nothing (ADR-004 asserted
rather than assumed), and `tests/unit/test_connect_sends_mailbox_id.py` proves somebody sends
one. A source check rather than a component test -- the frontend suite covers `api.js` and has no
rendering dependencies, and adding React Testing Library to assert one fetch argument is a
dependency tree to audit forever. Same bargain as `test_send_guard.py` and `test_cors_methods.py`.
It strips `//` lines first, because the comments explaining the fix contain the word `mailbox_id`
and prose must not satisfy an assertion about code.

**`check_env` had never heard of the OAuth configuration.** It builds its whole report from
`REQUIRED_KEYS`, `FEATURE_KEYS` and `FUTURE_KEYS`, and the multi-user work added `CREDENTIAL_KEY`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` and `DASHBOARD_URL` to none of
them. So the one command whose job is answering *"what is configured?"* printed **"All required
configuration is present."** on a server where Connect Gmail was drawn as unavailable and sign-up
was shut.

**That is worse than an omission.** A report that does not mention a variable is
indistinguishable from a variable that is fine, so its silence was read as an answer; confirming
the truth took a hand-written `python -c` against the settings object inside the container. The
fix is one `FUTURE_KEYS` group -- the script needed no change, which is the point -- plus a
structural test that reads every `*_configured` property with `ast` and asserts each field it
depends on is reported. A property added later is covered without anybody remembering the test
exists, which is the only way a guard against forgetting can itself not be forgotten.

`SIGNUP_MODE` joins `SEND_MODE` in the Safety block rather than the set/unset groups: it always
has a value, and **`closed` looks identical to `unconfigured` from the outside**.

**Anyone who signs up can now send as themselves — and until this change, they sent as *us*
(ADR-020).** Asked as a product question: why is the sending account fixed to one address when
Gmail OAuth is integrated? The answer turned out to be a cross-tenant credential leak in two
directions, and both had been live since sign-up opened.

**Outbound.** ADR-018's fallback — a mailbox with no credential uses `SMTP_EMAIL` — had no owner
check. A stranger signs up, creates a campaign, types their own `from_email`, never connects:
`login_for` hands back **the operator's Gmail**. Their campaign sends on the operator's daily
cap, under the operator's sending reputation, aligned to the operator's SPF and DKIM — mail the
operator never saw, from an address they have never heard of, with nothing afterwards to say it
happened. The `From` header shows the stranger, so the recipient's mailbox does not say so either.

**Inbound, which is worse.** `ImapPoller` never called `login_for` at all. It read
`IMAP_EMAIL`/`IMAP_PASSWORD` directly, so `ImapLoop`'s careful once-per-mailbox pass logged into
**the same inbox every time**. A second workspace's replies, bounces and opt-outs are in their
own inbox and were never fetched — while the operator's personal mail was ingested into the
stranger's org as orphaned inbound messages. An opt-out nobody can read is an opt-out we will
breach, and that is a compliance failure rather than a missing feature.

**The fix is two conditions, and either alone is wrong.** `orgs.uses_environment_mailbox` says
whose the environment credentials are; no route sets it and the migration grants it to the oldest
org only. The address must *also* equal `SMTP_EMAIL`, because an owner holds several mailboxes
and only one of them is the account that logs in. Matching on the address alone would have been
the hole in a new costume: `uq_mailboxes_org_from_email` is per org, so anyone may create a
mailbox named after the operator's address, and the guard would have handed them the password.

`ImapPoller` now takes a `MailLogin` per call and authenticates with XOAUTH2 when the mailbox has
a token — a parameter rather than a rewrite, which is ADR-018's choice of SMTP/IMAP over the
Gmail API paying off. A mailbox that cannot authenticate is **skipped and counted**, not polled
as somebody else, and one unconnected mailbox does not stop the others.

**The dashboard can say so, and asking cost a design decision.** `blockers` gains
`mailbox_unavailable`. The obvious implementation — call `login_for` — would mint an access token
from inside a `GET`, so refreshing the campaign screen would call Google and could write an
`invalid_reason`: a read path with a side effect, fired by anyone pressing F5. So the knowable
half lives in `unavailable_reason()` and **`login_for` calls the same helper**, rather than a
second copy of the rule that would drift the first time either side changed — which would have
been this project's signature defect for the eighth time.

**And `/admin/sending-config` was telling every workspace the operator's address.**
`sending_account` returned `config.SMTP_EMAIL` unconditionally. It now names the org's own
mailboxes, with `uses_shared_account` saying whether one of them is the deployment's.

1,614 tests, ruff and mypy strict clean. Both new guard sets were run against the unfixed code
first: restoring the old fallback fails three tenancy tests and two IMAP tests, and the
pre-existing `test_a_mailbox_with_no_credential_uses_the_environment` **asserted the bug** — it
is now `test_the_owners_own_mailbox_with_no_credential_uses_the_environment`, which is the case
the fallback was always for.

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

**The research cache was keyed on the wrong domain, and one company's findings could be mailed
to another (ADR-011).** Asked as a storage question — keep `domain_research` to save Tavily
credits and tokens, or flush it after 30 days? Measuring it answered a different and worse
question.

**The key was `leads.domain`, which is the domain the founder writes *from*, not the company's
site.** A founder on a free-mail provider is imported with a warning rather than rejected —
deliberately, because founders do use Gmail — so that column is routinely `gmail.com`. Every
Gmail-addressed lead in an org therefore shared **one** `domain_research` row keyed `gmail.com`,
and the second such lead was served the **first company's findings**, which the drafter goes on
to cite as fact. An email to one company describing another is the worst output this system is
capable of, and it was one shared key away. The key is now the registrable domain of
`company_url`, which fixes both directions: unrelated companies never collide, and two founders
at one company are one fetch however either of them happens to write to us.

The same root cause was spending Tavily credits on nothing: the search fallback queried
`f"{lead.company_name or lead.domain} company news"`, so a nameless free-mail lead searched for
*"gmail.com company news"* out of a budget of 1,000 a month.

**On the storage question itself: it is not a tradeoff.** `evidence_text` is capped at 20,000
characters and nulled after 30 days; what survives is ~2 KB of findings and sources. At 100
emails a day — about 1,100 new companies a month in the worst case where no two leads share one
— keeping every company's research **forever** costs ~3 MB a month, **40 MB a year**, against 23
GB free, plus a constant ~22 MB for the rolling month of un-purged text. Three orders of
magnitude from mattering.

**And the 30-day expiry was the worst of both.** Nothing has ever deleted a `domain_research`
row — the retention sweep only nulls the text and lead erasure sets the FK to NULL — so the
expiry saved no storage at all. It only stopped the reuse.

**What actually constrains reuse is staleness, and it is not uniform.** What a company does, and
what it is built with, reads the same in April as in January. What it announced last week does
not, and a cold email opening on a stale "saw you're hiring two backend engineers" is *worse*
than one that opens on nothing, because the recipient knows whether it is still true. A single
expiry date cannot express that. So `CACHE_DAYS` is 90, rows are kept, and **hooks age rather
than rows**: past 30 days a hit keeps only its durable hooks, `news`/`hiring`/`launch` are
dropped with a note in `gaps` so the reviewer can see it happened, and **if that leaves no hooks
the row is treated as a miss** and the company is researched again — the durable findings alone
are not worth an email.

**The campaign form did not offer the field its own validator required.** Creating a campaign
failed with *"set booking_url or cta_text: a draft needs a concrete next step to ask for"*. The
rule is right and stays. The form offered no `booking_url` field at all and labelled `cta_text`
*optional*, so the obvious way through the screen was refused by a sentence naming a field that
was not on it. Both are now offered under a heading saying to fill in at least one, the pair
rule is checked in the form as well as the API, and `booking_url` is omitted rather than sent
empty — `''` is not a URL and would have traded the error for a different one.

**A campaign now has a mode, so this is two tools (ADR-012).** `research` is unchanged: read the
company's site, find something specific and true, ask a model for a grounded email. `template`
is new: the operator writes the email once with merge fields, and each lead's own details fill
the blanks. No research and no model for a first email or a follow-up, which makes a sheet of a
thousand drafts a matter of seconds rather than of a free tier's daily model limit. Replies stay
with a model in both modes, because a reply has to answer what the person actually said.

**The mode changes who writes the email and nothing else about what happens to it.** Both
drafters return the same `DraftResult`, so a rendered email is stored as a draft row, approved by
a person, and sent under the same cap, window, suppression check and `review_all` rule. A test
asserts a template lead still cannot reach `SENT` without a human.

The renderer's rules, and the one that matters most: **an unresolved placeholder never reaches a
recipient.** `Hi {{first_name}},` is the most recognisable failure of mail-merge and `Hi ,` is
the second — both worse than a generic opening, because both announce that nobody read it. So a
placeholder resolves, or has a fallback like `{{first_name|there}}`, or the render fails and the
draft is stored unsendable naming the field. It is checked twice independently: at substitution,
and again by the existing placeholder check reading the finished text. Substitution is one pass,
so a spreadsheet cell containing `{{booking_url}}` renders as those characters; nothing is
evaluated; and another tool's syntax (`{first_name}`, `%NAME%`, `${name}`) is refused when the
template is *saved* rather than when it is sent.

**Validation is narrower for a rendered template, on purpose.** `validate_draft` mostly catches
*invention* — unresolved claims, numbers absent from the evidence, a subject over six words —
and none of that applies to text a person typed. Hard rule 6 forbids the **drafter** inventing
facts about the sender; an operator is the authority on their own offer, and failing their email
for saying so would be the system refusing to send what it was asked to send. What still applies
is the shape of the message, because that is about the recipient: merge fields, forbidden
phrases, plain text, one link and only the booking URL.

**Two real gaps found while wiring this up, and neither has ever worked.** Both are the same
shape as every other defect in this project — a correct table and a correct consumer with
nothing producing the thing in between:

1. **No follow-up draft is ever created.** The send loop looks for an approved draft of kind
   `followup`, `domain/scheduling` computes when one is due, `SendLoop` sets `next_action_at`
   after a first send, the state machine has `SENT -> FOLLOWUP_SENT`, and `CampaignSettings` has
   `followup_after_days` and `max_followups` — and **nothing anywhere writes a
   `Draft(kind="followup")`.** A grep for it across `app/` returns only the constant and its
   consumers. So the locked product rule "one follow-up three calendar days after the first
   email" has never once happened. It is invisible so far only because the first real sends were
   two days ago.
2. **Nothing ever sets `NO_RESPONSE`.** `next_silent_close` computes the date; no code writes the
   state. A lead that never replies stays `SENT` for ever, so `final_wait_days` does nothing and
   the lifecycle has no end.

Both are the next change rather than this one: the `Scheduler` gains a pass that queues follow-up
drafts when they come due and closes silent leads, and the follow-up drafter serves both modes —
`generate_draft(kind="followup")` already exists for research campaigns, and
`CampaignTemplates.followups` for template ones.

**A draft that failed validation could not be rejected, so the lead had no way out.**

```
draft 452f5d21-dd3b-4ad0-b7a7-45552a935853 is validation_failed, so it cannot be rejected
```

A failed draft is shown in the review queue deliberately — a reviewer has to be able to see that
a weak model produced something unusable, rather than wonder where the lead went. But
`validation_failed` was not in `LIVE_STATUSES`, so both `approve` and `reject` refused it. The
only control the screen offered was the one the API would not do, and the screen even promised
*"reject it with a note and it will be rewritten"*. **Nothing available to a reviewer could move
that lead.**

`JUDGEABLE_STATUSES` is now the question that was actually being asked. `validation_failed` is
judgeable; the statuses that stay shut are the ones where the decision has already been made —
`approved`, `rejected`, `superseded`, `sent` — where acting again means the queue on screen is
stale.

**Approving a failed draft requires an edit.** Not because a human needs permission:
`approval_mode = review_all` already means a person decides, and an edit to an *accepted* draft is
unvalidated too, deliberately, because the reviewer is the authority on what leaves. It is because
the validator objected to something concrete in *this text* — a forbidden phrase, a claim resting
on nothing, an unfilled merge field — and the reasons are on screen beside the button. Approving
it untouched is a reviewer clicking past a warning rather than answering it, so Approve stays
disabled until something changes, which turns "I accept the risk" into "I fixed it". Bulk approval
still skips failed drafts entirely: the failure mode of a bulk button is somebody clearing a
hundred without reading any, and the weak ones are exactly the ones worth reading.

**And a second defect behind it.** A failed draft leaves the lead in `DRAFTED`, because the
handler only moves it to `AWAITING_APPROVAL` once a draft passes — so approving a reviewer's
fixed-up version died on `DRAFTED -> APPROVED is not a legal transition`, *after* the edit had
been written. The missing step is written rather than the table widened: `DRAFTED -> APPROVED` as
a legal move would let anything skip review, and the two-step version is also what really
happened — a person was shown this draft, and then approved it. That is the fourth time this
project has hit a transition the table did not describe, and the second time the right answer was
to add the step rather than loosen the rule.
**The first reply this system ever received went to Spam, and the reply engine could not see
it.** Reported as: the lead replied to both addresses, the dashboard shows nothing, and in the
Gmail account the reply is in spam.

`ImapLoop` polled `FOLDERS = ("INBOX",)`, with a comment saying Spam was worth adding *"once a
real campaign has run, because replies do land there"*. A real campaign ran; the first reply it
produced landed in Spam; and the loop could not see it. The dashboard showed no reply, which is
indistinguishable from no reply having been sent — and `DEFAULT_FOLDERS` in `imap.py` carried the
same comment above the same INBOX-only tuple, and was referenced by nothing at all.

**Why it was in Spam is the sending-identity problem, arriving as predicted.** The message went
out as `From: rahul@arkvane.in` authenticated as an unrelated Gmail account, so it carried no
SPF or DKIM alignment for `arkvane.in` — `GO_LIVE.md` §1 records this, and the owner chose to
defer it. An unauthenticated message to Gmail is a spam-folder message, and a *reply* to one
inherits that judgement. Polling Spam makes the reply visible; it does not make the mail
authenticated, and at real volume unauthenticated mail is not a spam-folder problem but a
suspended-account one.

**The folder is discovered, not hardcoded.** Gmail calls it `[Gmail]/Spam` in an English account
and translates it otherwise, so a hardcoded name is correct for exactly one locale. The loop asks
the server (`LIST`, RFC 6154's `\Junk` attribute), falls back to the names providers actually use,
and **only ever polls a folder the server listed** — polling one that does not exist is an error
on every cycle for ever, which is a worse failure than not reading spam. The parsing is a pure
function, `junk_folder_from_list`, so the part that actually breaks is testable without an
account. The list is cached for six hours, because a `LIST` is a login and the provider counts
those.

**Reading out of Spam is safe here.** A reply is matched to a lead by `In-Reply-To`/`References`
against a `Message-ID` we generated, before any model is called, so unrelated junk costs one row
and nothing more — already demonstrated when 1,400 unrelated messages were ingested from a
personal inbox and classified none of them.

`ImapLoop` had **no test at all**, which is how it came to read one folder in production. It has
five now, including one asserting that a *paused* mailbox is still read: pausing stops sending,
deliberately, and an opt-out we refused to read because we had stopped sending is the one we
would breach on starting again.
**A campaign with an unreadable persona crash-looped the send loop, and it was my regression.**
Reported as "test campaign 2 is running, I approved a draft, nothing sent, and it says something
about the persona" — while a draft approved in the *first* campaign on the same mailbox went out
fine.

PR #67 gave each candidate its own campaign so that a lead could not go out signed by a different
campaign. It did that with two **lazy properties**, `Candidate.persona` and `Candidate.settings`,
which called `model_validate` when read — and they are read inside `send_one`, *after* the
candidate has been chosen, with no `try` between there and `WorkerTask.run`. So a campaign whose
persona was missing a field raised, the error escaped `tick()`, the task recorded a failure and
backed off, and **no mailbox sent anything at all**, with the reason visible only as a traceback.
Which campaign's lead happened to be chosen first decided whether anything went out that cycle,
which is exactly the behaviour the owner described.

**That is the identical shape as the `send_days` incident that produced per-mailbox reporting in
the first place**, and a lazy property put it straight back. The lesson generalises better than
the fix: *validation that happens where the failure cannot be contained is the bug, not the
invalid data.* Both values are now resolved once inside `find_candidates`, in the same guarded
block that already handled unreadable settings, and a campaign that cannot be read contributes no
candidates while the others have their turn.

**The fallback was the other half of it.** `send_one` resolved
`chosen.persona if chosen.campaign.persona else persona`, so a campaign with an *empty* persona
borrowed the mailbox's first campaign's identity — the very thing PR #67 was written to stop,
surviving in the one branch meant to handle its absence. `send_one` no longer takes a `persona`
parameter at all: a message is signed by the campaign the lead belongs to and by nothing else, so
the only correct source is the chosen candidate, and passing one in is what made the mistake
expressible. `SendLoop._send_for` no longer resolves a persona either, and no longer returns
early when one is missing — it reads the mailbox-level defaults for the cap's day boundary and
falls back to the product defaults, because one campaign's bad row is not the mailbox's problem.

**Why 1,290 tests could not see it: the fixture created a campaign the API would refuse.** The
shared `campaign` fixture had `persona={}`, and every send test passed its own persona straight
into `send_one` — so the campaign row never needed one, and the production path, which reads the
row, was never exercised. The fixture now carries a persona, which is what `POST /campaigns` has
always required. Four tests were added against `send_pass` rather than `send_one`, because the
crash-loop lived in the layers between them: the same lesson as PR #54, which is now the second
time a defect has hidden in exactly that gap.

**And the dashboard could not have told anyone.** The worker's reasons go to the log and nowhere
else, so a campaign whose persona the send loop could not read showed a running status, an
attached mailbox and approved drafts, with no explanation anywhere. `GET /campaigns/{id}` now
returns **`blockers`**: every reason an approved draft will sit still — no mailbox, not running,
paused, an unreadable persona, unreadable settings, unreadable templates, the org paused, the
send mode disabled — each computed **from the same Pydantic models the worker validates with**
rather than from a second set of rules that would answer differently the first time either side
changed. Field names only, never stored values: this travels to a browser and into screenshots.
The campaign screen lists them, replacing two hand-written banners that covered the two causes
somebody had thought of.

**One more defect fell out of writing that test.** `campaign_summary` rendered the settings
through `CampaignSettings`, so an unreadable row made it raise — turning `GET /campaigns/{id}`
into a 500 and, because the same function builds every row of the list, taking out the campaigns
list for every healthy campaign beside it. **The one campaign an operator most needs to open was
the only one they could not.** It falls back to the product defaults for display now; the fault is
reported by `blockers`, and the detail route still returns the row exactly as stored, because the
settings form edits what is actually there.

**Template mode asked for an agency and an offer from people who have neither (ADR-013).** The
owner's second use for this is job hunting, and `Persona` required `sender_role`, `agency_name`,
`offer`, `goal` and a booking link or an ask — so the only way through the form was to type
something that passes. **A persona filled in to satisfy a validator is a persona the drafter
would go on to quote**, so the rule meant to stop invention was requiring it.

Those requirements are real, and they belong to the **research** drafter: it asks a model to
write on our behalf, so it has to be told who that is, or the model makes it up (issue A3, the
worst thing the prototype did). They were expressed as model validators, which made them a
property of the data instead — and so they applied to a campaign that never calls a model.

`sender_name` stays required: it signs the message and it is in the footer, so a recipient can
always tell who wrote to them. The rest moved into `Persona.missing_for_drafting()`, which
returns each missing field **with why it matters**, and is asked at the three places that need
it: creating or patching a `research` campaign, `handle_draft_lead` before the model is called,
and `blockers` so an existing campaign in that state says so rather than silently drafting
nothing. The footer already skipped an empty agency line, so nothing at send time changed.
**An import can be undone, and the reason it needed its own operation is the interesting part.**
Asked for as: what happens if someone uploads the wrong sheet?

Half of it already existed — `/imports/preview` writes nothing and the screen shows every row's
verdict before anything is committed — but there was no way to **abandon** a preview short of
choosing a different file, and no way back at all once committed. The preview now has a Cancel
button (which also clears the file input, or choosing the *same* file again fires no change event
and looks broken).

**The undo could not reuse `DELETE /leads/{id}`, and that is the decision worth recording.** That
route is the erasure path for a data-subject request, and it deliberately leaves a **hashed
suppression** behind — so that somebody who opted out and then asked to be forgotten cannot be
mailed again by the next import of the same sheet. Undoing a *mistaken upload* through it would
permanently block every address in the sheet: re-uploading the corrected one would skip exactly
the people it was meant to reach, and nothing on screen would say why. The test that proves an
undo suppresses nothing is the most important one in the file, and the one after it re-imports the
same address to show it.

`leads.import_batch_id` stamps each upload, returned by the commit so the dashboard undoes the
batch it actually made rather than hunting for "the most recent" — which would be the wrong one
for anybody with two tabs open. Leads imported before this column existed have NULL, which is the
truth: there is no record of which upload they came from, and inventing one would make an undo
delete rows it was not asked about.

**An undo does not unsay anything.** A lead that has been emailed is kept, whatever the operator
meant to upload — the record of what we sent to whom is the one thing here that must not be
editable, because a reply can still arrive, an opt-out has to be honourable and a bounce has to be
attributable. A message still `sending` counts as emailed, since ADR-002 is explicit that we never
assume such a message did not leave. So is a lead that has replied or bounced: something happened
to that person. The response says what was kept and why — *"Removed 9 lead(s). Kept 1: 1 already
emailed."* — rather than reporting a silent partial success.

**A proposed reply has never once been written, and the deploy's own logs said so.** Found by
reading the worker log straight after deploying everything above:

```
"job permanently failed" ... kind: "draft_reply", error: "job payload has no lead_id"
```

**Two callers queue a `draft_reply` job, with two different payload shapes, and neither matched
what the handler read.**

- `reply.service` queues on ingest with `payload={"message_id", "label"}` and puts the lead in
  the `jobs.lead_id` **column**. The handler read `payload["lead_id"]`. So **every reply to a
  real inbound message died**, permanently — a missing key is not retryable. The lead reached
  `REPLIED` and nothing ever proposed an answer, which from the dashboard is indistinguishable
  from a reply nobody has got to yet.
- `drafts.reject` queues a regeneration with `payload={"lead_id"}` and no `message_id`, so
  rejecting a reply draft would have died the same way on the other key.

Each side was individually right and individually tested. Nothing checked that they agreed —
which is now, unmistakably, the dominant shape of defect in this project: the rejection path,
`NEW -> RESEARCHED`, `RESEARCHED -> AWAITING_APPROVAL`, `DRAFTED -> DRAFTED`, research queuing
nothing, the free-mail cache key, and now this.

The handler resolves the job from whatever each caller does provide: the **lead from the
column** (every caller sets it, and it is the FK that cascades a deleted lead's work away), the
**message** from the payload or else the newest inbound message for that lead — which is what a
regeneration is answering — and the **label from `classifications`**, which is the authoritative
record, written before anything acts on it precisely so the answer does not depend on re-running
a model (issue B17). Reading it back beats storing a second copy on the draft that could
disagree with it.

Two things it still refuses, both permanently: a lead with no inbound message, and a message
with no classification. There is no safe default for a label — it decides whether a reply may be
drafted at all, so guessing could propose an answer to somebody who asked to be left alone.

**The new tests drive the real producers rather than a payload a test author wrote.** That is the
only shape of test that could have caught this: both sides were already covered in isolation.

**And the first reply draft ever written was then refused for ending its last sentence with a
full stop.** The message, from the live database:

```
the only link allowed is the booking URL https://calendar.google.com/calendar/u/0/r?pli=1,
not https://calendar.google.com/calendar/u/0/r?pli=1.
```

The two strings are identical except for a trailing `.`. `_URL` matches greedily, so a sentence
ending *"book here: https://cal.example.com/asha."* captures the stop as part of the URL, and
`_check_links` then compared it against the persona's booking URL and found a mismatch. A correct
draft, refused, with a message nobody could act on — which is the worse half: an operator reading
that has no way to tell what is wrong. `found_url()` takes the sentence's punctuation back off
before a URL is counted or compared. The rule is unchanged: one link, and it must be the booking
URL.

**Worth the owner's attention, separately:** the booking URL on that campaign is
`https://calendar.google.com/calendar/u/0/r?pli=1`, which opens *the reader's own* Google
Calendar. It books nothing. A recipient clicking it sees their own week. That is a persona value,
not a bug, and it should be a real booking link before any of this reaches a stranger.

**A failed draft stayed in the review queue beside its own replacement.** Seen on the live
dashboard the moment regeneration started working: one lead showing a `validation failed` reply
at v1 *and* a `pending_review` reply at v2, two rows for one lead, one of them unjudgeable.

`_supersede_live` retired the previous draft only when it was in a *live* status, and
`validation_failed` is not one — so every failure accumulated in the queue for ever, beside
whatever eventually succeeded. Keeping a failure visible is deliberate, so a reviewer can see
that a weak model produced something unusable rather than wonder where the lead went; keeping it
visible *after* a newer attempt exists is just noise in the one screen that must stay scannable.
It is superseded now. Nothing is lost: the row keeps its failures and `save_draft` writes a
`draft.created` event per attempt, so the lead's timeline still shows every version and why each
one failed.

**Approving a reply was a 500, and it was the third link in the same chain.** The operator saw
only *"Something went wrong. Quote the request id if you report this."*

`approve()` always moved the lead to `APPROVED`. A reply is sent from `REPLIED` and a follow-up
from `SENT` — `outbox._VALID_FROM` says so and the send loop checks it — so moving either to
`APPROVED` would make the message **unsendable by the very act of approving it**. It never got
that far: `REPLIED -> APPROVED` is not a legal transition, so it raised inside the route.

Only a first email moves the lead now. What makes a draft sendable is the *draft* being
approved; the lead's state says where the **conversation** is, and approving our answer does not
move the conversation — sending it does. A reply or follow-up approval writes a `draft.approved`
event instead, so a decision a person made is still in the trail.

Three defects in a row on one path, each hidden behind the last: the job was never queued in a
runnable shape, the one draft that got written was refused over a full stop, and the one that
passed could not be approved. Every one was a correct component meeting another correct
component on a contract nothing checked.

**And why `test_campaign`'s reply produced no draft at all: it is working as designed, and that
was invisible.** The reply was classified `other`, whose policy is "unclear — a person decides",
which deliberately proposes no draft. Five labels do that. Nothing recorded it, so the lead sat
in `REPLIED` with no draft, no job and no explanation an operator could see — indistinguishable
from drafting being broken, which it also was at the time, so the two could not be told apart. A
`reply.no_draft_by_policy` event now names the label, the action and the reason.

**The architecture review, and the two P0s it named are fixed (§27 of `ARCHITECTURE_TARGET.md`,
ADR-014, ADR-015).** §21's failure table was written before anything ran; §27 is written after a
deployment, three real sends, a real reply and sixteen production defects, and it says what the
code does rather than what it was meant to do.

**The two gaps that mattered are closed.** `app/services/lifecycle.py` is the producer that was
missing at both ends of a lead's life: it queues the follow-up drafts that have come due, and it
closes as `NO_RESPONSE` the leads that have had everything we were going to send and said
nothing. Neither had ever run. The follow-up is drafted and reviewed like any other email — it is
not auto-sent — and a template campaign renders the operator's own follow-up text.

**On adding RabbitMQ: no, and the reason is structural rather than a preference (ADR-014).**
Every job here is enqueued *in the same transaction as the state change that justifies it*, which
is why a crash anywhere loses no work and duplicates none. A broker destroys that property —
`commit()` and `publish()` are two systems with no shared transaction — and the standard remedy,
a transactional outbox, is the table we already have. At 0.005 jobs per second we are five orders
of magnitude from `SKIP LOCKED`'s limit, and the ceiling we are actually near is the free-tier LLM
quota, which a broker does nothing about. **No new infrastructure.**

**Corrections to assumptions that were raised.** Reply matching is **threading-first** — our own
stored `Message-ID` against `In-Reply-To`/`References`, then a DSN's `Final-Recipient`, then
sender-plus-subject within 30 days as a bounded fallback — and quoted text *is* stripped before
classification. "Sender address only, no threading, no stored Message-ID" describes the
prototype (issues B10, B11) and has not been true since Phase 6. Soft bounces are already
distinguished from hard ones and deferred a day rather than suppressed. Bulk approval already
does not bulk send: it makes leads eligible, and the send loop still sends one message per cycle
under the cap with a jittered gap.

The rest of §27.8 is recorded rather than fixed: the importer's single-transaction commit, the
unstaggered fan-out of a large import against daily LLM limits, uncapped reviewer-driven
regeneration, no campaign scheduling, no dashboard route to requeue a dead job, and four minor
items. Each has a named fix.

**The §27.8 gaps are closed, and one of them was a correctness bug rather than a load concern.**

**The same person in two campaigns was sending two cold emails.** `UNIQUE(campaign_id,
email_norm)` is per campaign on purpose — the prototype's *global* unique key made re-importing a
corrected sheet silently do nothing (B16) — and the cost of that choice is that two campaigns can
hold the same person and both send. Worse, if they reply to one, the other sends anyway, because
it is looking at its own lead row and that row knows nothing about the conversation beside it.

The guard is now **per person per org**, in `claim_send` beside the suppression check, and it
covers exactly the scenario described: campaign A sends, A is paused, B runs with the same person
— pausing a campaign does not unsend its email. A live conversation anywhere in the org blocks a
cold approach whatever the date says, which is the reply-mid-switch case. Follow-ups and replies
are deliberately not checked: they belong to a thread already open with that person. It is a
**cool-down, not a suppression** — ninety days, after which a second approach reads as a fresh one.

Writing that test exposed something smaller and worth keeping: **`claim_send` caught every
`IntegrityError` and called it "already claimed by another worker"**, so a foreign-key violation
on `draft_id` reported a concurrent worker that did not exist. Only the idempotency key means
that now; anything else raises.

**The load gaps.** An import no longer makes every job runnable at once: `run_after` spreads
research over the days the leads could actually be emailed, derived from the campaign's own daily
cap with one day of head start. There is no value in researching a thousand leads today when at
most a hundred can be sent. Template imports are untouched — rendering is local, instant and
free, and a thousand drafts in seconds is the point of that mode.

**Orphaned inbound mail is no longer kept for ever.** Unmatched mail is stored on purpose so a
reply we failed to match is not lost, and `match_lead`'s fallback only looks back 30 days — so
after 45 an orphan is not a reply we might still place, it is somebody else's correspondence in
our database. That is a privacy surface before it is a storage one; the hourly sweep now deletes
them.

**Rejecting now has a limit (5 per lead per kind).** Each rejection is a model call, and a loop
would spend the day's free-tier quota on one lead while every other lead waits behind it. The
refusal says what to do instead rather than silently declining, which would look like the
rejection button being broken — again.

**A dead job can be requeued from the API.** `/admin/dead-jobs` lists them with `last_error`, and
`POST /admin/jobs/{id}/requeue` resets the attempts, because the retries were spent on a
different version of the code. Three dead reply-drafting jobs were recovered by hand with SQL
after a deploy, which is what prompted this.

**`docs/OPERATIONS_QA.md` is new** and answers every question raised, by area, with file
references — crashes and at-most-once, deadlocks and lock ordering, LLM rate limiting and
exhaustion, throttling at four levels, reply threading, bounces and opt-outs, WAL and MVCC and
the connection pool, dead letters, roles, worker liveness, the backup trade, and where the real
throughput ceiling is (human review, as §20 predicted). It marks what is **not** implemented as a
gap rather than describing it as if it worked, which is how most of this project's defects got
written down as features.

**The dashboard has a design system rather than a stylesheet.** It was readable and plain: one
weight of grey, seven identical stat tiles, "Loading…" as a loading state, and an empty state
that was a sentence in a box. Fine to operate, not something to put in front of a founder.

**Tokens first.** `index.css` is now a vocabulary — a neutral ramp, semantic colours with soft
backgrounds and readable text pairs, a type scale named by role, spacing, radii, shadows and
*motion* tokens. Two rules keep it honest: a component never uses a raw colour, and
`prefers-reduced-motion` is honoured in exactly one place rather than in forty. Nothing in this
app is communicated only by movement, so switching it all off loses nothing.

**Light and dark, with a toggle.** The same token names are defined twice, so no component asks
which theme is on. An untouched install gets the system's answer; once somebody chooses, the
choice is stored and wins. A dark-only dashboard demoed in a bright room is a bad first
impression, and this is three blocks of CSS.

**The review queue is now keyboard-driven**, which is the screen that decides whether this tool
is pleasant: `J`/`K` to move, `A` to approve, `R` to reject, `X` to select. Deliberately inert
while typing — an editor fixing a subject must be able to write "approach" without approving
three drafts on the way through. The focused card is marked and scrolled into view, and a
progress bar replaces two stat tiles that between them held two integers: how far through you
are is a shape, not a pair of numbers.

**Seven equal stat tiles became two groups.** Volume and outcomes answer different questions,
and giving them one visual weight made "Meetings booked: 0" look exactly as important as
"Leads: 2" — so the number anybody cares about was the hardest to find. Outcomes take a colour
only when non-zero: a red "Bounced 0" is an alarm about nothing.

**Smaller things that add up.** Icons in the sidebar and an identity block instead of two lines
of text; skeletons shaped like the content instead of the word "Loading"; toasts that carry an
icon so success and failure differ by more than wording; a focus ring on `:focus-visible` only;
a busy button that keeps its width so a row of controls does not reflow mid-click; hover lift on
list rows; and the destructive "Delete campaign" pushed away from "Pause sending", because a
destructive action a thumb-width from a routine one is one somebody will eventually hit by
accident.

**No UI library.** Twelve hand-written SVG paths instead of an icon package: they ship nothing,
cannot break on a major version, and are not a dependency to audit for the life of the project.
The CSS is ~700 lines and the whole bundle grew by 2 KB gzipped.

**Two things found by opening the redesigned dashboard rather than by reading it.**

**It said "No campaign yet" to somebody with two campaigns.** `campaigns` starts as `[]`, which
is indistinguishable from "this org has no campaigns" — so for the half-second before the fetch
returned, the screen stated a falsehood and offered a Create button. **A loading state that
asserts something false is worse than a spinner.** "Loaded" now means *we asked and heard back*,
including hearing an error, so a failed fetch does not leave a skeleton spinning for ever above
the real message. Every network request on that page was a 200 — the bug was entirely in what
the UI chose to say while waiting.

**The filter bar ran into the paragraph above it**, so the field labels read as the last line of
the explanation. It is its own surface now, which is what it always was.

**Four interactions that make the dashboard feel like a tool rather than a form.**

**A command palette (⌘K / Ctrl+K).** Every screen and every campaign, two keystrokes away. Built
from the same `nav` list the sidebar draws, so a screen added later appears in it without
anybody remembering to. Matching is a plain substring: fuzzy matching looks clever and ranks
badly on short lists — with eight commands and a handful of campaigns, typing "cam" should show
the campaigns, not a scored permutation of the whole app.

**Lead detail is a drawer, not a page.** It used to replace the list, so comparing two leads
meant navigating twice and losing the scroll position both times. The list stays where it was,
Escape closes, the body scroll locks behind it, and focus moves into the panel so a keyboard
user is not left on the page underneath.

**A real drop target for the sheet.** Dropping a spreadsheet is how anybody who has used one
expects to start. It is a `<label>` wrapping the real input rather than a `div` with an
`onDrop`, so clicking opens the picker and the keyboard still reaches it — a drop target alone
is invisible to anybody not using a mouse. One path handles the file however it arrives.

**A chart, because every number on the campaign screen was a snapshot.** "1 reply" says nothing
about whether a campaign is working, only that it once did. `GET /campaigns/{id}/activity`
returns sends and replies per day, **bucketed in the campaign's own timezone** — a send at 23:40
IST belongs to that operator's day, and bucketing by UTC would quietly move a sixth of India's
working hours into the previous bar. Empty days are returned as zeros rather than omitted,
because a sparse series is a chart with invisible gaps and a quiet week should look like one.

Drawn by hand in SVG: a charting library is ~50 KB and a styling argument for two series of
thirty points, and this inherits the theme toggle for free. Replies stack *on* sends rather than
sitting beside them — the question is how much of what went out came back, and two adjacent bars
make that a comparison the eye has to perform.

**A dangling section heading, found by looking at it.** The campaign screen rendered "Activity"
above the chart unconditionally, so when the chart declined to draw — no data yet, or a backend
that does not have the endpoint — a title sat over empty space, which reads as something broken
rather than something absent. Heading and chart together now, or neither.

**And the screen that found it confirmed something bigger: the follow-up has been sent.** Three
leads of `test_campaign` are in `FOLLOWUP_SENT` with the message in their timeline — *"quick
follow up"*, sent 17 October. The chain ran end to end for the first time: the lifecycle pass
queued it, the handler drafted it, a human approved it, the send loop sent it, and the lead
moved on. That product rule was written in Phase 5 and had never once executed.

**Follow-ups arrived as new conversations, not under the email they were chasing.** Reported
from a real mailbox, and it was two bugs wearing one coat.

**Every outbound message went out with no `In-Reply-To` and no `References`.** `build_message`
has always accepted both headers and `build_references` has always known how to extend a chain —
`SendLoop` simply never passed anything to `claim_send`. The signature defect of this project,
one more time: a correct producer and a correct consumer with nothing connecting them.

`outbox.thread_headers` now says what each kind threads onto. A **follow-up** continues our own
last message to that lead — the first email, or the previous follow-up. A **reply** answers
*their* last inbound message, not our own. A **first email** threads onto nothing. `References`
extends the parent's chain rather than restarting it, so the third message in a conversation
still names the first, which is how a client groups a thread when an intermediate message is
missing from somebody's mailbox. Nothing to thread onto is not an error: a follow-up with no
visible parent is still a useful email, and refusing to send it would be the worse outcome.

**The subject was the other half.** Clients group on `References` first but fall back to the
subject, and the follow-up drafter was inventing a fresh one — so even with headers, anything
falling back would still file it separately. A follow-up now inherits the thread's subject
prefixed `Re:`, decided at **drafting** time rather than swapped in at send time, because a
reviewer has to see the subject that will actually be sent. Replies have worked this way since
Phase 6; follow-ups did not. The validator was updated to match: a follow-up must now keep the
thread's subject, and is exempt from the six-word limit for the same reason a reply is — the
length was chosen when the thread started and is not ours to shorten. A *first* email still may
not begin `Re:`, because faking a reply to a conversation that never happened is a deception
recipients recognise instantly.

**`docs/DEPLOY_CHEATSHEET.md` is new** — the three commands that deploy the backend, how to get
a shell without SSH, how to tell what is actually running, what to do when it fails, and the
reminder that the dashboard needs no deploy at all because Cloudflare builds it on every merge.

**The chart deployed and was three times too tall.** `preserveAspectRatio="none"` means the
viewBox stretches to whatever box it is given — so `height: auto` derived the height from the
100×34 viewBox and produced a chart about 370px tall on a desktop, for two bars. A fixed 120px
is what that attribute was always implying. Caught by looking at it after the backend deploy
made it draw for the first time.
