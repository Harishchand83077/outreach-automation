# Deploying

This is the runbook for putting the api and the worker on a VM, pointing the Cloudflare
dashboard at them, and getting mail to arrive in inboxes rather than spam folders.

**Nothing here has been run.** Every command below was written from the configuration in this
repository, not from a successful deploy — the assistant that wrote it is not permitted to create
cloud resources or spend money (hard rule 5), so the first run of each step is yours. Where a
step is likely to behave differently than described, it says so.

Read the whole of §1 before starting. The order matters in two places and both are annoying to
undo: DNS must resolve before Caddy first starts, and `POSTGRES_PASSWORD` takes effect only on
the very first start of the database volume.

---

## 0. What runs where

| Thing | Where | Why |
|---|---|---|
| Dashboard (React) | **Cloudflare Workers** | Static assets, free, no server to patch. `ARCHITECTURE_TARGET.md` §3. |
| API (FastAPI) | VM, behind Caddy | Needs the database and the secrets. |
| Worker (five asyncio tasks) | VM, same image | The only process that sends mail. |
| Postgres 17 | VM, same Compose stack | ADR-001. No published port; reachable only inside the Compose network. |
| Caddy 2 | VM | TLS, obtained and renewed by itself. |
| Auth | Supabase | Auth **only** (ADR-001). No table is read through it. |

One image for the api and the worker, two commands. They cannot drift apart: a deploy updates
both or neither, and a schema the api expects is a schema the worker has.

---

## 1. The VM

### 1.1 Which provider

In the order `MASTER_PROMPT.md` gives:

1. **Oracle Cloud Always Free** — an Ampere A1 (ARM) instance with up to 4 OCPU and 24 GB RAM,
   free indefinitely. By far the most capable free tier, and the one these instructions assume.
2. Google Cloud `e2-micro` — free, but 1 GB of RAM is tight for Postgres plus two Python
   processes. Workable with the memory limits in `compose.prod.yml`; not comfortable.
3. A ~₹400/month VPS (Hetzner, DigitalOcean, Linode). Simpler, predictable, costs money.

**Oracle's free tier is reclaimable.** Oracle may stop an Always Free instance that is idle, and
"idle" is measured on CPU, network and memory — this workload is idle by that definition for most
of the day. The nightly off-box backup in §7 is what makes that survivable, and it is the reason
it is not optional.

**ARM matters for one reason only:** every image in the stack (`python:3.12-slim-bookworm`,
`postgres:17-alpine`, `caddy:2.10-alpine`) is multi-architecture, so `docker compose build` on an
Ampere instance just works. If you add an image later, check it has an `arm64` tag before you
rely on it.

### 1.2 Create it

- **Shape:** VM.Standard.A1.Flex, 2 OCPU, 12 GB (inside the free allowance, and leaves room).
- **Image:** Ubuntu 24.04 LTS (not Oracle Linux — the commands below assume `apt`).
- **Boot volume:** 50 GB. The free allowance is 200 GB total; this is comfortable for the
  database, fourteen days of dumps and the images.
- **SSH:** paste your public key. Oracle does not offer password login, which is correct.

Record the **public IPv4 address** when it appears. Reserve it (Oracle: "Reserved public IP")
rather than leaving it ephemeral, or a stop/start will change it and the DNS record will be wrong.

### 1.3 Open the firewall — twice

This is the step that wastes an afternoon, every time, for everyone.

Oracle has **two** firewalls, and the Ubuntu image ships with the second one closed:

1. **Security List / NSG** (in the Oracle console, on the subnet): add ingress rules for
   TCP **80** and TCP **443** from `0.0.0.0/0`.
2. **The instance's own iptables.** Oracle's Ubuntu images come with a default `INPUT` policy
   that drops everything except SSH, and it persists across reboots:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Do **not** open 5432, 5433 or 8000. Nothing outside the Compose network needs them: Postgres
publishes no port at all, and the api is reached only through Caddy.

Check from your own machine, not from the VM: `nc -vz <ip> 443` should connect once Caddy is up.

### 1.4 Install Docker

```bash
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and back in for this to take effect
docker compose version            # expect v2.x
```

### 1.5 Unattended security updates

A VM nobody logs into for three months is a VM with three months of unpatched kernel.

```bash
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

---

## 2. DNS

Point a name at the VM **before** starting Caddy. Caddy asks Let's Encrypt for a certificate on
first start, Let's Encrypt resolves the name and connects back on port 80, and a failure there
counts against a rate limit that is measured in hours.

| Record | Name | Value |
|---|---|---|
| A | `api.yourdomain.com` | the VM's public IPv4 |

Wait until `dig +short api.yourdomain.com` returns the address from somewhere other than the VM.

**Use a subdomain, not the apex.** The dashboard lives on Cloudflare and the api lives here;
giving them separate names means either can move without touching the other.

If the domain is on Cloudflare DNS, set this record to **DNS only** (grey cloud), not proxied.
Proxying it would put Cloudflare in front of the api as well, which breaks Caddy's HTTP-01
challenge and hides the real client address from the rate limiter.

---

## 3. The code and the configuration

The repository is **private**. Use a read-only deploy key rather than a personal token: a deploy
key grants one repository, a token grants every repository you can see.

```bash
sudo mkdir -p /opt/outreach && sudo chown "$USER" /opt/outreach
ssh-keygen -t ed25519 -f ~/.ssh/outreach_deploy -N ''
cat ~/.ssh/outreach_deploy.pub     # add this under Settings → Deploy keys, read-only
git -c core.sshCommand='ssh -i ~/.ssh/outreach_deploy' \
    clone git@github.com:Harishchand83077/outreach-automation.git /opt/outreach
cd /opt/outreach
git config core.sshCommand 'ssh -i ~/.ssh/outreach_deploy'
```

### 3.1 `.env`

```bash
cp env.example .env
chmod 600 .env
nano .env
```

`env.example` documents every variable. The ones that must be right for a first deploy:

| Variable | Value on the VM | Note |
|---|---|---|
| `APP_ENV` | `production` | Closes `/docs` and switches logs to JSON. |
| `SEND_MODE` | `disabled` | **Leave it.** §8 is where this changes. |
| `POSTGRES_PASSWORD` | a long random string | `openssl rand -base64 32`. See the warning below. |
| `API_DOMAIN` | `api.yourdomain.com` | Caddy issues the certificate for this name. |
| `ACME_EMAIL` | a real address | Where Let's Encrypt sends expiry warnings. |
| `DASHBOARD_ORIGIN` | the Cloudflare URL | Informational; CORS is set below. |
| `CORS_ALLOWED_ORIGINS` | the Cloudflare URL | **Exact origin**, scheme included, no trailing slash, never `*`. |
| `SUPABASE_URL`, `SUPABASE_JWKS_URL`, `SUPABASE_ANON_KEY` | from the Supabase project | The api verifies tokens against the JWKS locally. |
| `GROQ_API_KEY`, and any other provider keys | yours | Entries with no key are skipped by the router. |
| `SMTP_*`, `IMAP_*` | the sending mailbox | §6 before you trust these. |
| `ALERT_WEBHOOK_URL` | optional | Without it, alerts are visible only in the dashboard. |

**`POSTGRES_PASSWORD` takes effect only on the first start of the data volume.** Changing it
later does nothing, and the application then fails to authenticate against a database that still
has the old one. Fixing that means `down -v`, which destroys the data. Set it once, correctly.

`DATABASE_URL` in `.env` is **ignored** for the api and the worker: `compose.prod.yml` overrides
it to reach `postgres:5432` on the Compose network. That is deliberate, so the same `.env` shape
works on a developer machine (where it points at `127.0.0.1:5433`) and on the server.

### 3.2 Check it without printing it

```bash
docker compose --env-file .env -f deploy/compose.prod.yml run --rm --no-deps -T api \
    python -m app.scripts.check_env
```

This prints `OK` or `MISSING` per variable **name** and never a value (hard rule 2). It also
refuses placeholders copied from the template, which is the failure the prototype shipped with:
`your_groq_api_key_here` passed validation and the dashboard reported itself correctly configured
(issue B3).

---

## 4. Deploy

```bash
cd /opt/outreach
./deploy/deploy.sh
```

It refuses a dirty working tree, builds, **runs `alembic upgrade head` before starting the new
containers**, starts them, waits for `/health`, and puts the previous image back if the api never
answers. It never touches `SEND_MODE` and never runs `down -v`.

Then, from your own machine:

```bash
curl -s https://api.yourdomain.com/health   # {"status":"ok"}
curl -s https://api.yourdomain.com/ready    # 503 until the worker's first heartbeat, then 200
```

`/ready` being 503 for the first minute is correct and is the point of it: it checks the database
*and* the worker's heartbeat, because **an api that serves perfectly while the worker is dead
looks healthy and sends nothing**. If it is still 503 after two minutes:

```bash
docker compose --env-file .env -f deploy/compose.prod.yml logs --tail 50 worker
```

### 4.1 The first org and the first member

Access is a database row: a valid Supabase token with no `members` row gets no org and therefore
no data. Public sign-ups are disabled, so there is no form that creates one.

```bash
docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres \
    psql -U outreach -d outreach
```

```sql
INSERT INTO app.orgs (id, name) VALUES (gen_random_uuid(), 'Your Agency') RETURNING id;
-- the `sub` claim of your Supabase user, from the Supabase dashboard → Authentication → Users
INSERT INTO app.members (id, org_id, user_id, email, role)
VALUES (gen_random_uuid(), '<org id from above>', '<supabase sub>', 'you@yourdomain.com', 'owner');
```

Only `owner` is issued today. `admin` and `member` exist in the check constraint so inviting
people later is a row rather than a migration.

### 4.2 Point the dashboard at it

In the Cloudflare project's **build variables** (not in this repository — they are compiled into
the bundle and are public by design):

| Variable | Value |
|---|---|
| `VITE_API_URL` | `https://api.yourdomain.com` |
| `VITE_SUPABASE_URL` | the Supabase project URL |
| `VITE_SUPABASE_ANON_KEY` | the Supabase anon key |

Then redeploy the Cloudflare project. Without `VITE_API_URL` the dashboard asks its own origin
for `/api/v1`, gets `index.html` back with a **200**, and shows
`Unexpected token '<', "<!DOCTYPE "... is not valid JSON` — which is why `api.js` now reports
that case as "the API did not answer" and names this variable.

Add the dashboard's exact origin to `CORS_ALLOWED_ORIGINS` in `.env` and restart the api. The
allowlist is never `*`: the prototype sent `allow_origins=["*"]` with credentials enabled, a
combination browsers reject outright (issue C3).

---

## 5. Logs, alerts and uptime

### 5.1 Logs

JSON, one object per line, on stdout. With `APP_ENV=production` every line carries `request_id`,
and lines written while a job runs also carry `job_id`, `org_id` and `lead_id` — so a user saying
"it failed" can be traced to the request, the job and the lead without guessing from timestamps.

```bash
docker compose --env-file .env -f deploy/compose.prod.yml logs -f worker
docker compose --env-file .env -f deploy/compose.prod.yml logs api | grep '"level":"ERROR"'
docker compose --env-file .env -f deploy/compose.prod.yml logs api \
    | grep '"request_id":"abc123"'
```

Rotation is configured in `compose.prod.yml` (10 MB × 5 per service). Without it, a full disk
stops Postgres, not just the logging.

Secrets are redacted from log lines as a second line of defence: any configured key, password or
webhook URL is replaced with `***` wherever it appears, including inside the text of an exception
raised by a driver that was handed a connection string. The first line of defence is still not
logging them.

### 5.2 Alerts

The worker writes alerts to the `alerts` table and pushes each one once to `ALERT_WEBHOOK_URL` if
it is set. The table is the record; the webhook is the nudge — a push failure is logged and never
propagates, because the system must not stop working *because* it tried to say it was in trouble.

Repeats of the same condition are suppressed for an hour, and acknowledging an alert re-arms it.
See them in the dashboard's admin view, or:

```
GET /api/v1/admin/alerts?unacked=true
GET /api/v1/admin/worker       # the worker's last heartbeat and per-task detail
```

### 5.3 Uptime

Point a free external monitor (UptimeRobot, Better Stack, Healthchecks.io) at
`https://api.yourdomain.com/ready` every five minutes. **`/ready`, not `/health`** — `/health`
only says the api process is up, while `/ready` fails when the database is unreachable or the
worker's heartbeat is stale. The failure worth being woken for is the silent one: a worker that
died at 2am and sent nothing all day.

---

## 6. Deliverability: SPF, DKIM, DMARC

This section closes issue **D5** (the runbook ADR-006 promised) and finishes **C6**. It has a
lead time measured in days, not minutes — start it before you intend to send.

**What is already settled** (ADR-006): the sending mailbox is **Google Workspace (paid Starter)**
on a **dedicated domain** whose DNS is at **GoDaddy**. So the separate-sending-domain question —
`MASTER_PROMPT.md` Q4 — is answered, and the client-facing domain is not at risk from cold-mail
reputation. What remains is authenticating that domain, which is the one part of deliverability
no amount of code can substitute for.

Two prerequisites from ADR-006, worth confirming before anything else:

- **IMAP is enabled** in Workspace (Admin → Apps → Google Workspace → Gmail → IMAP access). It is
  off by default on some plans, and the reply engine sees nothing without it.
- **`SMTP_PASSWORD` is an App Password**, which requires 2-Step Verification on the account. A
  normal account password is rejected.

### 6.1 The three records, at GoDaddy

GoDaddy: **My Products → Domain → DNS → Manage Zones → Add New Record**. Replace
`outreachdomain.com` with the sending domain. Verify each with `dig +short TXT <name>` from your
own machine before moving to the next.

A GoDaddy quirk worth knowing: its panel shows the **Name** relative to the domain, so the name
for a record at the apex is `@`, and for DKIM it is `google._domainkey` — not the full hostname.
Pasting the full name produces `google._domainkey.outreachdomain.com.outreachdomain.com`, which
is a silent failure.

**SPF** — one TXT record, Name `@`. Exactly one: two SPF records is a permanent failure, not a
merge. If GoDaddy's default parking record already has one, **edit it**, do not add a second.

```
Type: TXT   Name: @   Value: v=spf1 include:_spf.google.com ~all   TTL: 1 hour
```

`~all` (softfail) while testing. Tighten to `-all` once mail is arriving correctly.

**DKIM** — generated by Google, not by you. Admin console → Apps → Google Workspace → Gmail →
**Authenticate email** → select the domain → **Generate new record** (choose **2048**-bit; the
default is 1024). Publish what it gives you at GoDaddy:

```
Type: TXT   Name: google._domainkey   Value: v=DKIM1; k=rsa; p=MIIBIjANBg...   TTL: 1 hour
```

Then return to the admin console and press **Start authentication**. This is the step everyone
forgets: the record alone signs nothing, and until this is pressed every message goes out
unsigned. Google may take up to 48 hours to see the record; it will say so rather than fail.

**DMARC** — start at `p=none` with reporting, so you can see what fails before asking receivers
to reject anything.

```
Type: TXT   Name: _dmarc   Value: v=DMARC1; p=none; rua=mailto:dmarc@outreachdomain.com; fo=1; adkim=r; aspf=r   TTL: 1 hour
```

Make sure that `rua` mailbox exists, or the reports go nowhere. After a week of clean reports move
to `p=quarantine`; only then consider `p=reject`.

### 6.2 Verify before the first send

- Send one message to `mail-tester.com` (or to a Gmail address and use **Show original**) and
  confirm **SPF: PASS, DKIM: PASS, DMARC: PASS** — all three, on the message *as received*.
- Confirm there is exactly one SPF record: `dig +short TXT outreachdomain.com | grep spf1`.
- Send to one Gmail and one Outlook address you control. Both must land in the inbox, not spam.

Do not go to §8 until all three pass. Everything the application does for deliverability — plain
text, no tracking pixels, no link rewriting, `List-Unsubscribe`, the footer, jittered spacing,
weekends off, warm-up, the daily cap — is undone by an unauthenticated domain.

### 6.3 What the app password costs, honestly (issue C6)

ADR-006 resolved C6 rather than merely mitigating it: on a **paid** Workspace account with its
own domain, SMTP relay is a supported use rather than something tolerated, and the roughly
2,000 messages/day allowance leaves real headroom above our 100 — so the cap is a deliverability
choice of ours, not a wall we are near.

The residual costs, recorded rather than assumed: an App Password is a long-lived credential with
no scopes, and SMTP gives no per-message feedback beyond the server's response. Mitigations are
already in the code — one account per provider per org and never a rotation to evade a quota
(hard rule 8), the 100/day cap counting *all* outbound, warm-up, jitter, weekends off, and
auto-pause on a bounce spike or repeated auth failures.

Moving to a transactional provider (Amazon SES, Postmark, Resend) is a change of `SMTP_HOST`,
`SMTP_EMAIL` and `SMTP_PASSWORD` and nothing else — the application speaks plain SMTP
deliberately. The Gmail API with OAuth was considered and rejected in ADR-006: better limits, but
it needs a Cloud project, a consent screen and a token-refresh path for a volume SMTP handles
comfortably.

---

## 7. Backups, restore, retention

### 7.1 Nightly dump

```bash
crontab -e
```

```cron
17 2 * * *  cd /opt/outreach && ./deploy/backup.sh >> /var/log/outreach-backup.log 2>&1
```

Fourteen days are kept in `/opt/outreach/backups`, `chmod 600`, and the directory is gitignored —
every dump is a complete copy of every lead, every email body and every reply.

### 7.2 The weekly off-box copy is the one that matters

ADR-001 put the database on this VM, which removed five problems and introduced one: **the
database dies with the box.** No point-in-time recovery, no vendor holding a copy. On an Oracle
Always Free instance that can be reclaimed for idleness, this is a real scenario, not a
hypothetical.

```bash
scp -r <vm>:/opt/outreach/backups/ ~/outreach-backups/
```

### 7.3 Restoring — do this once now, before you need it

A backup nobody has ever restored is a hypothesis. Restore into a *scratch* database, never over
the live one:

```bash
gunzip -c backups/outreach-20261011T021700Z.sql.gz > /tmp/restore.sql
docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres \
    psql -U outreach -d postgres -c 'CREATE DATABASE restore_drill'
docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres \
    psql -U outreach -d restore_drill < /tmp/restore.sql
docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres \
    psql -U outreach -d restore_drill -c 'SELECT count(*) FROM app.leads'
docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres \
    psql -U outreach -d postgres -c 'DROP DATABASE restore_drill'
rm /tmp/restore.sql
```

A real restore stops the api and the worker first, so nothing writes while the data is replaced:

```bash
docker compose --env-file .env -f deploy/compose.prod.yml stop api worker
# ... restore into `outreach` as above, using the live database name ...
docker compose --env-file .env -f deploy/compose.prod.yml start api worker
```

### 7.4 Retention

`MASTER_PROMPT.md` **Q7** asks how long lead data is kept and the default is **12 months**.
Nothing enforces it yet: the retention sweep, the per-lead erasure endpoint and the rewritten
`PRIVACY.md` are issue **D6**, the next slice of Phase 9. Until then, retention is whatever these
dumps hold — which is one more reason not to leave old ones lying around off-box.

---

## 8. Going live

Do **not** set `SEND_MODE=live` as part of a deploy. It is a separate, deliberate act, and
`deploy.sh` is written so it cannot touch it.

The sequence, which is Phase 10:

1. `SEND_MODE=allowlist`, `SEND_ALLOWLIST` set to your own two addresses. Restart the worker.
2. Import a handful of leads, research them, review the drafts, approve, and watch the sends
   arrive. Reply to one from the outside and confirm it is ingested, classified and that the
   follow-up is cancelled in the same transaction.
3. Confirm SPF, DKIM and DMARC all pass **on a message as received** (§6.3).
4. Work through the go-live gate in `MASTER_PROMPT.md` §12 and record the result.
5. Only then, `SEND_MODE=live`, by hand, on the VM, by the owner.

---

## 9. When something is wrong

| Symptom | First thing to check |
|---|---|
| `/ready` is 503, `/health` is 200 | `logs worker`. The heartbeat is stale or the worker never started. |
| `/ready` says `database unavailable` | `ps` — is `outreach-pg` healthy? Then `logs postgres`. A full disk looks like this. |
| Dashboard shows `Unexpected token '<'` | `VITE_API_URL` is unset or wrong in the Cloudflare build. §4.2. |
| Dashboard says "no organisation for your account" | No `members` row for that Supabase `sub`. §4.1. |
| A browser CORS error | `CORS_ALLOWED_ORIGINS` must be the **exact** origin, scheme included, no trailing slash. |
| Certificate never issued | Port 80 blocked (both firewalls — §1.3), or DNS not resolving yet, or the record is Cloudflare-proxied. |
| Nothing is being sent | `check_env` first — `SEND_MODE` is `disabled` by default and that is deliberate. Then the mailbox: a paused mailbox stops sending and raises an alert. |
| Jobs piling up as `dead` | `GET /api/v1/admin/jobs?status=dead` for the error, then `logs worker`. |
| A message stuck in `unknown` | ADR-002: a send whose outcome could not be determined is left for a human rather than retried into a possible duplicate. Check the Sent folder, then resolve it in the dashboard. |

Useful commands:

```bash
C=(docker compose --env-file .env -f deploy/compose.prod.yml)
"${C[@]}" ps
"${C[@]}" logs --tail 100 worker
"${C[@]}" restart worker
"${C[@]}" exec api python -m app.scripts.check_env
"${C[@]}" exec postgres psql -U outreach -d outreach -c 'SELECT status, count(*) FROM app.jobs GROUP BY 1'
```

### Rolling back

`deploy.sh` rolls back automatically if the api never answers. By hand:

```bash
git log --oneline -5
IMAGE_TAG=<previous short sha> docker compose --env-file .env \
    -f deploy/compose.prod.yml up -d --no-build api worker
```

The **schema is deliberately left migrated**. Migrations are forward-only and
backwards-compatible within a release (§19), precisely so that rolling the code back does not
require rolling the data back — automatic down-migrations are how data gets dropped at the worst
possible moment. If a migration itself is the problem, restore from §7.3.
