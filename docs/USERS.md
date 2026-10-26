# Giving somebody access

Two systems have to agree before anybody sees anything, and confusing them is the cause of almost
every "I signed in and it says I have no organisation".

| | Who it is | What it answers | Where you change it |
|---|---|---|---|
| **Supabase Auth** | the identity | may this person *sign in at all*? | the Supabase dashboard |
| **`app.members`** | the membership | which **workspace** are they in, and with what role? | this database |

A valid Supabase token with no `members` row gets **403**, not an empty dashboard — `app/api/deps.py`
has never taken an org from a request, and it is not about to start taking one from a stranger.
Access is a row.

---

## Which of the two things do you want?

**A. Their own workspace.** They sign up with their own email, see only their own campaigns, leads
and drafts, connect their own Gmail, and send as themselves. They cannot see yours and you cannot
see theirs. This is the ordinary case, and it is the one that needs no SQL.

**B. A teammate inside *your* workspace.** They see your campaigns and your review queue, and the
point is usually to share the reviewing. This is a row you insert.

They are not a progression: a person is either in your org or in their own. Moving them later means
deleting one `members` row and writing another, and their own campaigns do not come with them.

---

## A. Their own workspace

### 1. Decide who may sign up at all

`SIGNUP_MODE` in `/opt/outreach/.env` on the server:

| Value | Who gets a workspace |
|---|---|
| `closed` | **nobody** — a valid token with no membership is refused. The default. |
| `allowlist` | anyone whose email domain is in `SIGNUP_ALLOWED_DOMAINS` |
| `open` | anyone who can sign in through Supabase |

```bash
sudo sh -c 'printf "\nSIGNUP_MODE=open\n" >> /opt/outreach/.env'
cd /opt/outreach && sudo ./deploy/deploy.sh
sudo docker compose --env-file .env -f deploy/compose.prod.yml exec api \
    python -m app.scripts.check_env | grep SIGNUP
```

The last line must print `SIGNUP_MODE  = open`. `check_env` reports it in the **Safety** block
beside `SEND_MODE`, deliberately: it always has a value, and `closed` looks identical to
`unconfigured` from the outside.

`MAX_ORGS` (default **25**) is a hard ceiling on top of whichever mode is set. It exists because
this runs on one small instance with one shared free-tier LLM quota, and the failure mode of open
sign-up is not a breach but a hundred strangers quietly exhausting it until the people who actually
use the tool get nothing.

### 2. `open` is only as open as Supabase is

**This is the part that surprises people.** `SIGNUP_MODE=open` does not mean "anyone on the
internet": it means "anyone *Supabase lets sign in*". So Supabase is the gate, and you have two
ways to hold it:

- **Let anyone in.** Supabase dashboard → **Authentication → Sign In / Providers → Email** →
  leave *Allow new users to sign up* **on**. Anybody with an email address can then create a
  workspace, up to `MAX_ORGS`.
- **Invite only, which is almost certainly what you want.** Turn *Allow new users to sign up*
  **off**. Now only accounts **you** create can sign in, and `SIGNUP_MODE=open` simply means
  "everybody I let in gets their own workspace". You add a person in
  **Authentication → Users → Add user**, either creating them with a password or sending an
  invitation. Nobody else can get in, and you never touch this database.

That second combination is the useful one: you keep the guest list in one place, in a UI, and the
workspace provisioning happens by itself.

### 3. They sign in

1. They open the dashboard and enter their email.
2. They click the magic link.
3. Their **first authenticated request** creates the org and makes them its owner —
   `app/services/signup.py`, ADR-017. There is no `POST /signup` to remember to call: a separate
   endpoint would be a second door into the same room, and anything that forgot to knock on it
   would see a 403 it could not explain.

The workspace is named from the local part of their address, so the first screen says *"Priya"*
rather than *"Org 7f3a"*. They can rename it.

### 4. They connect their own Gmail

Campaign screen → **Connect Gmail**. Until they do, their campaign shows a `mailbox_unavailable`
blocker and sends nothing. It does **not** fall back to the deployment's account: that was
ADR-020's whole subject, and `orgs.uses_environment_mailbox` is granted to the oldest org only.

---

## B. A teammate inside your workspace

No invite UI exists, so this is one row. The person must **not** have signed in yet — if they have,
they already have their own org and the insert fails on `members.user_id`'s unique constraint.

### 1. Create them in Supabase

Supabase dashboard → **Authentication → Users → Add user**. Then open their row and copy the
**UID** — that is the `sub` claim their token will carry, and it is what `members.user_id` holds.

### 2. Insert the membership

```bash
cd /opt/outreach
sudo docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres \
    psql -U outreach -d outreach
```

The `sudo` matters: `docker compose` reads `.env`, which is root-only. Without it you get
`open /opt/outreach/.env: permission denied`, which reads like a Docker problem and is a
permissions one.

```sql
SELECT id, name FROM app.orgs ORDER BY created_at;   -- yours is the first row

INSERT INTO app.members (id, org_id, user_id, email, role)
VALUES (gen_random_uuid(), '<your org id>', '<their Supabase UID>', 'them@example.com', 'member');
```

### 3. Roles

Checked in `app/api/deps.py`; strongest first.

| Role | May |
|---|---|
| `owner` | everything, **including setting the send mode** (ADR-010) |
| `admin` | connect and disconnect mailboxes, edit sending rules, remove a suppression, read `/admin/*` |
| `member` | read the dashboard and approve or reject drafts — and nothing that changes the rules underneath them |

`member` is the right default for somebody who is there to review. The gate is on the rules, not on
the work: a test asserts a member can still review.

Removing a suppression is `admin` on purpose — it undoes somebody's opt-out, which is the one
action in the dashboard that can lead to mailing a person who asked not to be. *Adding* one is
protective and stays open to anyone.

### 4. Removing them

```sql
DELETE FROM app.members WHERE user_id = '<their Supabase UID>';
```

Then delete or ban the user in Supabase, or they can still sign in — and with `SIGNUP_MODE=open`
their next request would provision them a **brand-new empty workspace**, which looks exactly like
the data having vanished. Remove the identity and the membership together, in that order.

---

## When it does not work

| What they see | What it is |
|---|---|
| *"not a member of any organisation, and sign-up is closed"* | `SIGNUP_MODE=closed`. §A.1. |
| *"Sign-up is restricted to approved email domains"* | `SIGNUP_MODE=allowlist` and their domain is not in `SIGNUP_ALLOWED_DOMAINS`. |
| *"This deployment has reached its workspace limit"* | `MAX_ORGS`. Raise it in `.env` and redeploy. |
| They sign in and see an **empty** workspace that should have your campaigns | They signed in before the `members` row was inserted, so they were provisioned their own org. Delete both rows and start §B again. |
| The magic link never arrives | Supabase, not this system. Check **Authentication → Logs** there. |
| A browser CORS error on the dashboard | `CORS_ALLOWED_ORIGINS` must be the exact origin, scheme included, no trailing slash. |

Useful:

```sql
SELECT m.email, m.role, o.name AS workspace, m.created_at
FROM app.members m JOIN app.orgs o ON o.id = m.org_id
ORDER BY m.created_at;
```
