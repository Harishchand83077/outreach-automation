# Deploying, in the fewest commands that are still safe

`DEPLOY.md` is the full runbook — the VM, DNS, TLS, the first-time setup. This file is the thing
you actually need at 11pm: what to type, in order, and how to know it worked.

There are **two** deploy targets and they are independent:

| Target | What it serves | How it deploys |
|---|---|---|
| **Cloudflare** | the dashboard (`frontend/`) | **automatic** on every merge to `main` — nothing to run |
| **EC2** | the API and the worker | **manual**, the commands below |

So a change that is only to `frontend/` needs no deploy at all. A change under `app/` does.

---

## The whole thing, in three commands

Open a shell on the instance (see *Getting a shell* below), then:

```bash
cd /opt/outreach
sudo ./deploy/backup.sh          # ~10s. Skip only if you enjoy regret.
sudo ./deploy/deploy.sh          # ~2-3 min. Pulls, migrates, restarts, verifies, rolls back on failure.
curl -s https://api.arkvane.in/ready
```

The last line is the only check that matters:

```json
{"ready":true,"checks":{"database":"ok","worker":"ok","worker_seen_seconds_ago":2}}
```

`"ready":true` means the API is up **and** the worker has written a heartbeat within the last
two minutes. If it says `false` or the request fails, go to *When it goes wrong*.

### Why the backup first

`deploy.sh` runs migrations before starting the new containers. A migration is the only step in
this process that can lose data, and the nightly dump may be up to 24 hours old. The backup
takes ten seconds and uploads off-box to S3. Run it.

---

## Getting a shell

**Session Manager, not SSH.** Port 22 can stay closed; SSH broke four times during this project
(three residential IP rotations and once because the machine switched to IPv6 while the security
group held an IPv4 `/32`).

1. <https://ap-southeast-2.console.aws.amazon.com/systems-manager/session-manager/start-session>
2. Select `i-02ca5f650887fc66c` (outreach-automation) → **Start session**.

The console session expires after a while; signing in again is all it needs.

---

## Checking what is actually running

```bash
# Which commit is deployed, and are the four containers up?
sudo git -C /opt/outreach log --oneline -1
sudo docker ps --format '{{.Names}} {{.Image}} {{.Status}}'

# Which migration is applied? Should be the newest file in app/db/migrations/versions/.
sudo docker compose --env-file .env -f deploy/compose.prod.yml exec -T api alembic current

# Is the send mode what you think it is? Prints names and OK/MISSING, never values.
sudo docker compose --env-file .env -f deploy/compose.prod.yml exec -T api \
  python -m app.scripts.check_env
```

A quick way to tell whether a *new API route* reached the server, without logging in:

```bash
# 401 means the route exists and wants auth. 404 means it is not deployed yet.
curl -s -o /dev/null -w '%{http_code}\n' https://api.arkvane.in/api/v1/admin/dead-jobs
```

---

## When it goes wrong

```bash
# Did the deploy fail? It writes the whole transcript here when you redirect it.
sudo ./deploy/deploy.sh > /tmp/d.log 2>&1; echo "exit=$?"; tail -40 /tmp/d.log

# Worker unhappy? Errors only, most recent last.
sudo docker compose --env-file .env -f deploy/compose.prod.yml logs --tail 200 worker \
  | grep -iE 'error|traceback|exception'

# Is every task cycling? `consecutive_failures` should be 0 for all five.
sudo docker compose --env-file .env -f deploy/compose.prod.yml logs --tail 50 worker \
  | grep 'worker heartbeat' | tail -1
```

**`deploy.sh` rolls itself back** if the API does not answer `/health`, so a failed deploy leaves
the previous image running. It also cannot change `SEND_MODE` and cannot run `down -v` — CI
asserts both of those by grep, so neither can be added by accident.

### Jobs that died

A job goes `dead` after five attempts. The cause is usually a defect, so the useful action after
shipping the fix is "run it again":

```bash
# From the dashboard: Settings shows the count. These are the API routes behind it.
curl -H "Authorization: Bearer $TOKEN" https://api.arkvane.in/api/v1/admin/dead-jobs
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.arkvane.in/api/v1/admin/jobs/<id>/requeue
```

### Restoring a backup

`DEPLOY.md` § *Restoring*. Do the drill once while nothing is wrong — a backup nobody has ever
restored is a hypothesis, not a backup. (This one has been drilled: row counts matched.)

---

## Deploying the dashboard

Nothing to do. Cloudflare builds `frontend/` on every merge to `main`, usually within a minute
or two. To confirm a build actually shipped rather than assuming it:

```bash
# The asset hash changes whenever the bundle does.
curl -s https://outreach-automation.sanketyadavdohar123.workers.dev/ | grep -o 'index-[A-Za-z0-9_-]*\.js'
```

If the dashboard shows `<!DOCTYPE` errors, the build variables are missing: `VITE_API_URL`,
`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` must all be set as **build** variables in the
Cloudflare project, and the project redeployed after setting them. Without `VITE_API_URL` the
dashboard asks its own origin for `/api/v1`, gets `index.html` with a 200, and fails to parse it.

---

## The order, when both change

A frontend that expects an endpoint the backend does not have yet is the one unpleasant window
in this process. It is brief and it is survivable — the dashboard is written so a missing
endpoint degrades rather than errors — but if you care:

1. merge,
2. deploy the **backend** (the commands at the top),
3. Cloudflare will already have published the frontend; reload it.

Doing it the other way round is fine too, and is what happens by default. It just means a few
minutes where a new panel does not draw.
