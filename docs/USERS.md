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

Settings -> **Team** -> *Invite by email*. That is the whole thing. There is no SQL, and **the
order no longer matters** -- which it used to, silently and badly (ADR-022).

### What an invitation does

It holds a seat. When that address signs in for the first time, `provision()` finds the pending
invitation and joins them to **your** org with **the role you chose**, instead of creating a
workspace of their own.

An invitation also **overrides `SIGNUP_MODE=closed`**. An admin naming an address deliberately is
a stronger statement than a global policy, and a deployment kept closed so strangers cannot wander
in still has to be able to add its own team.

**It is not a token and it is not secret.** A pending invitation grants nothing by itself: it is
consulted only when somebody authenticates *as that address* through Supabase, so the security
boundary is still the identity provider. Matching on the address rather than on a user id is
unavoidable -- a Supabase `sub` does not exist until the first sign-in -- and it is the same
assumption every invitation email in existence makes.

### The one case it cannot fix

**If they have already signed in, they already have a workspace of their own**, and the invitation
is refused with a sentence saying so.

Quietly giving them a second membership would be worse than refusing. `uq_members_org_user` is
scoped per org, so nothing in the database stops it, and `deps.current_user` resolves a person
holding two memberships by taking whichever row comes back first -- so they would see one
workspace on Monday and the other on Tuesday, with no pattern to it and nothing on screen
admitting that two exist.

The way out is a decision, not a button: they keep their own workspace, or an admin deletes their
membership and their org and then invites them fresh. The second throws away everything they had
created, so nothing does it automatically.

### Roles

Checked in `app/api/deps.py`; strongest first.

| Role | May |
|---|---|
| `owner` | everything, **including setting the send mode** (ADR-010) |
| `admin` | connect and disconnect mailboxes, edit sending rules, remove a suppression, invite and remove people, read `/admin/*` |
| `member` | read the dashboard and approve or reject drafts -- and nothing that changes the rules underneath them |

`member` is the right default for somebody who is there to review. The gate is on the rules, not on
the work: a test asserts a member can still review.

**You cannot invite somebody above your own role.** Otherwise the door built to let people in is a
privilege escalation: an admin invites an owner, who may then change the send mode.

Removing a suppression is `admin` on purpose -- it undoes somebody's opt-out, which is the one
action in the dashboard that can lead to mailing a person who asked not to be. *Adding* one is
protective and stays open to anyone.

### Removing somebody

Settings -> **Team** -> *Remove*. Two refusals, both about leaving the workspace reachable: you
cannot remove yourself (an admin who does is locked out of the org they were administering), and
the last `owner` cannot be removed (an org with no owner has nobody who may change its send mode).

**Their work stays.** A member is a key, not a container: the campaigns, leads and drafts belong to
the org, and deleting them because somebody left would destroy the record of what was sent to whom,
which `PRIVACY.md` is explicit must stay attributable.

Delete or ban them in Supabase too, or they can still sign in -- and with `SIGNUP_MODE=open` their
next request provisions a brand-new empty workspace, which looks exactly like the data having
vanished.

### By hand, if the dashboard is not reachable

```bash
cd /opt/outreach
sudo docker compose --env-file .env -f deploy/compose.prod.yml exec -T postgres     psql -U outreach -d outreach
```

The `sudo` matters: `docker compose` reads `.env`, which is root-only. Without it you get
`open /opt/outreach/.env: permission denied`, which reads like a Docker problem and is a
permissions one.

```sql
SELECT id, name FROM app.orgs ORDER BY created_at;   -- yours is the first row

INSERT INTO app.invitations (org_id, email, role, invited_by)
VALUES ('<your org id>', 'them@example.com', 'member', 'you@example.com');
```

**Write an invitation, not a `members` row, even here.** The invitation works whether or not they
have signed in yet; a `members` row written after they have already signed in is the
double-membership failure above, and nothing will tell you it happened.

---

## When it does not work

| What they see | What it is |
|---|---|
| *"not a member of any organisation, and sign-up is closed"* | `SIGNUP_MODE=closed`. §A.1. |
| *"Sign-up is restricted to approved email domains"* | `SIGNUP_MODE=allowlist` and their domain is not in `SIGNUP_ALLOWED_DOMAINS`. |
| *"This deployment has reached its workspace limit"* | `MAX_ORGS`. Raise it in `.env` and redeploy. |
| They sign in and see an **empty** workspace that should have your campaigns | They signed in before being invited, so they were provisioned their own org. §B, "The one case it cannot fix". |
| *"already has a workspace of their own"* when inviting | Exactly that, and refused rather than papered over. §B. |
| The magic link never arrives | Supabase, not this system. Check **Authentication → Logs** there. |
| A browser CORS error on the dashboard | `CORS_ALLOWED_ORIGINS` must be the exact origin, scheme included, no trailing slash. |

Useful:

```sql
SELECT m.email, m.role, o.name AS workspace, m.created_at
FROM app.members m JOIN app.orgs o ON o.id = m.org_id
ORDER BY m.created_at;
```
