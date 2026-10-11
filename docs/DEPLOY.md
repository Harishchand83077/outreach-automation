# Deploying

This is the runbook for putting the api and the worker on a VM, pointing the Cloudflare
dashboard at them, and getting mail to arrive in inboxes rather than spam folders.

**Where this stands.** The VM exists, Docker is on it, the repository is cloned, the production
image has been built and run there, and **`api.arkvane.in` resolves to it**. §1 and §2 record
what was done and found.

**One thing is left, and it is yours: `.env` on the VM** (§3.1). It holds every secret, so
nothing automated should write it — and `deploy.sh` refuses to run without it. Once it exists,
§4 is a single command.

`POSTGRES_PASSWORD` is the one value that is painful to get wrong: it takes effect only on the
very first start of the database volume, and changing it later means destroying the data to fix
it. Read §3.1 before typing.

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

**This section is no longer hypothetical.** The instance below exists and the steps in §1.2 to
§1.5 have been run against it. What is recorded here is what actually happened, including the two
places where the plan and reality differed.

### 1.1 The instance that exists

| | |
|---|---|
| Provider | **AWS EC2** (Oracle Cloud sign-up failed on their card/address check) |
| Region | **ap-southeast-2 (Sydney)** — see the note below, it was not a free choice |
| Instance | `i-02ca5f650887fc66c`, name `outreach-automation` |
| Type | `t3.small` — 2 vCPU, 2 GB RAM, x86_64 |
| Image | Ubuntu Server 24.04.4 LTS (`ami-020728ad6199d7fa0`) |
| Storage | 30 GiB gp3 |
| Elastic IP | **3.25.23.191** (allocated and associated, so it survives a stop/start) |
| SSH | `ssh -i ~/.ssh/outreach-automation-key.pem ubuntu@3.25.23.191` |
| Security group | `outreach-automation-sg` — 22 from the owner's IP only, 80 and 443 from anywhere |
| Budget alarm | `outreach-automation-5usd`, notifies at 85%, 100% and forecast-100% of $5/month |

**Sydney, not Mumbai, and that was forced.** Mumbai is the right region for sending to Indian
founders, and it is what this runbook originally said. The AWS account is on the new **Free
Plan**, which pins a project to **one** region, and this one was assigned to Sydney. The console
answers a Mumbai URL with "Region Asia Pacific (Mumbai) unavailable", offering only to activate
advanced features — which upgrades the plan and changes billing, so it was not taken. The cost of
Sydney is roughly 150–200 ms of extra latency to India, which this workload does not notice:
sends are paced minutes apart and IMAP polls every few minutes. Only the dashboard feels it.

**`t3.small`, not `t4g.small`.** ARM would be about 20% cheaper and every image in the stack is
multi-architecture, so it would have worked. It was not taken because the image that has actually
been built and smoke-tested — locally, in CI and on this VM — is amd64, and discovering a missing
`aarch64` wheel during a deploy costs more than the saving. Revisit it if the bill matters more
than the certainty later.

**This is not free forever.** `t3.small` is **not** the old always-free tier (that was
`t2.micro`); under the Free Plan it draws down credits, and at list price it is roughly
**$19/month** in Sydney once they run out. A €4–5/month Hetzner box is the better long-term home,
and nothing in this repository is AWS-specific — the stack is Docker Compose and a Caddyfile, so
moving is a `git clone`, a `.env` and a DNS change.

### 1.2 If you are rebuilding it from scratch

Launch wizard: Ubuntu Server **24.04** LTS (the console now defaults to 26.04 — take 24.04, it is
what `get.docker.com` and these instructions are known to work with), `t3.small`, 30 GiB gp3, a
new key pair (RSA, `.pem`), and a new security group with:

| Port | Source | Why |
|---|---|---|
| 22 | **My IP** | Not `0.0.0.0/0`. The wizard defaults to anywhere; change it. |
| 80 | Anywhere | Let's Encrypt's HTTP-01 challenge needs it, and Caddy redirects to 443. |
| 443 | Anywhere | The API itself. |

Then **allocate an Elastic IP and associate it**. Without one the public address changes on every
stop/start, and DNS silently points at nothing — or worse, at somebody else's instance.

**SSH is pinned to one IP, which will expire.** If your ISP gives you a new address, SSH stops
working and nothing else does. Fix it in EC2 → Security Groups → `outreach-automation-sg` → edit
the port 22 rule → My IP. This is the intended failure: it is far better than leaving 22 open to
the internet.

### 1.3 Outbound mail ports — verified on this instance

AWS throttles outbound **port 25** by default, which is the one thing that would have stopped this
deployment dead. Tested from the instance:

```
port 587 OPEN      ← SMTP submission; this is what the sender uses
port 993 OPEN      ← IMAP; this is what the reply engine uses
port 25  blocked   ← expected on AWS, and we never use it
```

Nothing here needs a support request to AWS. Re-run the check after any instance change:

```bash
timeout 10 bash -c 'cat < /dev/null > /dev/tcp/smtp.gmail.com/587' && echo open || echo blocked
```

### 1.4 Docker

```bash
sudo apt-get update && sudo apt-get upgrade -y
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and back in for this to take effect
docker compose version
```

Installed on this instance: Docker **29.9.0**, Compose **v5.6.0**.

### 1.5 Unattended security updates

A VM nobody logs into for three months is a VM with three months of unpatched kernel.

```bash
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

### 1.6 What is already done on this instance

- Docker and Compose installed; `ubuntu` is in the `docker` group.
- `unattended-upgrades` installed.
- The repository cloned to **`/opt/outreach`**, using a repository-scoped **read-only deploy key**
  (`ec2-outreach-automation (read-only)`, in the repo's Settings → Deploy keys). A deploy key
  rather than a personal token because it grants one repository and nothing else, and revoking it
  is one click. The private half never leaves the VM.
- **The production image builds and runs on this instance** — `docker build` succeeds, the app
  imports, `alembic` runs, and the container is `outreach` rather than root.
- `deploy/compose.prod.yml` validates there, and both scripts parse with LF endings and their
  executable bit intact.

**What is deliberately not done**, because it cannot be done for you:

1. **`.env` does not exist on the VM.** It holds every secret, and nothing automated should ever
   write or read it. §3.1 is yours.
2. **No DNS record points at 3.25.23.191 yet**, so Caddy has not been started: it would ask
   Let's Encrypt for a certificate, fail the challenge, and burn into an hourly rate limit. §2
   first, then §4.

---

## 2. DNS — done

**`api.arkvane.in` → `3.25.23.191`**, an A record with a 600-second TTL, added at GoDaddy and
resolving on both Google and Cloudflare public resolvers. Nothing else needs doing here.

```
$ nslookup -type=A api.arkvane.in. 8.8.8.8   →  3.25.23.191
$ nslookup -type=A api.arkvane.in. 1.1.1.1   →  3.25.23.191
```

**The domain is `arkvane.in`**, whose DNS is at GoDaddy (`ns29/ns30.domaincontrol.com`). An
earlier draft of this runbook guessed `athenaedu.in` from the `Message-ID` in ADR-002's addendum;
that was a test mailbox, not the sending domain.

Why a subdomain and not the apex: the dashboard is served by Cloudflare and the api lives on the
VM, so separate names let either move without touching the other. The apex `arkvane.in` still
points at GoDaddy's parking address (`216.198.79.1`) and was left alone.

### 2.1 If you ever point it somewhere else

Add the record wherever the domain's nameservers say DNS is managed — `nslookup -type=NS` on the
domain answers that. The record is:

| Record | Name | Value | TTL |
|---|---|---|---|
| A | `api` (just the label, not the full hostname) | the server's IP | 600 |

GoDaddy's panel takes the **Name relative to the domain**, so `api`, never `api.arkvane.in` —
the latter creates `api.arkvane.in.arkvane.in`, which silently resolves to nothing.

If the domain is ever moved to Cloudflare DNS, set this record to **DNS only** (grey cloud), not
proxied. Proxying puts Cloudflare in front of the api, which breaks Caddy's HTTP-01 challenge and
hides the real client address from the rate limiter.

**Order matters.** Caddy must not start before the name resolves: it asks Let's Encrypt for a
certificate on first start, and a failed challenge counts against a rate limit measured in hours.

### 2.2 The mail records on this domain were not touched

`arkvane.in` is a live Google Workspace mail domain, and its records are what make mail from it
deliverable. Adding the `api` A record took the zone from 16 records to 17; everything below was
re-checked afterwards and is unchanged:

| Record | Value | State |
|---|---|---|
| MX | `aspmx.l.google.com` + 4 alternates | 5 records, intact |
| SPF | `v=spf1 include:_spf.google.com include:dc-aa8e722993._spfm.arkvane.in ~all` | exactly **one** record |
| DKIM | `google._domainkey`, 2048-bit | published |
| DMARC | `v=DMARC1; p=none; rua=mailto:rahul@arkvane.in` | reporting on, enforcement off |
| apex A | `216.198.79.1` | unchanged |

**This means most of §6 is already done**, and done correctly — one SPF record rather than two
(two is a permanent failure, not a merge), DKIM published, and DMARC starting at `p=none` with
reporting, which is exactly the right opening posture. §6 is now a verification step rather than
a setup step.

---

## 3. The code and the configuration

**Already done on this instance** (§1.6): the repository is at `/opt/outreach` and pulls with a
repository-scoped, read-only deploy key. To bring it up to date:

```bash
cd /opt/outreach && git pull --ff-only
```

If you ever rebuild the VM, this is how it was set up. A deploy key rather than a personal access
token, because it grants exactly one repository and revoking it is one click; the private half
never leaves the VM.

```bash
sudo mkdir -p /opt/outreach && sudo chown "$USER" /opt/outreach
ssh-keygen -t ed25519 -f ~/.ssh/outreach_deploy -N ''
cat ~/.ssh/outreach_deploy.pub     # add under Settings → Deploy keys, **read-only**
git -c core.sshCommand='ssh -i ~/.ssh/outreach_deploy' clone \
    git@github.com:Harishchand83077/outreach-automation.git /opt/outreach
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
| `API_DOMAIN` | `api.arkvane.in` | Caddy issues the certificate for this name. The record exists (§2). |
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

**Do not add `--project-directory`.** Every relative path inside `compose.prod.yml` resolves
against it — the build context `..` as well as `env_file: ../.env` — so Compose's default, the
compose file's own directory, is the only value under which both mean the repository root.
Pinning it to the repository root makes the build look for a Dockerfile in `/opt` instead.


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
curl -s https://api.arkvane.in/health   # {"status":"ok"}
curl -s https://api.arkvane.in/ready    # 503 until the worker's first heartbeat, then 200
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
| `VITE_API_URL` | `https://api.arkvane.in` |
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
`https://api.arkvane.in/ready` every five minutes. **`/ready`, not `/health`** — `/health`
only says the api process is up, while `/ready` fails when the database is unreachable or the
worker's heartbeat is stale. The failure worth being woken for is the silent one: a worker that
died at 2am and sent nothing all day.

---

## 6. Deliverability: SPF, DKIM, DMARC

This section closes issue **D5** (the runbook ADR-006 promised) and finishes **C6**.

**Most of it is already done on `arkvane.in`, and done correctly.** Checked against public DNS
(§2.2): exactly one SPF record including Google, a 2048-bit DKIM key published at
`google._domainkey`, and DMARC at `p=none` with reporting to `rahul@arkvane.in`. That is the
right opening posture, so what follows is now **verification**, not setup. §6.1 is kept as the
reference for rebuilding these or setting them up on a second domain.

| | Published | Still to do |
|---|---|---|
| SPF | ✅ one record, `include:_spf.google.com`, `~all` | tighten to `-all` once mail lands reliably |
| DKIM | ✅ `google._domainkey`, 2048-bit | confirm **Start authentication** was pressed in the Workspace admin console — DNS cannot show this |
| DMARC | ✅ `p=none` with `rua` | move to `p=quarantine` after a clean week of reports |

**What DNS cannot tell you**, and what therefore still needs checking by hand: whether Google is
actually *signing* with that DKIM key. The record existing is necessary but not sufficient —
somebody has to press **Start authentication** in the admin console. §6.2 is how you find out.

**One thing to confirm about the sending address.** SPF, DKIM and DMARC protect the domain in the
**From** header. These records are on `arkvane.in`, so `SMTP_EMAIL` must be an `@arkvane.in`
address. If it is still the `@athenaedu.in` mailbox used during testing, mail will go out
unauthenticated against a domain with no matching records, which is worse than no setup at all.

Two prerequisites from ADR-006, worth confirming before anything else:

- **IMAP is enabled** in Workspace (Admin → Apps → Google Workspace → Gmail → IMAP access). It is
  off by default on some plans, and the reply engine sees nothing without it.
- **`SMTP_PASSWORD` is an App Password**, which requires 2-Step Verification on the account. A
  normal account password is rejected.

### 6.1 The three records, at GoDaddy

GoDaddy: **My Products → Domain → DNS → Manage Zones → Add New Record**. Replace
`arkvane.in` with the sending domain. Verify each with `dig +short TXT <name>` from your
own machine before moving to the next.

A GoDaddy quirk worth knowing: its panel shows the **Name** relative to the domain, so the name
for a record at the apex is `@`, and for DKIM it is `google._domainkey` — not the full hostname.
Pasting the full name produces `google._domainkey.arkvane.in.arkvane.in`, which
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
Type: TXT   Name: _dmarc   Value: v=DMARC1; p=none; rua=mailto:rahul@arkvane.in; fo=1; adkim=r; aspf=r   TTL: 1 hour
```

Make sure that `rua` mailbox exists, or the reports go nowhere. After a week of clean reports move
to `p=quarantine`; only then consider `p=reject`.

### 6.2 Verify before the first send

- Send one message to `mail-tester.com` (or to a Gmail address and use **Show original**) and
  confirm **SPF: PASS, DKIM: PASS, DMARC: PASS** — all three, on the message *as received*.
- Confirm there is exactly one SPF record: `dig +short TXT arkvane.in | grep spf1`.
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
database dies with the box.** No point-in-time recovery, no vendor holding a copy. The Elastic IP
and the EBS volume survive a stop/start, but not a terminated instance, a deleted volume or a
closed account — and an account on a free plan with a $5 budget alarm is exactly the kind that
gets suspended over a billing surprise.

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
| Certificate never issued | No A record yet, the record is Cloudflare-proxied (must be grey cloud), or port 80 is closed in `outreach-automation-sg`. |
| Nothing is being sent | `check_env` first — `SEND_MODE` is `disabled` by default and that is deliberate. Then the mailbox: a paused mailbox stops sending and raises an alert. Port 587 is open on this instance (§1.3), so the network is not the cause. |
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
