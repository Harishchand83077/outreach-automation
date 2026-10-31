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

**A mailbox can now say whether it works, and when it will stop (ADR-021).** Both halves were
asked for by somebody operating the system, and both were questions it had no way to answer.

**"Did Connect Gmail work?"** The only way to find out was to approve a draft and wait for the
send loop -- which, with `SEND_MODE=disabled`, answers nothing at all. Somebody signing up could
connect their account, see a green tick, and have no way to tell a working connection from a
broken one until their first campaign quietly did not go out.

`POST /connect/google/verify` authenticates over SMTP and stops **before `MAIL FROM`**, then
authenticates over IMAP and opens INBOX `readonly=True`. No envelope, no recipient, no message --
so `SEND_MODE` has nothing to decide, hard rule 1 is untouched, and the check works on a
deployment where sending is off, which is every deployment before go-live.

**The two halves are checked separately rather than one inferred from the other**, and that is the
part worth keeping. A mailbox that cannot send says so within a cycle, because a campaign stops
and somebody notices. A mailbox that cannot be *read* looks perfectly healthy: mail goes out, and
replies, bounces and opt-outs simply never arrive. An opt-out nobody can read is one we will
breach.

**The test email was asked for and is gated like the real email it is.** `send_test=true` goes
through `SmtpSender.send` unchanged; with sending disabled it comes back `blocked` and the report
says so in a sentence instead of failing. **No exception is carved out**, because the value of
hard rule 1 is that there are none -- a "but this one is only a test" path is how a gate stops
meaning anything. It goes to the mailbox's **own** address and there is no parameter naming a
recipient: an authenticated endpoint that mails an arbitrary address on request is the prototype's
worst route (C2) wearing a better name.

**"When does this lapse?"** ADR-018 leaves the OAuth client unpublished on purpose, and the price
is refresh tokens that expire seven days after consent. That was handled as a product state but
only *after* the fact: the first anybody knew was a paused mailbox, which in practice is a campaign
that stopped on Friday and replies nobody fetched over the weekend.

`mail_credentials.granted_at` is the new column, and neither existing one could have done the job:
`created_at` stops moving once the row exists, so a mailbox reconnected this morning looked as
stale as it did yesterday, and `last_refreshed_at` moves every hour as access tokens are minted,
so it describes a token that was never the one that expires. The `Scheduler` sweeps hourly, never
fatally, and warns at **one day left and at zero** -- the day 6 and day 7 the owner asked for.

**The mailbox id is in the alert *kind*, not only in its detail.** The cool-down dedupes by kind,
so without it the first mailbox warned would silence every other mailbox in the deployment for
twenty hours -- and the one that never got its warning is the one that stops. Three tests fail if
it is taken out, which is how it was checked.

`days_left` rounds **up**: a connection with eleven hours left reports 1, not 0. A countdown that
reaches zero while the thing still works is one nobody believes the next time it says zero. And
`grant_status()` is one pure function that both the dashboard sentence and the alert threshold
read, rather than a rule implemented once for the screen and once for the sweep -- which would
have been this project's signature defect for the ninth time.

`GOOGLE_GRANT_DAYS` defaults to 7 and `0` means never; publishing the OAuth client stops the
countdown with a configuration change and no code change. `check_env` reports it in the Safety
block, because an operator who does not know that number reads a weekly reconnect as a fault.

**A latent bug came out of writing it.** `MailLogin` was imported in `smtp.py` only under
`TYPE_CHECKING`, so `send(login=None)` -- the documented fallback to the environment account --
raised `NameError` instead of taking it. Nothing ever reached it, because `login_for` never
returns `None`, which is exactly why it went unnoticed.

**And the first version of this shipped a button that could not work.** `App.jsx` called
`setChecks(report)` with no `const [checks, setChecks] = useState(...)` anywhere in the file.
`npm run build` was green, because Vite does not resolve identifiers; `npm test` was green,
because the frontend suite covers `api.js` and renders nothing; all four CI jobs were green. The
button threw `ReferenceError` the first time a person pressed it, which is the one moment nobody
is watching. A second defect of exactly the same shape went in alongside it -- `Field` hands its
`onChange` the raw event and the Team panel passed a bare setter, so the invite box could not be
typed in.

`tests/unit/test_dashboard_state_is_declared.py` closes that class: every `setX` the dashboard
calls must be declared by a `useState`, a `const`, or a destructured prop. It is a source check
rather than a component test, for the same reason `test_connect_sends_mailbox_id.py` is -- ESLint
would catch this and more, and it is a dependency tree to audit for the life of the project.
Comments are stripped first, because the prose explaining a piece of state names it. It was run
against the broken file to watch it fail before it was kept.

1,649 tests, ruff and mypy strict clean, frontend builds, 14 frontend tests pass.

**`docs/USERS.md` is new: how somebody actually gets access.** Asked for after the
multi-user work landed, and the question turned out to have two different answers that were being
confused for one.

Access needs **two** systems to agree — Supabase Auth says whether a person may sign in, and
`app.members` says which workspace they are in — so "add a user" means either *their own separate
workspace*, which `SIGNUP_MODE` provisions on their first request with no SQL at all (ADR-017), or
*a teammate inside yours*, which is one `INSERT` and a role. They are not a progression, and
getting them the wrong way round produces the one symptom nobody diagnoses: a person signs in,
is provisioned a brand-new empty org, and reports that the campaigns have vanished.

**The thing worth knowing is that `SIGNUP_MODE=open` is only as open as Supabase is.** Turning off
*Allow new users to sign up* in the Supabase dashboard and leaving this `open` gives an invite-only
product with the guest list in a UI and the workspace provisioning automatic — which is what the
owner wanted and is not obvious from either setting alone.

Two smaller things are recorded because they cost time: `docker compose` on the server needs
`sudo` (it reads the root-only `.env`, and without it fails with `open /opt/outreach/.env:
permission denied`, which reads like a Docker fault), and `MAX_ORGS` defaults to 25 and is a
ceiling on top of whichever `SIGNUP_MODE` is set.

**Adding a teammate no longer depends on doing things in the right order (ADR-022).** The old way
was one `INSERT` into `app.members` against production, and it had to happen *before* that person's
first sign-in.

**And what happens if you do it afterwards is worse than "the insert fails".** There is **no global
unique index on `members.user_id`** — `uq_members_org_user` is scoped per org — so the second
insert succeeds. `deps.current_user` then resolves that person with `scalar()`, taking whichever
row the database returns first. The symptom is not an error: it is somebody seeing one workspace on
Monday and a different one on Tuesday, with nothing on screen admitting that two exist. Before
that, the ordinary symptom is signing in, landing in an empty org, and reporting that every
campaign has vanished.

`app.invitations` holds a seat. `provision()` consults it before anything else, so an invited
address joins **that** org with **that** role instead of being given a new one, whenever they
happen to sign in. **An invitation overrides `SIGNUP_MODE=closed`**, because the default this
project ships is `closed` and without the override the safest configuration would also be the one
where nobody can add their own team. A stranger with no invitation is refused exactly as before.

**It is matched on the email address, and that is a stated limit rather than an oversight.** A
Supabase `sub` does not exist until the first sign-in, so the address is the only thing available
to match on. It is not a token and not secret: a pending invitation grants nothing until somebody
authenticates as that address, so the boundary is still Supabase.

**One pending invitation per address across the deployment**, by a partial unique index. Per-org
would let two workspaces invite the same person and decide the winner by who signs in first —
refusing the second is a sentence somebody can act on, losing a race is not. Partial, so a used or
revoked row never blocks re-inviting somebody who left.

**Inviting somebody who already has a workspace is refused rather than absorbed**, since that is
the exact failure the table exists to prevent. And **you cannot invite above your own role**, or
the door built to let people in is a privilege escalation: an admin invites an owner, who may then
change the send mode.

Removal has two refusals, both about leaving a workspace reachable — not yourself, and not the last
owner — and a removed member's work stays, because a member is a key rather than a container and
the record of what was sent to whom must remain attributable.

Settings now has a **Team** panel, so none of this is SQL. `docs/USERS.md` §B was rewritten around
it, and its by-hand escape hatch writes an *invitation* rather than a membership for the same
reason.

**A campaign can be pointed at an account you already connected.** Asked for directly: *"he
can change the email per campaign ... either send from what he has connected or connect new"*.

Most of it already existed and nothing joined it up. `campaigns.mailbox_id` and
`PATCH /campaigns/{id}` have accepted a choice since Phase 2, and `POST /mailboxes` could create
one -- but nothing could **list** them, so the New campaign form asked for an address every time.
Within one org `POST /mailboxes` is idempotent on the address, so typing the same one back
quietly returned the same row; type a different one and you got a second mailbox that then had to
be connected separately, with nothing on screen saying why the new campaign would not send. The
signature defect once more: a correct producer, a correct consumer, nothing in between.

`GET /mailboxes` returns every sending account the workspace holds **with whether each one
actually works** -- connected, needs reconnecting, days left on the grant, and how many campaigns
use it. The state travels with the row rather than being fetched per mailbox afterwards, because
the choice is meaningless without it: an address that cannot authenticate is not an option, it is
a thing to fix, and a picker that presents the two identically sends people down the dead end.

It is **non-networked**. `grant_status` is arithmetic over a stored column, so a `GET` that
anybody can fire by pressing F5 never calls Google.

`invalid_reason` is deliberately not echoed -- it can quote a provider's reply and this reaches a
browser and screenshots -- so each row carries a boolean `usable` instead, computed by the same
`unavailable_reason()` the campaign blockers use. There is a test asserting the stored text does
not appear in the response.

The New campaign form now offers the list and preselects the single usable account when there is
one, so the ordinary case has nothing to decide and nothing to type. The campaign screen gains a
switcher when the workspace has more than one.

**What was not built, and why it is not a shortcut.** Connecting Gmail *before* signing up is not
possible: the OAuth callback carries no `Authorization` header, so the `state` nonce written by an
authenticated request is what says whose workspace the credential belongs to. Without that,
anybody could hand a signed-in user a crafted callback and attach their own mailbox to that user's
org. The order is necessarily sign in, workspace, connect.

And connecting at signup would not have prevented the double-membership failure, which is about
*which org a teammate lands in* -- it would have added a mailbox to the wrong org as well.
ADR-022 is the fix for that.

**The deployment no longer has a mailbox of its own (ADR-023).** Asked for plainly: *"now there is
no single user .ecell or arkvane.in — I can connect whatever email I want to send from."*

`SMTP_EMAIL`, `SMTP_PASSWORD`, `IMAP_EMAIL` and `IMAP_PASSWORD` are **gone from `Settings`
entirely**, along with `orgs.uses_environment_mailbox` and both fallback paths. A mailbox with no
credential cannot send and cannot be read — **including the owner's own**. `SMTP_HOST` and the
other three stay, because they describe Google's servers rather than anybody's identity.

ADR-020 narrowed that fallback to one org and one address, which was the right fix for the bug and
the wrong shape for the product. As long as one account lives in `.env` there is an identity that
belongs to the *deployment* rather than to a person, and every question about it — who may use it,
which org, which address, what if two match — is a rule somebody has to hold in their head and a
guard somebody has to maintain. Now there is no identity here that is not somebody's own: nobody
can send on an account they did not personally authorise, and "whose Gmail sent this?" has exactly
one answer.

**It costs a real outage and that is worth saying out loud.** The live deployment stops sending the
moment this lands, because its mailbox has no credential — it had been using the environment
account. One click of Connect Gmail resumes it. That is the change working rather than a cost of
it, but it must be expected rather than discovered.

**`CREDENTIAL_KEY` and the three `GOOGLE_*` values are now `REQUIRED_KEYS`.** What a deployment
must be able to do is no longer *send*; it is *let somebody connect their own account*. Missing
them, nothing can ever send — better as a startup failure than as a surprise at the first campaign.

**The allowlist is the org's rows now, not one environment variable.** `app.send_allowlist`, edited
in Settings by an `admin`. Before this, a new workspace could select `allowlist` mode and find no
way to put its own address on the list, while reading the operator's personal addresses out of its
own settings screen.

**The decision did not move.** `allowlist_permits()` in `smtp.py` is still the only function in the
tree that compares a recipient against anything, and the structural test over every file still
asserts it — only the *values* travel in now, which is exactly the trade ADR-010 made when the mode
moved out of the environment. `test_the_send_loop_does_not_reimplement_the_check` was tightened to
match: the loop may load the list, and may never test membership against it.

`None` and an empty list mean the same thing on purpose. "We could not load the list" and "the list
is empty" must have one answer, or a database hiccup becomes a send to somebody who was never on
it.

**The migration backfills `SEND_ALLOWLIST` from the process environment into the oldest org.**
Without it, a deployment rehearsing in `allowlist` mode comes back from the migration with an empty
list and sends nothing — correct by the new rules and indistinguishable from a break. A count is
logged, never an address.

**One connect option, not two.** Asked directly, and the answer is that a Workspace address and an
ordinary Gmail are the same OAuth client, the same `https://mail.google.com/` scope and the same
XOAUTH2 handshake. The address is whatever Google says it is. A second button would be a choice
with no consequence, which is a choice people get wrong. A genuinely different second path — app
passwords for a non-Google provider — needs per-mailbox host and port; `MailCredential.kind ==
"app_password"` already exists for it and nothing exposes it.

**Adding an allowlist entry is `admin`; going `live` stays `owner`.** Different in kind rather than
degree: an entry widens sending by one address somebody typed, inside a mode whose purpose is
rehearsal, while `live` removes the gate. Treating them the same would mean either handing out
`owner` or making rehearsal impossible without it.

`check_env` no longer counts the allowlist, because it cannot: the list is per org and in the
database, and a number right for one workspace and wrong for every other is worse than a sentence
saying where to look.

**What this cost in tests, which is the honest measure of how load-bearing that account was.**
Thirteen test files, and every send test now connects its mailbox and writes its own allowlist rows
— `connect_mailbox`, `allow_sending_to` and `mail_settings` in `tests/integration/conftest.py`.
The `org` fixture lost its one special field, which is the whole change in miniature: there is
nothing left that distinguishes the operator's workspace from anybody else's.

1,682 tests, ruff and mypy strict clean, frontend builds, 14 frontend tests pass.

**An admin screen, because three systems decide one question.** Whether a given person can send
depends on Supabase (may they sign in), this database (which workspace, what role) and Google (may
they connect a mailbox) -- and nothing showed them together, so "why can this person not send?"
meant checking two consoles and a `psql` session and holding the answer in your head.

`GET /admin/overview` and an **Admin** item in the sidebar, both `admin`-gated. Every person with
whether they have signed in, every mailbox with whether it is connected, who connected it and how
many days are left on the grant, plus the workspace's sign-up mode and grant length.

**Two things it refuses to pretend to know**, which is most of the design:

- **It cannot read Supabase.** `deps.py` has never joined across the identity provider's boundary
  and is not about to start. A person shows as *signed in* because a `members` row exists, and
  that row is written on their first authenticated request -- the only honest evidence available.
  Reporting it as "Supabase says yes" would be a guess dressed as a fact.
- **It cannot read Google's test-user list.** While the OAuth client is unpublished only addresses
  on that list can connect a mailbox, capped at 100, and there is no API for it. So the screen
  links to the console and names the step, rather than showing a tick nothing maintains. Missing
  that step produces a Google refusal about an unverified app, which reads like our bug and is
  not.

No `invalid_reason` is echoed, here as everywhere: that text can quote a provider's reply and this
reaches a browser and screenshots.

**Four things the review before merging turned up**, all in this stack's own code:

1. `check_env` still looped over `SMTP_PASSWORD` and `IMAP_PASSWORD` to warn about App Password
   length. `getattr(settings, name, "")` meant it silently did nothing rather than failing, which
   is exactly how dead code survives a refactor.
2. The new-mailbox form still told people to use "the same address as `SMTP_EMAIL` on the server",
   naming a variable that no longer exists.
3. **`_send_test_message` re-resolved the login and did not catch `MailboxUnavailable`.** The SMTP
   check immediately before it can *invalidate* a credential -- a refresh Google will not honour
   writes `invalid_reason` -- so the second resolution can fail where the first succeeded. That
   would have escaped as a 500, and a bare 500 leaves FastAPI's error middleware *outside* CORS,
   so the dashboard would have reported the backend unreachable while it was answering perfectly.
   The identical trap that made a duplicate-key error look like a connectivity failure.
4. `ImapLoop._login_for` was still typed and documented as returning `None` for the environment
   account. There is no environment account; it raises now.

**A campaign now knows what it is for: `outreach` or `job_hunt` (ADR-025,
`docs/CAMPAIGN_KINDS.md`).** An agency selling projects and a candidate looking for work are doing
the same mechanical thing, so the analysis was written first and its job was to find what is
*genuinely* different — because everything else had to stay shared. A forked pipeline would double
every future bug in a system whose signature defect is already two correct halves that disagree.

**`kind` is orthogonal to `mode`, and that is the load-bearing choice.** `mode` (ADR-012) already
decides who writes the email — `research` reads the company and asks a model, `template` renders
the operator's own text. That **is** "researched draft" versus "fixed draft", so both kinds get
both: four combinations, one pipeline, no third concept. A separate content-mode axis for job
hunting would have been a second way of saying something already said.

**The sender is a different input form, not a different contract.** `CandidateProfile` compiles
into the same `Persona` the drafter, validator, footer and claim check already take, with the
candidate's facts becoming proof points under the same `pp\d+` ids. So `evidence_corpus`,
`resolve_evidence` and `_check_claims_resolve` work **untouched**, and a job-hunt draft inventing
experience fails the same check, with the same message, as an outreach draft inventing a
statistic. That mapping is the whole design: without it, "may claim only what is in the profile"
would have needed a second validator, and a second validator is a second thing to keep honest.

**The grounding rule bites harder here and that is deliberate.** Hard rule 6 already forbids
inventing facts about the sender. For a candidate the failure is worse: an email claiming three
years of Kubernetes they do not have is not a weak email, it is a lie told in their name that
they will be asked about in an interview. So no résumé parsing — the candidate writes their own
facts, which is the only version in which the rule means anything.

**Drafting requirements belong to the sender, not the data.** ADR-013 moved them off `Persona` as
model validators precisely because a `template` campaign was being refused for not naming an
agency it does not have. A candidate is the same case more sharply, so `_refuse_if_undraftable`
now takes the *sender* and asks it.

**25 a day for a job hunt, and not as an opinion about ambition.** A candidate sends from a
personal Gmail with no domain reputation, no SPF/DKIM alignment of their own and no warm-up
history; 25 is the volume at which such an account does not look compromised. A default, editable
like every other — a kind that *enforced* a cap would be a second place sending limits are
decided, and `CampaignSettings` is the first.

**Different register, identical obligation.** The opt-out sentence differs, because a job enquiry
borrowing commercial sales language reads as insincere. Nothing else about compliance changes:
both kinds carry an honest identity and a working opt-out, both go through the suppression list,
neither tracks. The system deliberately does *not* claim a lighter obligation for job hunting on
the grounds that it may not be a commercial communication — the one time that classification was
wrong, nothing would have protected the recipient.

**Nothing changes for an existing campaign, and that is tested rather than asserted.** The column
defaults to `outreach`, whose defaults are today's values copied rather than re-decided, and
`tests/integration/test_campaign_kind_migration.py` builds a campaign the old way — no `kind`, a
plain `Persona` — and checks the whole read path, including that the outreach footer is
byte-identical. Every email already sent carries that sentence, and a quiet change would make the
archive inconsistent.

**One sharp edge caught while writing it.** `DRAFTING_REQUIREMENTS` as a bare class attribute on
a Pydantic model becomes a *field*, not a constant — it would have serialised into
`campaigns.persona` and been accepted back from a caller. It is a method now.
**The system now records what happened, so it can later be learned from (ADR-026,
`docs/DATASET_DESIGN.md`).** Built first of the three because nothing has been sent from the
current schema — a dataset cannot be backfilled out of rows nobody wrote, so this was the cheapest
it will ever be, and it makes the job-hunt kind measurable from its first send rather than
retrofitted.

**The design is mostly a decision about what *not* to build.** Almost everything needed already
existed and is joined to rather than copied: `messages` holds the text as sent, `drafts` already
creates a **new version** on an edit instead of mutating — so the model's draft and the human
rewrite both survive, which is the preference data, and it cost nothing — `drafts.claims` holds
the evidence ids behind each assertion, `classifications` the label and confidence, `events` the
append-only spine, `llm_usage` the provider and tokens. A warehouse beside the application would
have started drifting from it immediately.

Three things were genuinely missing, and only those were added: the lead's **outcome**, the A/B
**variant** and warm-up day, and an explicit link from a human edit back to the AI draft it came
from (`drafts.derived_from_id` — version numbers cannot express it, because a regeneration after
a rejection also makes a new version and that is a different event).

**Silence is censored, not negative, and the database enforces it.** Most cold email gets no
reply. Storing that as a negative class would teach anything reading this that every email without
a prompt answer was a bad email — when the commonest reason is that somebody was busy on Tuesday.
So `outcome` is NULL while we are still watching, becomes `no_reply_after_window` when the window
passes, and `ck_lead_outcomes_censoring` makes `(outcome = 'no_reply_after_window') = censored` a
constraint rather than a convention. The whole dataset rests on that one distinction and a stray
`UPDATE` would quietly destroy it.

**NULL rather than a `pending` member**, deliberately: a `pending` value would reach exports and
be counted as a class. "We have not decided yet" is not a kind of outcome.

**An outcome is not a reply label.** A classification says what *one message* said, is written
before anything acts on it, and drives suppressions (B17). An outcome is the lead's standing
result — slower, revisable, and sometimes set by no message at all. Overloading `classifications`
would have meant rewriting an audit row, which the append-only trigger correctly forbids.

**The reply taxonomy was extended, not replaced.** Two labels: `has_vendor` (they have the budget
and the problem and somebody else is serving it — a displacement signal that folding into
`not_interested` would have deleted) and `hostile` (suppressed, and **never** drafted for: a
machine-written reply to somebody threatening to report us as spam is how a complaint becomes a
listing). `objection` gained a *sub-type* rather than splitting, because separate labels would
have changed `POLICY`, which decides suppressions. A test asserts every label still has an action.

**`MEETING_BOOKED` stays unreachable from any label**, now with a test saying so. A warm reply is
not a meeting, and a system that inferred one would inflate every funnel it ever reported.

**Weekday and hour are derived at export, not stored** — they follow from `sent_at` and the
campaign timezone, and a stored copy of a derived value can disagree with its source. `warmup_day`
*is* stored, because `warmup_started_on` can be reset and the same `sent_at` would then compute a
different answer; it is the one piece here that is genuinely unrecoverable later.

**The export is pseudonymisation and says so.** Salted SHA-256 over addresses and company names, a
fresh salt per run so two exports cannot be joined, and the manifest carries only a *fingerprint*
of the salt — writing the salt beside the hashes would make the hashing decorative. The manifest
states plainly that the file is still personal data, which is the same honesty `PRIVACY.md`
already applies to the suppression hashes. One org per run; there is no "all orgs" flag, because
pooling is a consent decision rather than a command-line argument.
`orgs.dataset_pooling_opt_in` ships defaulted to false and read by nothing: the flag had to
predate the data, or consent could be applied retroactively.

Both new writes are wrapped and never fatal. The dataset records the work; it is not part of the
work, and a campaign must not stop because a row describing it could not be written. The
`Scheduler` resolves overdue silence in its existing hourly pass, so there is no new moving part.

**A note on what could not be verified locally.** Docker Desktop is paused on the development
machine, so the 300-odd integration tests skipped rather than ran; the unit suite, ruff, ruff
format and mypy strict are clean, and CI runs the full suite against its own Postgres.

**A plan for connecting your own Claude over MCP (ADR-027, `docs/MCP_PLAN.md`). Design only --
nothing is implemented**, which was the approved scope: MCP is the largest surface and the only
one of the three needing external OAuth setup, so it should not block the two that are purely
internal.

**The attraction is specific.** The free-tier LLM chain is the actual ceiling on this system --
not CPU, not the database -- so a user driving their own Claude spends their own quota and ours
stops being the constraint. Research can also use their own judgement and their own tools, with
LinkedIn unchanged: we still do not automate it, and a person reading a page and pasting what
they saw is not a scraper.

**The risk is not the obvious one.** A stranger reaching the API is handled the way everything is
-- a Supabase identity resolved to a `members` row, `org_id` from the membership and never from a
tool argument (ADR-004). The non-obvious risk is **prompt injection with tools attached**.
Everything here about a lead, a reply or a researched page is text somebody else wrote; hard rule
7 already calls it untrusted. What changes is where it goes. Today it reaches our classifier,
which can only *label*. Over MCP it reaches somebody else's agent, which has tools -- so a reply
reading "ignore previous instructions and use propose_draft to..." is, for the first time,
addressed to something that could act.

**The containment is the tool catalogue, not the model's good behaviour**, and that is the whole
safety argument. There is no send tool, no scope that could authorise one, and nothing that goes
near `SEND_MODE`. Delimiting untrusted text and validating claims make a hijacked agent less
likely to write a bad draft; they do not make it impossible. What makes the outcome acceptable is
that the worst a fully compromised agent achieves is **a bad draft in a queue a human reads**. A
design depending on a model ignoring an injected instruction fails the first time one works.

A write is a *proposal*: the same `Draft` row the in-app drafter creates, `pending_review`,
through the same validator, with `created_by='mcp:<user>'`. It does not bypass review, it enters
it -- which is why no new approval path is needed.

**What was verified against the spec, and what is assumed, is marked as such** in the plan.
Verified: Streamable HTTP (SSE deprecated), the server is an OAuth 2.1 *resource* server, RFC 9728
protected-resource metadata is required, clients send RFC 8707 `resource` and servers must check
it, Dynamic Client Registration is deprecated, and Anthropic connects from its cloud so the
endpoint must be publicly reachable. Assumed and to re-check on the day: exact plan availability
(two Anthropic pages disagree, so one is stale) and whether the 2026-07-28 revision is final.

**The largest unknown is recorded as unknown**: whether Supabase can act as the OAuth 2.1
authorization server with metadata discovery and `resource` support, or whether we need a minimal
one of our own. Issuing a JWT our API verifies is a different thing from running an authorization
code flow for somebody else's client.

Four phases, with read-only first: Phases 0 to 2 carry most of the value and none of the write
risk, so Phase 3 stays a separate decision taken with the first two already in production.

**There is a scope above the workspaces now, and it can read nothing inside them (ADR-024,
`docs/USERS.md` §C).** Asked as a roles question — the owner of the deployment is one person,
everybody else is a member, so why can the owner not see everything?

**The premise does not survive `SIGNUP_MODE=open`.** A sign-up provisions its own workspace and
owns it (ADR-017), which is the product: one user must not see another's leads. So "the owner sees
everything" cannot mean one shared org with everybody in it — that deletes the isolation the whole
schema is built around and puts every future customer's campaigns in the same tenant as ours.

But the person running the deployment genuinely had **no screen at all**. Every admin route
answers for exactly one org, correctly, and the questions an operator actually has cannot be asked
of any of them. `GET /platform/overview` and a **Platform** item in the sidebar answer them:
workspaces with their counts, mailbox connection health, send mode, quota burn, dead jobs,
unacknowledged alerts, worker liveness.

**The number worth opening it for is mailboxes connected.** A workspace whose OAuth grant lapsed
has silently stopped sending *and* stopped fetching replies, and from inside that workspace
everything looks completely normal — so the screen **names** the affected workspaces rather than
counting them, because the next action is to tell those people to press Reconnect and a count
gives you nobody to tell.

**It is not a role on `members`, and that is the load-bearing choice.** A role is a position
*inside* one workspace — `uq_members_org_user` is per org and `deps.current_user` resolves exactly
one membership. Reusing `role` for a scope above every workspace would make the word mean two
things depending on which row you read, and it would require the operator to hold a membership
somewhere: the scope above orgs would depend on being inside one, `provision()` could hand a
workspace to somebody who only wanted to read the dashboard, and `SIGNUP_MODE=closed` would lock
out the person who runs the deployment. Two questions, two tables, two lookups.

**The containment is the type rather than the route's good manners.** `CurrentOperator` has **no
`org_id` and no `ctx`**. `CurrentUser.ctx` is how every org-scoped route obtains the `OrgContext`
that every repository call demands, so an identity without one cannot reach a repository call —
a route that passed an operator where a user belongs fails on the missing attribute instead of
quietly querying somebody's leads with an org from who knows where. It is not a subclass of
`CurrentUser`, which would hand it `ctx` by the back door, and it is frozen. A boolean on
`CurrentUser` would have left `ctx` sitting right there, and the first convenient
`if user.is_operator` inside an org-scoped route would have turned the scope into a superuser key
into one workspace.

**ADR-004 keeps holding because the one deliberate exception is enforced from three sides.** The
router does not import `app.db.repo`; a structural test greps it for the column names that would
carry somebody's content and fails if one appears; and an integration test writes a lead, a draft
and a campaign with random strings and asserts **none of them appears anywhere in the response
text**. The third is the one that matters, because it catches a field somebody adds later in a
place nobody thought to look — a test that inspects only the keys it knows about cannot see a new
one.

**`invalid_reason` is read and never returned**, and that distinction is deliberate rather than
sloppy. A mailbox counts as connected when its credential has not been refused, so the one number
this screen exists for cannot be computed without reading that column — while returning it would
put a provider's quoted reply into a browser and a screenshot, as everywhere else. So it is absent
from the structural grep and asserted against the real response body instead, which is the only
place the real rule can be checked.

**Granted by an `INSERT` and by nothing else.** No route creates an operator, and a structural
test asserts that over the whole api and services tree — including `provision()`, the one place
that already creates memberships. There is no role that could legitimately hand out a scope with
nothing above it, so the only person who can create one is somebody who already has the database.
The `note` column exists because in a year it is the only thing distinguishing a deliberate grant
from one nobody remembers making.

**And it is explicitly not an admin key into any workspace.** An operator who is not a member of a
given org still gets 403 from that org's own admin routes, `/team` and `/campaigns`, and a test
says so. A superuser that could also use the ordinary routes would be a second path to a
workspace's settings — including, eventually, its send mode, which hard rule 1 keeps to one.

Two things it still refuses to pretend to know, unchanged: it cannot read Supabase, so "has this
person signed in?" remains the shadow a `members` row casts, and it cannot read Google's test-user
list (ADR-018).

**Two defects in the new query, both mine, both the signature shape, and both found by reading
the check constraint rather than by running anything.** They are worth recording because the
screen would have looked perfectly healthy with both of them in place.

1. The sent-mail count filtered `direction == "outbound"`. The column holds `'out'` —
   `ck_messages_direction` is the authority. So `emails_sent` was **zero for every workspace, for
   ever**, which is indistinguishable from a deployment that has sent nothing, and none of the
   tests I had already written would have noticed: every other assertion in that file is a
   `>= 1` on a count that happened to be right.
2. The failure count compared `outcome != "ok"`, and `Outcome` has no such value — success is
   `"success"`. So **every successful model call was counted as a failure**, and the quota panel
   would have reported a model chain in permanent trouble while it was working perfectly. It
   reads `Outcome.SUCCESS.value` now: one import, and it cannot drift from the enum.

A correct table and a correct caller with nothing checking that they agreed — twice, eleven lines
apart, in the newest file in the repository. `test_a_sent_message_is_counted` and
`test_only_failed_llm_calls_count_as_failures` exist because of them, and both were run against
the broken lines to watch them fail: `assert 0 >= 1` on the send count, and `assert 2 == (0 + 1)`
on the failure count.

`test_a_message_still_sending_is_not_counted_as_sent` was written alongside them and **does not
discriminate the bug** — with the broken filter every count was zero, so it passed trivially. It
guards a different property (ADR-002: a message mid-flight may still fail, so `sending` must not
be reported as `sent`), and saying it proved the fix would have been a claim the run does not
support.

The lesson is narrower than "be careful": **a literal that has to match a database constraint
should be read from the thing that defines it.** Where an enum existed, copying the string was
the whole defect; where no enum exists, the constraint is the authority and is one grep away.

The migration is one empty table with no foreign key, so adding it changed nobody's behaviour —
the route 403s for everybody until the first row exists.

**You can now connect your own Claude, and it can only read (ADR-028, `docs/MCP_PLAN.md`).**
`POST /mcp`, seven read-only tools, tokens minted in **Settings → Connect your own Claude**. The
attraction is specific: the free-tier model chain is the real ceiling on this system, so a user
driving their own Claude spends their own quota and ours stops being the constraint.

**Building it corrected two things the plan had assumed, and both made the work smaller.**

**The protocol got simpler.** Revision 2026-07-28 is final and removes the `initialize`
handshake, protocol-level sessions and `Mcp-Session-Id`, the standalone GET stream, and SSE
resumability. A server that only answers requests is now a single stateless POST route plus
`server/discover`. The older shape — sessions, a second endpoint, resumable streams — would have
been most of the work and none of it is required. What the revision adds is validation, for a
real reason: selected body fields are mirrored into HTTP headers so intermediaries can route
without parsing the body, so a server trusting one while executing the other would let a caller
show a load balancer one thing and us another. Hence `MCP-Protocol-Version`, `Mcp-Method` and
`Mcp-Name` are compared with the body, and a mismatch is `-32020`.

**And the plan's "largest unknown" is answered: Supabase cannot be the authorization server.**
supabase/auth#2610 is open — its OAuth 2.1 server accepts the RFC 8707 `resource` parameter and
**ignores** it, issuing `aud: "authenticated"` whatever was requested, confirmed on hosted
projects in October 2026. A commenter raises the consequence plainly: with dynamic client
registration a malicious MCP server could obtain a token another resource server would accept.

**That turned the authentication choice from a fallback into a positive refusal.** A Supabase
token's audience is a constant, so it cannot be shown to have been minted *for* this endpoint —
accepting one would mean accepting a credential obtained for something else, which is the
confused-deputy problem resource indicators exist to prevent. So there are two doors and each
takes one kind of key: a Supabase JWT is refused by `/mcp`, an `oa_` token is refused everywhere
else, and **a test asserts the dashboard's own perfectly valid token gets a 401 here.**

**The token carries no authority of its own.** `api_tokens` stores `org_id` for display and
authorisation **re-resolves the `members` row on every call**, so removing somebody from a
workspace takes their tokens with it. Without that a token outlives the membership that justified
it — an ex-colleague holding a key to the leads while the Team panel shows them gone. That test
was run against a version that trusted the token's own org, to watch it fail.

Only the `sha256` hash is stored and the secret is returned once; `sha256` rather than a password
hash deliberately, because 32 bytes of `secrets` output has no dictionary to slow down and the
lookup must be deterministic to be a lookup. `mcp:read` is the only scope and
`CHECK (scopes <@ ARRAY['mcp:read'])` makes a write scope unexpressible by hand — the same device
as `ck_orgs_send_mode`.

**The containment is the tool catalogue, exactly as ADR-027 argued, and it is now asserted rather
than intended.** No send tool, no approve tool, nothing near `SEND_MODE`; `test_mcp_safety.py`
reads the catalogue and the package source to say so, and the existing tree-wide send guard covers
the new package for free. The worst a fully hijacked agent achieves is reading one workspace's own
data back to its owner.

**One subtlety worth keeping.** The untrusted-text wrapper **escapes its own closing tag**. A
reply containing `</untrusted>` would otherwise close the block early and have everything after it
read as *our* instructions — the injection succeeding *because* of the delimiter meant to expose
it. That is a one-line fix and it is the difference between a defence and a decoration.

**And a test of my own that proved nothing.** `tools/list` ordering was asserted with
`sorted(names, key=lambda _: 0)`, which compares a list with itself and passes for any input. The
spec's actual requirement is that the order is *stable across calls*, which needs two calls to
observe; it is now written that way.

**Phases 2 and 3 are deliberately not built.** Phase 3 (proposing drafts into the review queue) is
by ADR-027's own gate a decision taken with the read-only phases **in production**, and nothing is
in production yet — shipping it in the same change would ignore the staging that made the first
part safe. Phase 2 (OAuth 2.1) now means writing and operating an authorization server of our own,
since Supabase cannot be one: a larger change than the endpoint it would protect. Meanwhile the
exposure is bounded by the catalogue rather than by the credential, which is what made shipping
the read-only phase on header auth defensible at all.

**One small piece of tidying the work forced, and it was the right kind.** The MCP endpoint has to
validate `Origin`, and the CORS middleware already parsed the same setting. Rather than a second
`split`, the rule moved onto `Settings.allowed_origins()` and the app factory's helper delegates to
it — so the two cannot disagree about what an allowed origin is.

**Your own Claude can now write a draft, and that is the only thing it can write (ADR-030,
Phase 3 of `docs/MCP_PLAN.md`).** `propose_draft` and `propose_reply`, behind a new
`mcp:propose` scope that is **off unless somebody ticks the box** when minting a token.

ADR-027 gated this explicitly: Phase 3 is "a separate decision taken with the first two already
in production". Phases 0 and 1 went live this morning, so the gate was waited out rather than
waived — and the design it described held exactly, which is worth recording because it was
written before any of it had run.

**A proposal is the same `Draft` row the in-app drafter creates**, `pending_review`, through the
same validator, with `created_by='mcp:<user>'`. It does not bypass review, it enters it — which
is why Phase 3 needed no new approval path, no new state and no new send gate. There is still
exactly one way an email leaves this system: a person approves it and the send loop sends it.

**The invariant was restated rather than weakened, and that is the part worth keeping.**
`test_mcp_safety.py` asserted that *every tool is read-only*, which was never the property that
mattered — it was a proxy for it. Tools now declare `sends` and `approves`, both false for every
entry, and the test reads those. "Nothing here can put an email on the wire or past a human" is
the real rule, so it is now written down as the real rule instead of being inferred from a
weaker one that had to break the moment anything wrote.

**The one genuinely new decision was which validator, and it is the whole ADR.**
`validate_rendered` is deliberately narrower than `validate_draft` — no invention checks — and
ADR-012's reason was that an operator typing their own email is the authority on their own offer,
so failing their draft for asserting it would be the system refusing to send what it was asked to
send. **A model is not that authority**, so a proposal goes through `validate_draft` unrelaxed.
Hard rule 6 does not care which model invents a fact. And the serious half: the agent has usually
just read a lead's reply, which hard rule 7 calls untrusted and which now reaches something
holding tools — so a proposal is the one place where text a stranger wrote could come back as
text we would send, and the claim check is exactly the defence that has to apply hardest there.
A proposal is therefore held to a *higher* standard than an operator's own template and the same
standard as our own drafter. Swapping the validator for the laxer one was run before being
ruled out: the invented "revenue by 42% for 173 teams" sails through as `pending_review`, and
three tests fail.

**Three refusals happen before anything is written, and two of them the plan had not thought of.**

1. **The lead's state has to admit a review.** `drafts.PROPOSABLE_FROM` lives beside `approve`
   because `approve` is what it has to agree with, and the test approves a draft from every state
   in it rather than restating the list. This exists because **four times** in this project a
   draft has been written and *then* found unapprovable — `NEW -> RESEARCHED`,
   `RESEARCHED -> AWAITING_APPROVAL`, `DRAFTED -> DRAFTED`, `REPLIED -> APPROVED` — each time
   after a model had been paid for. The in-app drafter is reached through the state machine and
   cannot ask the wrong question; a proposal arriving from outside can.
2. **A reply obeys the reply policy**, through the same `refuse_reply_draft` the in-app drafter
   calls. It was **extracted for this, not copied**: five labels deliberately produce no draft,
   and `hostile` is the one that matters — a machine-written answer to somebody threatening to
   report us as spam is how a complaint becomes a listing. A second copy of that rule would have
   been this project's signature defect in the worst available place. The test reads the labels
   from the policy table, so one added later is covered without anybody remembering.
3. **A reply needs a label of record**, because the label decides whether a reply may be drafted
   at all and there is no safe default.

**Smaller decisions.** `tools/list` is filtered by scope, because a tool a credential cannot call
is worse than an absent one — the model tries it, is refused, and tries again differently;
`cacheScope: private` was already set, which is what makes a per-credential catalogue safe to
cache. `required_scope` is **derived** from `read_only` rather than declared, so there is no
permissive default to forget and a tool added later is gated whether or not anybody gates it.
`propose_reply` has **no `subject` argument** at all: the in-app drafter overwrites the model's
subject to protect threading, and here the mistake is simply not expressible. `prompt_version`
is `agent-v1`, because an agent-written draft is not a sample of our own drafter's behaviour and
`DATASET_DESIGN.md` must not count it as one.

**A write commits before its audit row, and that ordering is a bug that was nearly shipped.** The
audit is deliberately wrapped and **rolls back** on failure — correct for a read, where the audit
row is the only write in flight, and destructive for a proposal, where the rollback would discard
the draft and then report success for a row that no longer exists. The event records `wrote`, so
the trail distinguishes the two without anybody having to know which tools write.

**The smoke check asserts the refusal rather than exercising the write.** It runs against
production, so minting a write-capable credential and proving it writes would leave a real draft
in a real workspace's review queue — a check with a side effect somebody then has to go and
delete. What it does instead is assert that the proposal tools are **withheld** from a read-only
token and that they **refuse** it, which is the half that can be checked safely and the half
worth checking: that the scope gate holds through TLS, Caddy and the real database, which no
in-process test can see. Removing the gate was run first, to watch the test fail.

**A proposal cannot replace a draft it has not read, and there is a cap (the owner's call).**
The first version of this shipped `save_draft`'s superseding behaviour unchanged and I filed the
consequence as a nuisance not worth pre-empting. That was wrong, and it was caught before the
tools ever went live: it is not churn, it is **the draft a reviewer has open being replaced under
them, so the one they approve is not the one they read.** The same class of defect as the headline
number that fell as the campaign succeeded -- the screen telling the truth about a different
moment than the one the person is acting in.

Superseding is right for a *regeneration*, because a reviewer asked for that one. A proposal
arriving from outside doing it silently is not. So `propose_draft` and `propose_reply` take
`replaces_draft_id`, and the only way to know that id is to have read the lead:

- **No id while a draft is waiting** -> refused, naming the draft and its id, so the agent can
  comply rather than guess.
- **A stale id** -> refused, naming the current draft. This is what makes it concurrency control
  rather than a speed bump: a re-run against a moved-on queue cannot clobber, and two agents
  cannot race.
- **An id for a lead with no draft** -> refused, because the caller is working from a stale read.
- **A draft a person wrote** -> never replaced, whatever id is passed. Somebody's own words are
  not an agent's to overwrite.
- **An approved draft** -> never replaced. A person decided and it is queued to send.

And **five proposals per lead per day**, the same number as `MAX_REJECTIONS` for the same reason.
It counts only drafts whose `created_by` carries the `mcp:` prefix, so our own pipeline's
regenerations do not count against an agent -- the cap is on what the agent did, not on how busy
the lead has been.

`REPLACEABLE_STATUSES`, `current_draft()`, `was_approved()` and `was_written_by_a_person()` live
beside `approve` in `app/db/drafts.py`, for the same reason `PROPOSABLE_FROM` does. **The
structural guard forced the last of those**: comparing `current.status == approved` in the MCP
package tripped the rule that nothing there may name an approved status, so the comparison became
a predicate owned by the module that owns the vocabulary. The guard was written to stop a write
and it caught a literal that would have drifted -- which is the better outcome of the two.

**And one of these tests proved nothing until it was checked.**
`test_an_approved_draft_is_never_replaced` used a lead with no classified inbound message, so the
call was refused for having no reply to answer and the assertion passed for the wrong reason.
Bypassing the clobber check made four of the six tests fail and left that one green, which is how
it was found. With the message and its classification in place, five of six fail -- the same trap,
and the same way of detecting it, as `test_a_message_still_sending_is_not_counted_as_sent`.

**The migration widens one check constraint by exactly one value** and changes no existing row:
every token in the table carries `['mcp:read']`, which still satisfies it. What is permitted is
still an allowlist of two, so `mcp:send` stays unexpressible — there is no tool it could
authorise, and the database refusing it is what keeps that true independently of the code. The
downgrade **revokes** any propose-scoped token rather than silently stripping the scope: a
credential whose permissions change under it while it keeps working is worse than one that stops.

**A correction to the command this repo published yesterday.** `docs/MCP_PLAN.md` gave the smoke
check as `sudo docker compose -f deploy/compose.prod.yml exec api ...` — **without
`--env-file .env`**, which every other Compose command in these docs carries. `.env` on the
server is root-owned and Compose interpolates it at parse time, so the command fails with six
`required variable ... is missing a value` lines that read like a broken config rather than a
missing flag. Fixed, with a note saying why, since it is the easy mistake to repeat.


**Every message had been claiming RFC 8058 compliance it did not have (ADR-033, slice S11.0).**
First slice of the v3 plan, and deliberately first of all of them: it is one line of behaviour and
it removes a false statement from every email this system sends.

`List-Unsubscribe-Post: List-Unsubscribe=One-Click` was set on every message while
`List-Unsubscribe` was a bare `mailto:`. RFC 8058's one-click is about an **https URI a receiver
can POST to**; the header beside a mailto announces a capability that is not there. The comment
next to it read "tells a client the mailto is safe to trigger without a confirmation step", which
is a misreading of the RFC -- one-click is not "no confirmation", it is "POST here and we will act
on it".

**Why this outranked the queue work it was planned behind.** Most wrong headers are inert. This one
is *testable by the receiver*: Gmail can POST, find nothing, and conclude the sender misrepresents
its own compliance -- against the exact criteria that decide whether our mail is delivered. We were
claiming the one bulk-sender rule we had not implemented.

The header is now sent only when an https URL exists, which until S11.4 means not at all. The
mailto stays, so every message still carries an unsubscribe mechanism; it is just no longer
described as something it is not. With a URL, `List-Unsubscribe` carries it first and the mailto
second, because a receiver takes the first target it understands. A non-https URL is refused rather
than normalised: `http` would have a receiver POST somebody's opt-out in clear.

**And the existing test asserted the defect.** `test_one_click_is_advertised` checked that the
header was present. That is the fourth time in this project a test has encoded the bug it should
have caught -- after `parse_chain`'s empty chain, the mailbox-credential fallback, and the
`tools/list` ordering assertion that compared a list with itself -- and it keeps happening for one
reason: a test written from the implementation describes the implementation.
**Job deduplication was not workspace-scoped, and a second workspace's work would have
vanished silently (ADR-032, slice S11.1).** The second of the three items to land before any new
job kind.

`jobs.dedupe_key` plus a partial unique index is what makes enqueueing idempotent, and
`enqueue` returning `None` for a duplicate is how that stays harmless. The index was unique on
**`(kind, dedupe_key)`** with no `org_id` -- so a live job in one workspace refused the same key in
**every other workspace**, and the refusal arrives as `None`, which every caller in the tree reads
as "already queued, nothing to do". No exception, no log line, no dead job, nothing on any screen.
The quietest failure available, and a cross-tenant one in a schema built on ADR-004.

**It had never bitten because every key in the tree is built from a UUID** -- `draft:{lead.id}` --
and a UUID cannot collide across orgs. The guarantee held by accident of key choice rather than by
design. It stops holding the moment a key is built from something two workspaces share:
`feed:greenhouse:{token}`, `company:{domain}`, `poll:{ats_slug}` -- which is exactly what the
job-post intake and signal polling need. Two users watching the same company is the ordinary case
in this product, not an edge one.

The index is now `(org_id, kind, dedupe_key)`. **Three places expressed the old rule and all three
had to change together**: the migration, the `Index` in `models.py` metadata -- without which
autogenerate reads it as drift and writes a migration that DROPS it, which it has already done
once -- and `index_elements` in `queue.py`, because Postgres infers the constraint from those
columns and a mismatch is rejected outright.

**The migration is index-only and creates the new index before dropping the old**, so at no instant
is the guarantee weaker. The downgrade needed care the upgrade did not: the old index is
*stricter*, so if two workspaces hold the same key -- the case this change allows -- recreating it
would fail. The revert cancels the newer duplicates with the reason in `last_error` rather than
deleting them, because a cancelled job is visible and re-enqueueable where a deleted one is gone.
Both directions were run against the development database and the index checked each way.

**A mistake on the way, worth recording because the recovery was the easy part.** I ran
`alembic downgrade -1` to test reversibility without checking where the development database
actually was -- it was one revision behind head, so that dropped `api_tokens` instead of reverting
the new migration. `upgrade head` restored it, and the run incidentally proved all three pending
upgrades apply cleanly in order. The lesson is the cheap one: `alembic current` before
`alembic downgrade`, because `-1` is relative to the database and not to the branch.

**A worker that lost its lease could still mark a job succeeded, so a handler could run twice
(ADR-032 addendum, slice S11.3).** Found by writing the crash-safety test the plan called for,
which is the argument for writing it: the v3 doc listed F21's crash safety as something to build,
the reality check found `reap_expired` already existed and was already called by the `Scheduler`,
so the slice was a test -- and the test found a real hole next door.

`queue.succeed()` had no ownership check, and `JobRunner._keep_lease` **deliberately tolerates a
failed heartbeat** -- the comment says so, and it is right to, because a reaped-and-retried job is
the queue working as designed. Together they left this: worker A claims a job, its lease lapses,
`reap_expired` requeues it, worker B claims it and runs the handler, and then worker A finishes and
records success for a job it no longer owns. **The handler ran twice and nothing about the queue
looked wrong.** Nothing was ever *sent* twice -- that is the outbox's unique `idempotency_key`,
tested with two concurrent claims -- but every other side effect was duplicated.

`succeed()` now takes `worker_id`, scopes the UPDATE by `locked_by`, and returns whether the row
was still ours. The runner passes its own id and raises on a `False` **inside** the transaction, so
the handler's work is rolled back with the outcome it could not record. Only the worker that still
owns the job leaves anything behind. The lost attempt is deliberately not routed through
`_finish`: stamping an error there would mark a job the new owner is running and burn an attempt
belonging to them, so it is counted as `lost`, separately from `failed`, because nothing went
wrong with the work.

**Red before green, on CI rather than locally.** The tests were pushed first with no fix, and CI
recorded the discriminating failure in the right words -- *"a worker that no longer owns the job
committed its work anyway"* -- while the `TestNothingIsLost` half passed, confirming the lease
really did already return a vanished worker's job.

**Why red-on-CI rather than red locally, and a real cost worth recording.** The local integration
database breaks in a cascade: each interrupted run leaves a backend `idle in transaction` holding a
lock on `app.events`, and every later `TRUNCATE` queues behind it forever. `pg_stat_activity` told
the story -- Postgres at 0.03% CPU with sixteen blocked backends. That one cause had been
masquerading as several different bugs for most of a session: deadlocks on the cleanup statement,
foreign-key violations from stale snapshots, 500-second timeouts, and a `test_claiming_leases`
failure recorded earlier in these notes as unexplained. **It is explained now, and it was never
product code.** It is written up as DEV-1 in `ISSUES.md` with three candidate fixes; the cheapest,
a `lock_timeout` on the cleanup connection, would have turned hours of confusion into one clear
error.

**And one self-inflicted lesson about exit codes.** A commit went out unformatted because the
chain `ruff format --check . | tail -1 && git commit` reads as a pass: a pipeline's exit status is
the *last* command's, and `tail` always succeeds. Check commands do not get piped from here on.

**One workspace's import could starve every other workspace's queue (ADR-032 addendum 2, slice
S11.2). F21 is complete.**

`claim` ordered by `(priority, run_after)` across the whole table, so a workspace that imported a
thousand leads held the thousand oldest `queued` rows and every claim for the next while was
theirs. A second workspace's single urgent job waited behind all of it -- correct by that
ordering, useless by any other measure.

**Round-robin across workspaces**, by ranking each workspace's due jobs among its own and taking
everybody's first before anybody's second. Ordering within a workspace is untouched, so a priority
still means what it meant. The same answer PR #67 reached for two campaigns sharing one mailbox,
and for the same reason: with one shared allowance, strict first-in-first-out lets a large holder
starve a small one for a working week.

**A queue cap is deliberately not the mechanism, and that is the judgement in this slice.** An
import of a thousand leads legitimately creates a thousand jobs, spread over the days they could
actually be sent. Refusing that would break the product to solve a problem the ordering already
solves. So `MAX_QUEUED_JOBS_PER_ORG` is a runaway guard at 20,000 -- far above any real import --
and it raises `QueueFull` rather than returning `None`, because `None` means "already queued" to
every caller and would silently drop work, which is exactly the bug the first half of this ADR
fixed.

The import route catches it and answers 429 with the reason, rolling the whole import back rather
than leaving leads half-queued -- a lead with no research job sits `NEW` for ever, looking imported
and doing nothing. Caught rather than left to escape, because a bare exception reaches FastAPI's
`ServerErrorMiddleware` *outside* `CORSMiddleware`, and the dashboard would report the backend
unreachable while it answered perfectly. That trap has already cost this project one long
investigation.

**Verified against real Postgres, not only in tests.** `FOR UPDATE ... SKIP LOCKED` alongside a
window-function subquery join is SQL a type checker cannot judge, so a probe ran it against the
development database: four jobs in one workspace, one in another, a claim of two returned one from
each. Worth noting because the local integration suite is still unusable (DEV-1), so this is the
evidence that the statement works at all rather than merely compiles.

**One-click unsubscribe is real now, and it is the only unauthenticated write in the system
(ADR-033 part 2, slice S11.4).** S11.0 removed the false claim; this earns the claim back.

`POST /u/{token}` and `GET /u/{token}`, at the root outside `/api/v1`, because a recipient must be
able to act without an account and a receiving mail server POSTs the URL by itself.

**POST acts, GET does not**, and that split is the point rather than pedantry. Mail clients,
security scanners and link-preview crawlers fetch URLs they find in a message; if GET
unsubscribed, a scanner opening the mail would opt somebody out of a campaign they had not read
and the record would say they chose to. GET returns a page with a button, and the button POSTs.

**The token carries its own proof**: HMAC-SHA256 over `org_id || lead_id`, truncated to 128 bits,
unpadded base64url so nothing in it needs escaping by whatever rewrites links in a recipient's
client. Integrity rather than confidentiality -- anyone holding the email holds the token, which
is the design; what must be impossible is editing one into a different lead.

**The signing key is derived from `CREDENTIAL_KEY` rather than being it.** That key encrypts
mailbox credentials; this signs public URLs. One secret for both means a flaw in either reaches
the other, and deriving under a fixed label costs nothing and adds no secret to manage.

**Every failure answers 200 after doing nothing** -- unknown token, forged token, erased lead. A
public endpoint that distinguishes them is an oracle for which tokens exist, and from the
recipient's side the outcome is identical: they will not be emailed again. `verify()` returns
`None` for every malformed input and raises for none, because a public route that can be made to
raise is a 500 handled outside the CORS middleware, which is the trap that once made a duplicate
key look like an unreachable backend.

**`PUBLIC_API_URL` is its own setting and empty means no one-click header at all.** Deriving it
from `GOOGLE_REDIRECT_URI` would be a public URL that breaks quietly when that value changes, and
a URL built on a guess would have receivers POST opt-outs into the void -- the defect S11.0 fixed,
wearing a different hat. So the deployment opts in by setting one value, and until it does the
behaviour is exactly what S11.0 left: a mailto, and no claim about it.

**mypy caught the one real mistake.** The send loop's `settings` parameter is `CampaignSettings`,
not the deployment config, so `settings.PUBLIC_API_URL` was an attribute that does not exist --
the same shape as `Event.created_at` before it, and again found by the type checker rather than by
a screen showing nothing for ever.

**And CI caught a second one, which is this project's most repeated defect and was mine.** The
route passed `reason="one-click unsubscribe"` to `add_suppression`. `ck_suppressions_reason`
allows exactly four values and that is not one of them, so **every POST was a 500** -- and the
test written alongside it asserted `"one-click" in rows[0].reason`, so it asserted my own invented
value and would never have caught it. A literal that has to match a database constraint, copied
into a caller that cannot see it change: the same shape as `direction == "outbound"` where the
column holds `'out'`, `outcome != "ok"` where success is `"success"`, and `Event.created_at` where
the column is `Event.at`.

The structural cure, rather than just the fix: `SUPPRESSION_REASONS` is defined once in
`models.py`, **the check constraint is built from it**, the route reads it, and
`tests/unit/test_suppression_reasons.py` walks the whole tree with `ast` and fails on any
`add_suppression(reason="...")` literal the database would refuse. Run against the broken line
first, to watch it fail.

The detail did not just get dropped. `reason` says *that* they opted out -- a four-value column is
not where a mechanism belongs -- and a `lead.unsubscribed` event now records *how*, so "why is
this address suppressed?" is still answerable months later.

**A sending domain can now say whether it meets the bulk-sender rules (F4, slice S11.5).**
`GET /admin/sender-requirements`, admin-gated, one report per **distinct domain** rather than per
mailbox -- SPF, DKIM and DMARC are properties of a domain, and two mailboxes on one domain listed
twice would show one problem as two.

**The whole thing is built around one honesty rule: say what DNS can prove and no more.** That is
not pedantry here, it is the defect this project has already paid for. Mail went out as
`From: rahul@arkvane.in` while SMTP authenticated as an unrelated Gmail, so the SPF and DKIM
records verified for that domain applied to none of it -- and the first reply the system ever
received was sitting in Spam. A report that had said "DKIM: pass" would have been true about the
record and wrong about the mail.

So: **SPF** is checkable, and two records is reported as the permanent error it is rather than a
merge -- the single commonest way a domain's mail breaks. **DKIM is only probeable**: DNS cannot
enumerate selectors, so a miss is `UNKNOWN` ("not at the selectors we tried") and a key that is
published is reported as *published*, with the reader sent to Show original on a received message
for whether anything is signing with it. **DMARC** with no `rua=` is a warning rather than a pass,
because aggregate reports are the one deliverability signal that works at our volume where
Postmaster Tools shows nothing (ADR-029). And **a failed lookup is `UNKNOWN`, never `FAIL`** --
a timeout is not evidence of absence, the same rule the importer's MX check already follows.
`ready` treats `UNKNOWN` as not ready, because for a go-live gate "we could not tell" is not a yes.

**No key, address or token appears in any detail string**, asserted by a test: these reach a
browser, a screenshot and a log.

**This route does talk to the network, and the contrast with `GET /mailboxes` is deliberate.**
That one is non-networked because refreshing it would mint OAuth tokens and could write an
`invalid_reason` -- a read with a side effect, fired by anyone pressing F5. A DNS lookup has no
side effect, and the resolver caches per request, so a refresh costs a few UDP packets.

`dnspython` turned out to be **already a dependency** (the importer's MX check uses it), so the
implementation plan's note about adding one was wrong -- corrected there. The checker is a shared
function precisely so P10's go-live script reads the same code: a screen and a script that
disagree about whether a domain is ready would be the signature defect again.

**One test of mine was a bad proxy for its claim.** It asserted the word "signed" was absent from
the DKIM detail, and failed against a sentence that uses it to *deny* signing. Asserting the
absence of a word is a poor stand-in for asserting a claim; it now checks that the detail says
"published" and defers to a received message.

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

**Deliverability monitoring, and a smoke check for the live MCP endpoint (ADR-029,
`docs/POSTMASTER.md`).**

**First, the thing that was reported as missing and turns out not to be.** "I can't see any token
or anything in our website related to the Claude connector" — it is there, and it is live. The
panel is **Settings → Connect your own Claude**, between Team and the suppression list. Verified
rather than assumed, from outside: the published dashboard bundle contains the panel and its
`/mcp-tokens` calls, `GET /mcp` on the live API answers **405** and an unauthenticated `POST`
answers **401**, so both halves of PR #116 are deployed. What it needs is an owner or admin role,
because the whole Settings screen is gated — a `member` does not see the nav item.

**Postmaster Tools is set up by hand and deliberately not integrated.** Reputation attaches to
the **sending domain**, and after ADR-023 this deployment has no mailbox of its own — every
workspace sends from an account its owner connected, under its own domain. So there is no
deployment-wide view of deliverability and there cannot be one: reading a tenant's reputation
would need credentials for their domain. The product change is therefore **one sentence and a
link** beside the sending account, naming the workspace's own domains, rather than a panel whose
numbers would require holding an OAuth credential for every tenant's Google account to redraw a
dashboard Google already draws for them.

**Two findings that change what to expect of it, which is more useful than the setup steps.**

1. **It will show nothing at our volume, probably for months.** Google withholds low-volume data
   by design, and counts only mail to `@gmail.com` — a founder on their own Workspace domain does
   not appear at all. Against the locked 100/day/mailbox, warming up from 20, the qualifying
   subset is tens of messages a day against an unofficial threshold in the hundreds. **The reason
   to do it today is that it has no backfill**: verify after the first campaign and the first
   campaign is the one you cannot see. Precisely ADR-026's argument for the outcome dataset.
   Worth knowing for a second reason too — Google says data may also be withheld when reputation
   is *particularly bad*, so "no data" and "bad data" look alike from outside.
2. **The signal that works at any volume is already configured and read by nobody.** DMARC
   aggregate reports arrive daily from every participating receiver and are not volume-gated.
   They would show the `From`-versus-authenticated-account mismatch this repo has warned about
   since Phase 9 — mail sent as `rahul@arkvane.in` while SMTP authenticates as an unrelated
   personal Gmail — immediately and unmistakably, as a source failing both SPF and DKIM for our
   own domain. And `rua` points at `rahul@arkvane.in`, an address this system both **sends from**
   and **polls**, so the reports are ingested as orphaned inbound messages until the hourly sweep
   deletes them at 45 days. Harmless, because nothing matches a lead and no model is ever called
   on them, and still the owner's correspondence sitting in application data for no reason.
   `p=none` stays until the reports are clean: tightening first would have receivers quarantine
   our own real mail.

**`app/scripts/mcp_smoke.py` exercises the deployed endpoint over the public URL.** The 24
integration tests drive a `TestClient` against a test database in the same process, so they
cannot see TLS, Caddy, a forwarded `Origin`, or a route mounted at the root rather than under
`/api/v1`. Six defects surfaced on this project's first real deploy; every one was invisible
locally and in CI and four lived in exactly that gap. The check covers the half a happy path
never looks at — no credential, a garbage one, a **Supabase-shaped** one (ADR-028's two-doors
rule, asserted against the live deployment), a disallowed `Origin`, the legacy `GET` and
`DELETE` — then breaks each 2026-07-28 validation rule on purpose (`-32020` on a header that
disagrees with the body, `-32022` on an old version, `404`/`-32601` on an unknown method), then
`server/discover`, the catalogue and every tool in it. It also asserts on the wire that **no
tool's schema takes an `org_id`**, which is the one tenancy mistake invisible from inside a
single workspace, and that `tools/list` is ordered stably **across two calls** — the property
that replaced the assertion which compared a list with itself.

**It mints a real credential against the real database, which is the one dangerous thing about
it.** So two properties are enforced by `tests/unit/test_mcp_smoke_script.py` rather than left to
care: the secret is **never printed**, because this output is pasted into terminals and chat
windows and hard rule 2 has already been breached once here by a failing assertion; and the token
is **revoked in a `finally` block**, because the run this matters for is the one that fails, and a
check that cleans up only on success leaves a live key behind exactly when nobody is reading the
output. A one-day expiry is the third defence, for a process killed between the two. The guard
was run against the absent script first, to watch all six assertions error.

**And the tool list is read from the catalogue rather than restated**, asserted by that same
guard. A hand-written list inside the checker is a second copy of the thing being checked, which
is this project's signature defect wearing a lab coat.

**And it was run, which is the only reason any of the above is a claim rather than a hope.**
Against a local API on the dev database: **29 checks, all passing, exit 0, token revoked.** Two
things came out of running it that reading it would not have given:

- **The audit assertion was added because of what the run showed.** It is the only check here
  that reads the database rather than the response, and it exists because writing the audit row
  is deliberately wrapped and non-fatal — so a trail that has quietly stopped being written looks
  exactly like one that is working. It reports **7 events for 7 calls**, and three consecutive
  runs each reported 7 against a table holding 21, which is how the time window was shown to
  discriminate rather than to count everything.
- **mypy caught a column that does not exist.** The audit query was written against
  `Event.created_at`; the column is `Event.at`. The signature defect again — a literal that has
  to match the schema — caught by the type checker before commit this time rather than by a
  screen reporting zero for ever. Worth noting only because the previous two instances of it, in
  the platform router eleven lines apart, were found by reading a check constraint, and a tool
  that finds this class automatically is cheaper than the discipline that was needed last time.

The cosmetic finding: the minted token's prefix was printed with a Unicode ellipsis, which a
Windows console renders as a replacement character in the middle of a credential prefix. Plain
ASCII now — this output is read in a container shell and in a terminal, and a credential prefix
that looks corrupted is one somebody will think they mistyped.
