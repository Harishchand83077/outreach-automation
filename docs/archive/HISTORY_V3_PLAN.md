# Implementation History: v3 Plan (Phases 10–17)

Extracted from `docs/PROGRESS.md`. Covers multi-tenant sign-up (PRs #96–97) through ADR-062 (Setup page). Includes all v3 plan slices S10–S17, feature work F1–F24, and ADRs 017–062.

---

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

**A mailbox that is bouncing badly now stops by itself (F3, ADR-034, slice S11.6). Phase 11 is
complete.** `mailbox_health` holds one row per mailbox per IST day, and an hourly `Scheduler` pass
rolls it up and pauses any mailbox whose bounce rate over a rolling week has crossed the limit,
with an alert naming the counts.

**The one rule everything else follows from: this pass may only restrict.** No branch resumes a
mailbox, raises a cap, extends a window or touches a send mode -- asserted over the source, with
comments stripped first so the prose explaining the rule cannot satisfy it. That asymmetry is what
makes acting without a human acceptable: **a signal that can only restrict cannot cause the outage
it would then report.** Resuming is the risky direction, so resuming stays a person's decision.
The structural test exists because the edit that breaks this is one somebody would make for a
good-sounding reason -- "un-pause it once the rate recovers" is exactly how this becomes something
that resumes sending on its own.

**Three guards that matter more than the threshold.** A minimum of 20 sends before any rate is
believed, because at five sends one bounce is 20% and alerting on noise is how an operator learns
to ignore alerts. A rolling seven days rather than a single day, because one bad afternoon is not
a reputation problem. And an already-paused mailbox is skipped, so the alert fires once rather than
every hour for something somebody is already handling. The threshold itself is 5%, the lenient end
of the usual guidance, because the action is automatic and stopping a campaign wrongly is worse
than stopping it a day later.

**A rollup rather than counters.** The table is recomputed from `messages` and `classifications`,
which are the record. A counter incremented at send time drifts -- a crash between the send and
the increment leaves it wrong for ever and nothing notices -- while a rollup is always a fresh
answer. The numbers lag one pass, which for a signal measured in days is not a cost.

**`complaint_rate` is nullable and deliberately never written.** A complaint rate needs a feed
from the receiving provider, and Postmaster Tools is a link we ask the owner to open rather than
an integration (ADR-029). A zero there would read as "no complaints" when it means "we cannot see
complaints", and the whole value of this table is that it can be trusted. The column exists so a
future feed has somewhere to go, and `source` records where a number came from. So the honest
summary of F3 is: **we can now see bounces automatically; complaints still need the owner's manual
Postmaster step**, and that is the number Gmail actually judges on.

**The alert kind carries the mailbox id**, because the cool-down dedupes by kind and the first
mailbox paused would otherwise silence every other one -- and the mailbox that never got its alert
is the one still bouncing. The same reasoning as the OAuth grant warnings, and three tests fail
without it.

**mypy earned its keep twice in this slice**: nullable `mailbox_id` and `sent_at` columns whose
queries exclude nulls but whose types cannot know it, so the narrowing is explicit and a row with
neither is skipped rather than crashing a housekeeping pass.

**The deliverability report was telling most users to fix something they cannot fix (ADR-035,
slice S11.7).** Caught by the owner pointing out -- for the second time -- that this is a
multi-user product where anyone connects their own mailbox, Gmail or their own domain.

The code was already domain-agnostic: ADR-023 removed the deployment mailbox, no sending logic
names a domain, and a grep confirms every `arkvane` string in `app/` is either the API's own
hostname or a comment about the historical incident. **The report was the problem.** It ran every
sending domain through the same DNS checklist, which is right for a domain the sender owns and
wrong for the commonest case here.

`gmail.com` answers an SPF record and a DMARC record and **no selector anyone could guess** --
Google signs with its own rotating ones. So a user who connected a Gmail address was told their
domain was **not ready**, over a DKIM record they cannot publish, do not control and do not need.
Most signups connect a Gmail address. Verified before fixing: `ready: False`, `DKIM: unknown`.

A provider-owned domain is now reported as the provider's business -- one passing
**Authentication** line, and **no DNS query at all**, because nothing in DNS could change the
answer. The detail carries the two things that genuinely are true of a shared domain, because both
are easy to assume away: you share its reputation with everybody else sending from it, and you
cannot add it to Postmaster Tools because it is not yours. Then the sentence that makes it
actionable rather than a shrug: sending from a domain you own gives you both of those.

One-click unsubscribe is still checked and can still fail -- that header is ours, not the
provider's. And a domain the sender owns still gets the full four-item checklist, asserted by its
own test: the point is to tell the two cases apart, not to skip the checks.

**The lesson is about where the single-tenant assumption hides.** Not in the schema -- ADR-004 put
`org_id` everywhere in the first migration -- but in a *report*, written as though every sender
owned their domain. The three cross-tenant queue defects earlier today were the same shape in a
different place. Tenancy is not only about which rows you can read; it is about whether the thing
you say to a user is true of *their* setup.

**And my own structural guard caught an ambiguity in my own code on the way.**
`HealthReport.paused` was a list of mailbox ids while `Mailbox.paused` is a boolean, so the guard
asserting "this module may only ever set `paused = True`" read `report.paused = decided.paused` as
an attempt to clear a pause. The fix was to remove the ambiguity rather than loosen the guard:
`paused_mailbox_ids`. A field named after the column it is not is a field that will be confused
for it.

**"Are we ready to mail strangers?" has an answer that is not somebody's memory of reading a
page (F1, slice S10.1).** `python -m app.scripts.preflight` prints a line per finding and exits
non-zero on NO-GO.

`docs/GO_LIVE.md` is the checklist a person works down; this is the half a machine can answer, in
the order things actually go wrong: every sending domain's SPF, DKIM, DMARC and one-click; every
mailbox connected and unpaused; the send mode and whether an allowlist deployment has anybody on
its list; and each campaign's booking link.

**Three rules it shares rather than restates.** The domain checks call the same `check_domain` the
admin screen uses -- a script and a screen disagreeing about whether a domain is ready would be
this project's signature defect in the one place somebody acts on the answer. Mailbox usability
asks `unavailable_reason`, which already answers that for the campaign screen and for `login_for`
and never touches the network. And the send mode comes from `effective_send_mode`, because hard
rule 1 says exactly one function resolves it. Three tests assert each of those by source.

**`UNKNOWN` is a FAIL here, and that is the one place it should be.** The admin screen reports
"we could not tell" because that is honest and useful there. A go-live gate has to answer, and
"we could not tell" is not a yes.

**A booking link that books nothing is worse than none, because it looks done.** The live
campaign's URL was `calendar.google.com/calendar/u/0/r`, which opens the **reader's own** calendar
-- a recipient clicking it sees their own week. That shipped and sat there because every validator
only asked whether the field was filled in. The gate now recognises that shape and the three
others like it, and a written ask with no link is a warning rather than a failure, since
`CampaignSettings` has allowed exactly that since Phase 2.

**And a GO still says what a machine cannot check.** DKIM passing in DNS is not DKIM signing;
only Show original on a received message proves that. A gate that said GO without saying so would
be the most expensive kind of wrong, which is confident.

**Two small things the first run taught, both already-learned lessons re-learned.** The printed
output had an em-dash and a middle dot in it, which a Windows console renders as replacement
glyphs -- the same defect as the smoke check's Unicode ellipsis in a credential prefix, and now
asserted by a test that every printable string is ASCII while docstrings are exempt. And I checked
the exit code through `| tail`, which reports *tail's* status, so a NO-GO looked like a pass. That
is the second time today that pipeline has lied to me.

**mypy caught the one wrong assumption**: `invalid_reason` is on `mail_credentials`, not on
`mailboxes`. Being made to look showed that `unavailable_reason` already existed and was the right
thing to call, so the type error prevented a second copy of a rule rather than just a crash.

**Phase 10's code half is done (F1, slices S10.2 and S10.3).**

**A booking link is now refused at save if it books nothing.** `Persona` validation rejects a URL
that opens the reader's own calendar -- the live campaign's was
`calendar.google.com/calendar/u/0/r`. The shapes live in `app/domain/booking.py`, which both the
validator and the go-live gate read, so the two cannot drift. A `cta_text` with no link is still
valid, as it has been since Phase 2.

**`python -m app.scripts.rehearsal_report --since <date>`** lists every outbound message in a
window with whether its recipient was on the allowlist, and exits non-zero if any *delivered*
message went to an address that was not. A **blocked** send to an unlisted address is the gate
working and does not fail the report -- a rehearsal in allowlist mode should be full of them.

mypy caught two wrong column assumptions in these two scripts (`SendAllowlistEntry.value`, which
is `email_norm`, and `invalid_reason` on the wrong model), which is the fourth and fifth time it
has caught that class this week.

**What is left of F1 is not code.** Connect a mailbox whose address matches the account that
authenticates, confirm `DKIM: PASS` in Show original on a received message, and replace the
booking URL. The gate reports all three and cannot do any of them.

**Job postings come in now, from public ATS feeds or a pasted URL (F5, ADR-036, slices S12.1 and
S12.2).** `job_posts`, a parser for Greenhouse, Lever and Ashby, a fetch client, and
`/api/v1/job-posts` to paste, list and archive.

**The endpoints were tested before anything was built on them**, because the v3 doc took them
from one third-party article. All three answer 200, and `docs/ATS_FEEDS.md` records what they
actually return. The difference that mattered: **Lever returns a bare JSON array where the other
two wrap theirs in `jobs`**, and its timestamps are epoch milliseconds rather than ISO 8601. A
parser written against an assumed shape would have failed for exactly one source, which reads
like a bad board slug rather than a bad assumption. Greenhouse's list response carries no
description at all, so `body_text` is nullable from the first migration instead of being
discovered to be optional later. Fixtures are real trimmed responses, so the parser is tested
against what the services send rather than what I expected.

**Nothing fetches a pasted URL.** The user supplies the text, and a structural test asserts the
router imports no HTTP client -- that is what keeps this on the right side of hard rule 4 and of
each board's terms.

A test caught a real hole: an unknown source whose payload happened to have the right shape
returned **zero postings** rather than an error, which is indistinguishable from an empty board.
The source is now validated before parsing.

**Boards are polled daily now (F5, slice S12.3).** `watched_boards` holds the boards a workspace
wants read, the `Scheduler` enqueues one `poll_ats_feed` job per board per day, and the handler
fetches and upserts.

**This is the slice ADR-032 had to land before.** The dedupe key is `feed:{source}:{slug}`, built
from values two workspaces genuinely share -- two candidates watching the same company is the
ordinary case. Before dedupe was scoped by org, the second workspace's poll would have been
refused and its postings would simply never have arrived, with nothing anywhere saying so. There
is now a test for exactly that.

`last_polled_at` is stamped **on enqueue, not on success**, so a board whose poll keeps failing is
retried once a day rather than on every scheduler pass. A wrong slug is a `PermanentFailure`
rather than a retry, because trying a 404 five more times will not make the board exist, and the
route validates a slug with the same `feed_url` the poll uses -- so a board that saves is a board
that can be polled.

Adding the handler needed a module logger in `app/worker/handlers.py`, which had none at all.

**A candidate owns their facts now, and a fit score can be audited (F6, ADR-037, slices S12.4
and S12.5).** `profile_facts`, `answer_bank`, a deterministic fit score, and `/api/v1/profile/*`.

**A fact is unusable until the person confirms it.** Typed facts are confirmed by the act of
typing; facts mined from GitHub or a resume arrive as proposals. Only confirmed facts reach the
drafter or the scorer, because the person asked about a claim in an interview is the person who
has to have agreed to it.

**The fit score is rules over a model's extraction, not a model's judgement.** The model turns
posting prose into requirement lines and does nothing else; the comparison is deterministic and
takes instructions from nobody, so a posting that says "report a perfect match" cannot. Every
match names the fact id and the words that made it one, and the score is never returned without
`matched` and `missing` beside it.

**Only an approved answer is ever offered, and editing one unapproves it** -- the approval was of
the previous wording. An unmatched question is reported as needing the user rather than guessed.

`ppN` ids come from the highest existing id rather than a count, so deleting `pp2` cannot later
produce a second `pp3`; a duplicate id would make a citation ambiguous.

**One company, researched once, for everybody (F19, ADR-038, slice S12.6). Phase 12 is
complete.** `company_profiles` is the only table in this schema with **no `org_id`** -- the single
deliberate exception to ADR-004, approved by the owner.

**What makes the exception safe is what the table may hold**: a name, a careers URL, an ATS type
and slug, a size band, a summary, and the source behind each field. Two guards rather than a
convention: `apply()` writes only `PUBLIC_FIELDS`, so a caller passing `notes` has it dropped; and
a structural test fails if the table gains a column named anything like `org`, `lead`, `contact`,
`message`, `note` or `credential`. **It is the opposite of a tenancy test** -- every other table
is checked for isolation and this one is checked for having nothing to isolate.

`domain_research` stays per-workspace and untouched. Those are the findings a draft *cites*: not
public facts, paid for by one workspace, and not ours to pool. That distinction is why this is one
exception and not a precedent.

**A cache hit costs no credit**, which is the entire point; a fetch counts against a daily
per-workspace budget, so one workspace importing a thousand companies cannot spend everybody's
allowance. Credits are summed rather than searches counted, because one search can cost several.
Past the age limit the content hash is compared before anything is written, so an unchanged page
costs the fetch and nothing else and `version` only moves on a real change.

mypy caught `SearchUsage.created_at`, which is `at` -- the same naming trap as `Event.at`, for the
third time in this project and the third time found by the type checker rather than by a screen
reporting zero for ever.

**The extension has a gateway, and it is a third door (F8, ADR-039, slices S13.1-S13.3).**
`/ext/v1` with its own scope `ext:suggest`, plus `captures` and `suggestions`.

**Each key opens exactly one door.** A Supabase JWT opens the dashboard, an `mcp:` token opens the
connector, an `ext:suggest` token opens the gateway, and none of them opens another. The one worth
stating is that an extension token **cannot call `/mcp`**: if it could, a credential sitting in a
browser on a laptop would read every campaign, lead and reply in the workspace. Five tests assert
the matrix, and the minting route refuses to issue `ext:suggest` alongside an MCP scope at all --
so there is no such thing as a token that can do both.

`ALL_SCOPES` is one tuple and the database check constraint is built from it, so the constraint,
the minting route and each door's gate cannot disagree about what a scope is.

**An unmatched label comes back as `needs_user`, never as a guess**, and no model is reachable
from this path at all. A guess dressed as an answer is worse than a blank, and guessing screening
answers is precisely what the tools we are not copying do.

**A form capture stores the labels and the URL and no page text.** Only labels are needed to match
an answer; the rest of an application page is the user's own data and somebody else's copy. A test
asserts the stored row has no text, and `PRIVACY.md` now describes exactly what the extension
sends and keeps.

**The extension itself exists now, and its containment is an absence rather than a promise
(F8, ADR-040, slice S13.4). Phase 13's F8 is complete.** `extension/`: MV3, TypeScript strict, a
side panel, four saved application forms and 38 tests.

**The manifest declares no content script.** The code that reads a page is injected by the panel
on a button press, under `activeTab` -- which Chrome grants for one tab because the user clicked
this extension's own button. So there is **no code path by which the extension could read a page
nobody asked about**, which is a checkable statement where a privacy policy is not. It also ships
with **no host permissions**: the user's own API origin is requested at the moment they save its
address, so the install prompt covers nothing and the grant names one host. A packaged wildcard
would be a permission over the entire web, accepted once for a reason nobody could evaluate.

**Four absences are asserted over the package's source** -- no form submission, no assignment
into a page's field, no file upload, and no read of a field's *contents*, with
`getAttribute("value")` and `.checked` named because both are the back door round the assignment
rule. Comments are stripped first, because the prose explaining each rule names every API it
forbids. And the strongest form of "it cannot fill the form" is that **there is no channel back
into the page**: nothing sends a message to the content script and the content script registers
no listener, so a panel that wanted to has nothing to ask.

**The guard is written twice on purpose.** `extension/tests/safety.test.ts` and
`tests/unit/test_extension_package.py` assert the same four things. The npm suite needs `npm ci`
in a second directory and is a CI job that can be skipped or reordered; the Python one runs in the
suite that gates every change to this repository. The same bargain as
`test_connect_sends_mailbox_id.py`. It was run against a planted `form.submit()` to watch it fail.

**A page that cannot be fully read says so, and that is where the design decisions are.** Some
boards render the form inside a **closed** shadow root, where `host.shadowRoot` is null by design.
An open root is walked; a custom element with no open root is counted as a field we could not
read. That **over-reports** -- not every `<x-y>` is a field -- and over-reporting leaves the panel
saying the list may be incomplete, which is the harmless direction. Reporting a page we cannot see
into as a complete form is the other one, and it was the first version's behaviour: the scan came
back with zero questions, which is indistinguishable from a page that has none.

**Two reading rules that are wrong in the obvious version.** A radio or checkbox takes its
**fieldset's legend**, not its wrapping label -- Lever wraps each *option* in a label, so the
obvious rule asks the answer bank about "A referral" once per option instead of asking the actual
question once. And a control's `name` is **never** used: nobody wrote an answer to
`job_application[answers_attributes][3][text_value]`. Nor is nearby text, because a `<div>` above
an input is a question often enough to be tempting and wrong often enough to produce a
confidently misapplied answer. No label of any kind means no question, counted and reported.

**`form.ts` is separate from `collect.ts` for a mechanical reason that is easy to lose.**
`chrome.scripting.executeScript({ files })` injects a **classic** script, so one `export`
statement in that bundle is a syntax error at the moment of injection -- on a page the user is
part-way through filling in. Keeping every export out of the entry means the built content script
contains no module syntax at all, and `tests/build.test.ts` asserts that against the real build,
along with the absence of a shared chunk. Which is why **CI builds before it tests**: with no
`dist/` those three tests skip, so the order of the two steps is the assertion.

**What the fixtures are, said plainly because it changes what the tests prove.**
`docs/ATS_FEEDS.md` recorded the three *feed* shapes as live-tested -- those endpoints were called.
These four forms are not that: they are hand-written to each board's published markup conventions,
and `tests/fixtures/README.md` says so rather than implying more. So the tests prove the scanner's
rules, not any live board's current markup. What makes that acceptable is the **`generic` mapper**,
which scopes to the page's largest form -- so the per-board mappers are an optimisation over a
fallback that works, and a markup change degrades into "some fields could not be read" rather than
into wrong answers.

**Playwright was considered and not taken.** It is the test that would prove the most, and it costs
a browser download in CI plus a network request to somebody else's live application form on every
run. jsdom against saved structures buys most of it for none of that, and `jsdom` and `vitest` were
already dependencies of the dashboard -- so this added no new tooling and nothing paid.

**A self-inflicted one worth recording.** `src/config.ts` would not compile, and the three errors
pointed at an expression three lines below the real cause: a doc comment containing the literal
host pattern `https://` + `*/*`, whose `*/` closed the block comment early. A comment that is about
a glob needs to not contain one.

**A workspace can look up hiring contacts on its own provider key, and nothing else can (F9,
ADR-041, slice S13.5).** `provider_credentials`, `contact_candidates`, adapters for Apollo, Hunter
and Snov plus a `mock`, and `/api/v1/provider-keys` and `/api/v1/contact-candidates`.

**This is the first feature in this system that spends the user's money**, which changes what the
design has to guarantee. Not only "is it correct?" but "can it bill somebody for work they did not
ask for?" So the containment is that **nothing but a person can start a lookup**: no job kind, no
`Scheduler` pass, no worker module that imports it, and `tests/unit/test_contacts_button_press_only.py`
asserts all three over the source tree -- including the *positive* assertion that the only caller of
`lookup.run` is the contacts router, so a caller added later fails rather than being quietly
allowed. And no `GET` on that router talks to a provider: `GET /mailboxes` is non-networked because
a refresh would mint OAuth tokens, and here a refresh would spend a credit.

**There is no shared key**, which is ADR-023's argument reused: as long as one identity lives in
the environment, every question about who may use it is a rule somebody has to maintain. A
workspace with no key gets a sentence saying to add its own and that there is no shared one by
design.

**Hard rule 8 is in the schema rather than in a code path.** A partial unique index on
`(org_id, provider) WHERE revoked_at IS NULL` makes holding several live keys for one provider
inexpressible, so "rotate five keys to dodge a free tier" is not a loop somebody writes later.
Saving a key revokes the old row instead of deleting it, so the change stays auditable.

**The budget and the audit trail are the same fact, and that is the decision worth keeping.** 25
lookups per workspace per rolling 24 hours, counted from `events` rows of type `contact.lookup`.
`events` refuses UPDATE and DELETE by trigger, so the count cannot drift -- where a counter column
incremented at call time can, which is why the mailbox-health rollup already chose recomputation
over a counter. It also makes this **the one audit write in the system allowed to fail its
request**: everywhere else the work is the point and the record of it is not, and here the record
*is* the accounting. A rolling window rather than a calendar day, because a midnight reset is a
free second allowance for anybody who waits for it.

**A proposal is not a lead**, so the send loop cannot see one. Importing is a separate explicit
action -- and it goes through **the ordinary importer, as a one-row CSV**. That looks like
indirection for its own sake and it is the most defensible thing in the slice: `importer.commit` is
where the suppression check, the MX check, the already-in-this-campaign check and the batch id that
makes an import undoable all live. A second `Lead(...)` insert would be a second place each of
those could be forgotten, and **the one that would get forgotten is the suppression check** -- a
provider will cheerfully propose somebody who asked us never to write again, because it has no idea
they did. `test_a_suppressed_address_is_refused` is the most important test in the file.

**Two shared rules were extracted rather than copied**, both because a second caller appeared:
`importer.first_job.queue_for` (which job a new lead gets, and how a large import is paced against
the model chain's daily limit) and `importer.one_row_csv`. The import route and the contact import
now call the same two functions. Copying either would have been the signature defect in a place
where one copy would have been corrected and the other not.

**The importer needs a company name and a provider does not give one.** The shared public-company
cache (F19) usually has it, since this is a company somebody has already looked at; when it does
not, the fallback is **the domain itself**, which is a true fact about the company. Deriving a
trading name from it (`acme.test` -> "Acme") would be inventing one, and hard rule 6 applies even
to a field this minor.

**`email_status` is a column rather than a note inside a confidence score.** Several providers
return `first.last@domain` built from a naming convention and present it beside a verified address.
Mailing a guess is how a bounce rate rises, which now pauses a mailbox by itself (ADR-034), so the
distinction has to reach the review screen. Apollo's `email_not_unlocked@domain.com` placeholder is
stored as **no address at all**, because a placeholder in an email column is an address something
eventually tries to mail; the proposal keeps its name and title, and the import route refuses it
with a sentence rather than a validation error.

**A response we cannot read raises rather than returning nobody.** `ProviderUnreadable`, naming the
provider and the missing field. The job-post intake taught this expensively -- an unknown source
with a plausible payload returned **zero postings**, indistinguishable from an empty board. Here
the equivalent reads as "nobody works at this company", and somebody would believe it.

**What is verified and what is assumed is a table in `docs/CONTACT_PROVIDERS.md`**, in the spirit
of `MCP_PLAN.md`. Unlike the ATS feeds, which were live-tested and are public, **none of these
three can be called without a paid account** and hard rule 5 forbids buying one -- so Hunter's
endpoint is documented and confirmed, Apollo's path is **disputed** between
`/mixed_people/search` and `/mixed_people/api_search` with its own reference not reached, and Snov
documents both a synchronous domain search and an asynchronous start-and-poll flow with sources
disagreeing about which is current. Each provider's response is read in exactly one function, so a
wrong assumption is one function and one test. `mock` is a first-class provider in the check
constraint for the same reason: the owner holds no key, and without it the feature's first run
would be against somebody's paid account.

**The key travels in a header.** Hunter's documentation shows `api_key` as a query parameter; a
credential in a URL is a credential in an access log, a proxy log and a browser history. A test
asserts the key does not appear in the request URL at all.

**`SettingsDep` exists because of this slice, and it closes a real gap.** Sealing a provider key
needs `CREDENTIAL_KEY`, and a router until now reached for the module-level `config` singleton --
so an app constructed with its own `Settings`, which is every test app, was **partially ignoring
them**. The signature shape once more: a correct producer (`create_app(settings)`) and consumers
reading a different object. New routers take `SettingsDep`; the older ones still import `config`,
and that is recorded here rather than quietly left.

**And it surfaced a hard-rule-2 near miss of exactly the kind these notes already describe.** The
global `config` is built at **import time** and does read `.env` -- which the session-scoped
`hermetic_settings` fixture cannot reach, because the import has already happened. A failing
assertion in this session printed a real provider key into the terminal: the incident that fixture
was written for, in the one place it does not cover. The slice's own tests now build their app with
an explicit test key and never touch the singleton, and the gap is written down.

**The referral finder reads a file and never a profile (F10, ADR-042, slice S13.6). Phase 13 is
three slices from done.** `connections`, a parser for the LinkedIn export, a company-name
matcher, a rule-built note, and `/api/v1/connections` and `/api/v1/referrals`.

**Nothing in this package can fetch anything** -- not "does not", cannot. No module imports
`httpx`, `requests`, `urllib`, `aiohttp` or `socket`, and no LinkedIn hostname appears in any
form; `tests/unit/test_referrals_never_fetch.py` asserts both with `ast` over the package and its
router. Hard rule 4 has forbidden scraping LinkedIn since the first commit and ADR-038 restated
that its data enters only through a file the user uploads; this is that rule made structural.
The `profile_url` in a response came out of the user's own file and is shown for them to open.

**The export's `Email Address` column is dropped before anything is written, and that is the
decision the feature turns on.** A connections export is a few thousand people who agreed to
connect with *the user* and have never heard of us. Keeping their addresses would make this a
contact database with a different name -- and it is the one dataset in this system that somebody
who came after us could pool or sell. Three independent guards: the parser drops the column,
`connections` has no column to put it in, and a test uploads a file containing an address and
checks the stored row for it.

**The note is built by rules, not by a model**, and a test asserts no model is reachable from the
package at all. Every substitution is a value from a row: the connection's own name, their own
company and title as *they* wrote them, the posting's title and link, and one **confirmed**
`profile_facts` entry when there is one. Hard rule 6 forbids inventing facts about the sender;
here the reader is a friend who would notice, so the cost of a fabricated line is not a weak
email but the user telling somebody something untrue about themselves.

**The export begins with a preamble, and the header row is found rather than assumed.** LinkedIn
writes a `Notes:` block and a blank line before the headers, so a parser taking line 1 as the
header reads the whole file as one unnamed column and reports **zero connections** --
indistinguishable from an empty network, and the same silent wrong answer as the ATS feed that
returned no postings. A file with no recognisable header is an error with a sentence, **never**
an empty result: "we read your file and you know nobody" is the one answer this must not give to
somebody who uploaded an invoice by mistake.

**Company names are normalised, and the normalisation refuses to empty a name.** The export says
whatever the connection typed on their own profile, so "Acme, Inc.", "Acme Pvt Ltd" and "ACME"
have to be one key. Suffixes are stripped repeatedly -- but **stripping stops at the last word**,
because "Technology Group Ltd" is somebody's actual company and reducing it to nothing would
match it against every other all-suffix name. That is the worst kind of match available here:
confident, wrong, and about a person the user is one click from messaging. Each match also carries
its reason in words -- exact, or containment in either direction -- because the user is the one
deciding whether a subsidiary counts, and a bare list gives them nothing to judge with.

**A second export updates rather than inserts**, since a re-export is mostly the same people and
inserting would show each of them twice in a shortlist, which reads as broken rather than long.
The newer row wins on title and company: a note quoting a job somebody left two years ago is
worse than one with no title at all.

**`GET /connections` returns a count, not a list**, and `DELETE /connections` is a real delete.
A route paging through thousands of named people would be the contact database this is carefully
not; and unlike a suppression, forgetting a connection cannot cause anybody to be contacted, so
there is nothing worth a tombstone.

**One of my own tests broke its own rule.** The guard asserting no response mentions an address
failed on the router's **note saying that addresses are discarded** -- prose about a rule
satisfying the guard for it. It now ignores string constants, which is the same lesson as
stripping comments before a structural assertion, learned again in the same afternoon.

**A tailored resume is an ordered selection of confirmed facts, and nothing else can get a line
into it (F20, ADR-043, slice S13.7). Phase 13 is complete.** `resume_versions`, `resume_renders`,
`applications`, a validator, a hand-written PDF writer, two job kinds and
`/api/v1/resume-versions`, `/api/v1/applications` and `POST /job-posts/{id}/tailor`.

**The model proposes edit operations, not prose.** `include` a fact in a section, optionally
reworded, or `drop` it with a reason; the order of the `include` ops *is* the order of the
document. There is no operation that writes a heading and no field anywhere for a header --
`ProposedEdits` has `kind`, `fact_id`, `section`, `text` and `reason`, and the sections are a
fixed tuple. The same device as `propose_reply` having no `subject` (ADR-030): the mistake is not
expressible rather than caught. One line of unanchored prose is all an invention needs, and a
"summary" field is exactly where somebody would put "8 years of experience".

**A rewording may not assert anything its fact does not**, and the two checks are the slice.
Numbers are compared after stripping thousands separators and a trailing `%`, so "2,000" against
"2000" is one quantity and "42%" out of nowhere is refused. **Named things** -- tokens that are
capitalised, all-capitals, or carry an internal digit or `+`, `#` or `.` -- must appear in the
fact: `AWS`, `Python`, `C++`, `k8s`, `Node.js`, `Acme`. Between them those are the three
inventions that matter on a resume: a skill, a product and an employer.

**Position in the sentence is deliberately not used, and that was the one real design call.**
Exempting the first token is the obvious way to let a rewording start with a capital letter, and
it leaves unchecked the one bullet somebody would actually write -- the one opening on a
technology they want to be seen to have. So every capitalised token is checked, which means a
rewording must begin with a word the fact already uses. A real constraint, in the recoverable
direction: a refused rewording costs one edit, or the fact goes in verbatim, while an invented
skill costs the user their credibility in a room we are not in.

**What this cannot catch is written down rather than discovered later:** a lower-case
exaggeration, "led a team" from a fact about writing code. No deterministic check could. That is
why a version is shown to the person before anything is attached, and why `created_by` records
whether the model or the person produced it -- so the dataset (ADR-026) never counts a machine
selection as somebody's own choice.

**Validated twice, and the second time is the point.** `validate_ops` checks the plan;
`check_rendered` checks the finished document line by line -- every line must be the person's own
header, a heading from the fixed list, or exactly the bullet of an included op. `create_version`
runs it on the text it is about to store **whatever produced that text**. It looks redundant,
because `render` builds its input, and it is the check that holds the first time somebody adds a
second producer. That is this project's dominant defect for the ninth time, pre-empted rather
than repeated.

**Only confirmed facts are shown to the model at all**, so the rule is true one step before the
validator: a model cannot choose a fact it was never offered. The validator still checks, because
a rule enforced in one place is a rule with one bug -- and it refuses by name, so "pp3 is still a
proposal" is actionable where "no such fact" sends somebody hunting a typo that is not there.

**One retry with the refusals fed back, then a recorded refusal, and the job succeeds.** The
commonest failure is one invented number in one bullet, and a model told which bullet and which
number returns the fact unchanged. A second refusal writes a `resume.tailor_refused` event with
the reasons: nothing went wrong with the work, the validator did its job, and a dead job is where
somebody looks for a fault.

**`resume_versions` is immutable, by a trigger.** An `applications` row points at the exact
document that was sent, so "what did they actually read?" has no answer if the row can be edited
afterwards. A change makes a new row. The escape hatch is `app.allow_resume_version_delete`,
exactly as `app.events` has one, so an accidental `DELETE FROM` fails loudly while a lawful
erasure stays possible and obvious in the code that performs it. The test for this is a real
`UPDATE` against real Postgres, because a test that only checked there is no edit route proves
nothing about the thing somebody will actually reach for.

**The PDF is its own table, and the immutability is the reason before the size is.** A rendering
happens after the version exists, so there is nowhere on an unupdatable row to put it; and
`bytea` on the version would travel with every row of the versions list, which is ADR-016's
argument for keeping an attachment off `campaigns`. A re-render replaces, and a failed render
leaves the previous bytes alone -- a document somebody is about to attach is worse missing than
stale.

**The PDF writer is 150 lines of ours and adds no dependency.** `reportlab` and `weasyprint` are
large trees for laying out images, tables and flowing HTML, none of which a plain-text resume
has; what this needs is short lines in Helvetica, one of the fourteen fonts a reader is required
to provide, so nothing is embedded and nothing is licensed. **The caps are the memory limit** the
implementation plan asked for on a 2 GB host (§4 risk 6): the text and the line count are checked
before anything is allocated, and the renderer has its own job kind with a concurrency of one.

**And `(`, `)` and `\` are escaped, which is a defect rather than a nicety.** They are structural
in a PDF string literal, and a resume says "(remote)" and "C:\\" often enough that an unescaped
writer produces a file that opens as garbage -- looking like our bug and being a quoting one.
There is a test for each, plus one that walks the cross-reference table and checks every offset
points at the object it claims, because a readable PDF with a wrong xref is the failure that
opens in one viewer and not another.

**Only a person marks an application applied.** The routes are on the dashboard door, and ADR-039
refuses an `ext:suggest` token at every door but `/ext/v1` -- asserted here specifically, not
only in general, with a minted extension token getting 401 on the tracker and on the versions
list. Nothing on `/ext/v1` names these tables, checked structurally over the gateway *and* the
browser package. The reason is not tidiness: an extension that could mark an application sent
would have the tracker claim the user did something they did not do, which destroys the only
thing a tracker is for. And `applied_at` cannot disagree with the status, by a check constraint
rather than by the route remembering both -- the same device as `ck_lead_outcomes_censoring`. An
applied row **cannot go back to `planned`**, because the one thing a tracker must not do is
forget that somebody sent a document to a company.

**Two of my own tests were bad proxies and were caught before being kept.** One asserted the page
count with `b"/Count 4" in out or b"/Count 5" in out or b"/Count 3" in out`, which would pass
against a writer that put everything on one page -- the same shape as the `tools/list` assertion
that compared a list with itself. It derives the count from the page geometry now. The other
checked a transliterated rupee sign with two alternatives joined by `or`; it asserts the exact
rendered line.

**The validator was run against itself broken**, to see the tests discriminate: removing the
named-things check leaves the four invention tests failing with the right words -- an invented
`Kubernetes` sailing through as a clean report -- and nothing else moves.

77 new tests (19 validation, 10 structural, 12 PDF, 9 tailoring, 27 integration), ruff and mypy
strict clean, and the migration applies and reverses cleanly against the development database.

**A campaign can send from several mailboxes, and a lead keeps the one that first wrote to it
(F7, ADR-044, slice S14.1).** `campaign_mailboxes`, `leads.mailbox_id`, weighted selection, and
`GET/PUT/DELETE /campaigns/{id}/mailboxes`.

**The sticky sender is the half that goes wrong in every tool that does this.** Rotating addresses
naively means a recipient gets the first email from one person and the follow-up from another,
which threads separately in their client whatever `References` says and reads as two strangers
writing about one thing. So `leads.mailbox_id` is written **by `claim_send`, under the lead's row
lock, and nowhere else** -- the only point where the assignment and the outbox row that justifies
it land in one transaction, so two mailboxes that both believed an unassigned lead was theirs are
serialised and the second one's `why_not_sendable` refuses it by name. Deciding it where the lead
is *selected* would be the lazy-`Candidate.persona` mistake again: a rule evaluated where its
failure cannot be contained, which is what once crash-looped every mailbox in the deployment.

**`campaigns.mailbox_id` stays the primary and is always a member of its own pool**, and that is
the decision the migration rests on. Making `campaign_mailboxes` the sole authority would have
needed a backfill *and* a change to every path that attaches a mailbox -- `POST /campaigns`,
`PATCH`, and whatever comes next -- and the path that forgot would have stopped sending with
nothing on screen saying why. As it stands the migration is one empty table and one nullable
column, and a campaign created tomorrow by code that knows nothing about pools still sends.
`TestNothingChangedForAPoolOfOne` asserts that rather than assuming it, including that a lead
with `mailbox_id` NULL -- which is every lead in the live database -- is still sendable. Treating
NULL as "not yet eligible" would have stopped an in-flight campaign at deploy.

**One function answers "what is this campaign's pool?", and one answers the inverse.**
`pool.pool_for` and `pool.campaign_ids_for` live in the same module because the send loop works
mailbox by mailbox and therefore asks the question the other way round. Each caller joining
`campaign_mailboxes` itself is this project's dominant defect waiting to happen: the first
condition added to the rule would reach one caller and not the other, and the symptom would be a
campaign sending from a mailbox the screen says it does not use.

**Selection is by lowest weighted load, with no stored cursor.** Today's count per member comes
from `messages`, which is already the only source for the cap (§9.2) -- so the rotation and the
cap can never tell different stories about how much a mailbox has sent, and there is no second
number to keep right. The member with the smallest `sent_today / weight` goes next, and **ties
break on the lowest mailbox id**: a tie broken at random would hand the same unassigned lead to
two mailboxes in one pass, and the claim would then refuse one of them for no reason anybody
could see.

**The cap stays per mailbox.** A pool spreads a campaign's sends over several reputations; it does
not raise any one of them. Two mailboxes capped at 100 are not one campaign sending 200 from
addresses that each promised 100 -- which is the number ADR-034 pauses a mailbox over.

**An unusable member is skipped rather than fatal, and a partly broken pool is not called
blocked.** The campaign's blockers gain `pool_member_unavailable`, naming the affected addresses
and saying how many can still send; `mailbox_unavailable` stays for the case where none can.
Degrading to a smaller pool is the point of having one, and ADR-034 pauses a mailbox that is
bouncing badly -- a pool that stopped because one member was paused would turn that safety signal
into an outage, and a campaign that reported itself blocked over one of three mailboxes would
have somebody stop a campaign that was sending perfectly well.

**Removing a mailbox from a pool leaves its leads on it, and the response says so.** Reassigning
them would do the exact thing the sticky sender exists to prevent, to the people furthest along
in a conversation. Removal stops a mailbox taking *new* leads; the threads it already owns finish
on it. The primary cannot be removed this way at all -- its membership comes from
`campaigns.mailbox_id`, and changing that is `PATCH /campaigns/{id}`, where a screen can explain
what it does.

**Weights are bounded 1-100 by a check constraint built from the constants in `models.py`**, so
the constraint and the route that validates a weight cannot disagree -- the cure adopted after
`reason="one-click unsubscribe"` made every opt-out a 500.

**One of my own tests was wrong about the thing it was testing**, and it is worth recording
because the code was right. I traced weight 3 against weight 1 as "three sends in a row", which
is not what proportional load means: after one send from the heavy mailbox its load is 1/3 and the
light one's is 0, so the light one goes next. The test asserts the **share over eighty sends**
now, which is what the weights actually promise, instead of a hand-traced sequence of two.

**And one nearly-wrong assertion in the F7 blockers.** My first version asked
`unavailable_reason` about each pool member and reported nothing for a *paused* one -- because
that function answers a different question, whether the credential can log in. A paused mailbox
and an unauthenticated one are the same thing from a campaign's point of view, and the test for
the ADR-034 case is what showed it: `pool_member_unavailable` was absent for a mailbox paused on
bounce rate, which is the commonest way a member drops out.

34 new tests (10 weighted-selection unit, 24 integration), ruff and mypy strict clean, migration
applied and reversed against the development database.

**A booking tool can now tell us a meeting was booked, and the state machine could not record
the commonest one (F15, ADR-045, slice S14.2).** `booking_webhooks`, `booking_events`,
`POST /hooks/booking/{token}`, and `/booking-webhook` plus `/bookings` on the dashboard.

**The headline is the defect this slice found in the system it was added to.**
`MEETING_BOOKED` was reachable only from `REPLIED` and `IN_CONVERSATION` -- so the outcome this
whole product exists to produce, somebody clicking the booking link in a **first** email and
taking a slot without ever replying, **could not be recorded at all**. The webhook's own first
test found it: the booking was verified, stored, matched the right lead, and the lead sat in
`SENT`. `SENT`, `FOLLOWUP_SENT` and `NO_RESPONSE` allow it now; `NEW`, `APPROVED`, `OPTED_OUT`
and `BOUNCED` do not, and it still never reopens.

**And the test that should have caught it asserted the wrong thing.**
`test_meeting_booked_is_never_reached_from_a_machine_state` checked that `SENT -> MEETING_BOOKED`
was illegal, *as a proxy for* "only a human may set it". The real property is about **who** may
declare a meeting, not where the lead happened to be. It is rewritten to assert that, with a new
test that no reply label's policy names the state -- the half that was actually about the rule.
This is the same shape as `test_mcp_safety`'s "every tool is read-only", which was a proxy for
"nothing can send or approve" and had to break the moment anything wrote. **That is now the sixth
test in this project found to be encoding something other than the rule it was named for.**

**A booking is reported, not inferred, and that is the whole argument for touching the locked
rule.** A third party says a calendar slot was taken, signed with a secret this workspace pasted
into its own tool. A reply is somebody's words being interpreted -- which is what the prototype
did (issue B4) and what inflates every funnel it touches. So the rule stands as written and gains
a second producer that is not an inference.

**Two independent proofs, and neither alone is enough.** The URL carries an HMAC over the org id
under a key **derived** from `CREDENTIAL_KEY` -- the unsubscribe link's device (ADR-033), because
a public route must never take an `org_id` from a caller. And the body must be signed with that
workspace's own secret, HMAC-SHA256 over the **raw bytes**. A URL-only scheme makes the URL the
credential, and that URL ends up in a third party's settings page, their delivery logs and
anybody's screenshot of either.

**The raw bytes, never a re-serialised object.** Verifying against JSON we parsed and dumped
again is the classic way a correct-looking implementation accepts a forged body: key order,
spacing and number formatting all change under the round trip, so the only way to make it work is
to be lax, and lax is what the signature was for.

**Deliberately the scheme Cal.com already implements** -- `X-Cal-Signature-256` *is* HMAC-SHA256
of the raw body with your secret -- so the commonest tool needs no adapter, while nothing here is
specific to it. A timestamp, when a tool sends one, is part of the signed payload so it cannot be
edited forward, and a delivery more than fifteen minutes old is refused. **A delivery with no
timestamp is still accepted**, named as a compromise rather than left implicit: Cal.com signs the
body alone, so requiring one would mean refusing the tool this is built for. Retries -- which
every provider does and some do on a 2xx -- are handled by the unique key, so a second delivery
lands on the row it already wrote.

**Every refusal answers 204, having done nothing**, and a test asserts all four refusals are
indistinguishable. A public endpoint that tells an unknown workspace from a bad signature from an
unreadable body is an oracle for all three, and a provider reads any non-2xx as "retry". The
symptom an operator sees instead is a screen saying no booking has arrived -- which is why
`last_delivery_at` exists: it is the cheapest way to tell "no meetings yet" from "this was pasted
into the wrong field". A *verified* body we cannot read is the one case worth a log line, because
the signature proved it came from this workspace's own tool, so it is a shape we should support
and do not.

**There is no `GET` on the public path**, and that is a safety property. The unsubscribe endpoint
has one because a human clicks that link; nobody clicks a webhook URL, and the things that do
fetch URLs found in text -- scanners, link previews, a provider's own test button -- would
otherwise reach a write path.

**`intake` had a `BOOKABLE_FROM` set and it was deleted before it was committed.** It was a
second copy of `_ALLOWED`, and the first thing it did was disagree with it, claiming `NEW` was
bookable. The move is attempted and `IllegalTransition` is caught instead -- which is also how
the missing edge was found rather than papered over, and it is why an opted-out lead is still
refused without this file holding a list of who may be refused.

**A cancellation is recorded and does not move the lead.** `MEETING_BOOKED` is terminal with no
exits by construction; a meeting that was booked was booked, and widening a terminal state is a
change to a locked product rule rather than a side effect of adding a webhook. Stated as a limit
rather than discovered later.

**Every verified booking is stored, matched or not**, and the unmatched ones are what `GET
/bookings` is mostly for -- usually the lead replied from a second address or forwarded the link,
and dropping them leaves a funnel quietly one meeting short. Matching is on the address, with the
**conversation** breaking ties: when the same person is in several campaigns, the lead we most
recently wrote to wins, because taking the oldest or newest would attribute the meeting to a
campaign that never contacted them. `matched_by` records which rule applied, in words.

**It never creates a lead** -- that would put somebody into a sending pipeline for having asked
to talk -- and **nothing in the package can send**, asserted over its imports.

**Lead erasure now reaches a booking.** The row survives (`ON DELETE SET NULL`), because erasing
somebody must not erase the record that a meeting happened, and the invitee's address and name
are cleared. Without that the erasure would be decorative for exactly the leads who got furthest,
which is the worst available place to be wrong.

**And the F9 gap was closed in the one place it would have been silent.** `receive` takes
`SettingsDep` rather than the module-level `config`: the URL token is signed when the secret is
generated and verified when a delivery arrives, so if those two read different `Settings` objects
every delivery is refused -- answering 204, with nothing anywhere saying why.

67 new tests (39 signature and payload unit tests, 28 integration), ruff and mypy strict clean,
migration applied and reversed against the development database.

**Search had never once run, and F17's first job was to notice (F17, ADR-046, slice S14.3).
Phase 14 is complete.** `search_cache`, three provider adapters behind an ordered chain,
`/api/v1/search-keys`, and the one line in the research handler that was missing.

**The headline is that the feature this slice was meant to extend had never executed.**
`research_lead` has accepted a `search` and a `budget` since Phase 3. `_try_search` was correct
and tested. `build_provider` and `SearchBudget` existed, were tested, and were imported by
nothing. And `handle_research_lead` -- the only caller in the system -- **passed neither**, so
`search` was None on every real research job and the whole fallback was exercised only by tests
that handed a provider in themselves.

**That is the eleventh instance of this project's signature defect**: a correct producer, a
correct consumer, and nothing joining them. The same shape as research never queuing a draft, as
nothing ever writing a `Draft(kind="followup")`, as the two `draft_reply` payload shapes that
matched nothing, and as every outbound message going out with no `In-Reply-To`. Every one of
those was invisible to a suite that tested both halves separately, and every one was found by
looking at the join rather than at either side.

`tests/unit/test_research_passes_a_search_chain.py` closes the class rather than the instance: an
`ast` walk over `app/` asserting every caller of `research_lead` passes both keywords, with
string constants blanked first, because the comment in the handler explaining this fix names
both of them and prose must not satisfy an assertion about code. It was run against the unfixed
handler to watch it fail, and it fails naming the real keyword set --
`assert 'search' in {'fetcher', 'lead', 'offer', 'org_id', 'router'}`, which is exactly what
`main` was passing.

**The chain is `LLMRouter`'s shape, because it is `LLMRouter`'s problem.** Several providers, each
with a free tier, any of which may be down or out of credit, tried in an order the operator chose.
Reusing that shape rather than inventing a second one means there is one answer in this system to
"what happens when a provider fails?"

**The order is the feature, and it has two edges.** A hit short-circuits, so the second member is
never called and therefore never billed; and a **cache hit calls nobody at all**. Those are the
slice's two acceptance criteria and they are the only two properties here that cost money when
they are wrong. Both were run against a chain with the short circuit removed, which fails two
tests with the right words -- the second provider called, and the answer coming back from the
wrong member.

**A failure is not an empty answer, and that is the change the chain forced.** With one provider
the two were the same thing: `search()` returned `[]` on an outage, which was a correct way of
saying "degrade, do not fail the lead". With a chain they are opposite answers. "Nobody writes
about this company" stops nothing -- the next member is tried, because a provider that does not
index a small Indian company is the ordinary case this exists for -- and once every member has
answered, that empty result **is cached**, so the next lead at that company does not pay to be
told the same silence. "This provider is down" moves on and is **never** cached, because storing
it would let one bad afternoon stop a workspace searching for a week.

A provider reporting an outage as zero results makes those indistinguishable, which is the
job-post intake's expensive lesson in a costlier place: an unknown source with a plausible
payload returned zero postings and read as an empty board. So `search()` raises now, and the
chain catches both kinds and degrades exactly as the single provider did.

**A refused key is told apart from an outage, by two classes rather than one.** A 401 records
`invalid_reason` on the credential, so the next research job leaves that member out instead of
paying a request to be refused again. A 429 or a 503 leaves the key alone -- being out of credit
is not being wrong, and next month it works. Collapsing them is how a perfectly good key gets
recorded as dead because a provider had a bad afternoon.

**Every key is the workspace's own**, read from `provider_credentials` exactly as F9's contact
lookups are, with ADR-041's reasoning unchanged: there is no shared key, so every question about
who may use one has the same answer, and a workspace with none degrades to the company's own
pages rather than failing.

**The deployment's `TAVILY_API_KEY` survives as the last member, named as shared, and that is the
slice's one deliberate compromise.** ADR-023 removed the deployment's mailbox precisely because an
identity in the environment makes "who may use it?" a rule somebody has to maintain, and every
word of that applies here. It stays because removing it would stop the live deployment searching
at all, and it is contained three ways: always last, so a workspace's own key shadows it; recorded
in `search_usage` as `tavily:deployment` rather than `tavily`, so the ledger can say which account
a credit came from; and described on the screen as a free tier shared by everybody using it. A
workspace told "search works" without being told whose credits it spends has been told something
misleading.

**Where the cache actually earns its keep is narrower than it first looks, and one of my own tests
was wrong about it.** I wrote "two leads at one company, the second costs nothing" -- and it is
nothing to do with this cache: `domain_research` is keyed on the registrable domain and the second
lead is served from it before a search is even considered. The test failed, and it was right to:
it would otherwise have passed for the wrong reason the first time somebody changed the research
cache. What the search cache does save is a company researched **again** -- a run whose synthesis
failed after the search was paid for and stored nothing, or a row past `PERISHABLE_AFTER_DAYS`
whose hooks were all perishable and is therefore treated as a miss. The test now sets up exactly
that, asserts `from_cache is False` so the findings really were re-researched, and shows the
credit is not spent twice.

**Seven days, and the number does a different job from the research cache's ninety.** That one
governs findings verified against text we fetched. This governs somebody else's index of what a
company has been up to lately, which is the most perishable thing here --
`PERISHABLE_AFTER_DAYS` already drops the hooks that cite it after thirty days, and a cache
serving month-old news would be feeding precisely the hook that embarrasses us first. Expired
rows are **deleted** by the hourly sweep, unlike `domain_research`, which is only ever blanked:
those rows are part of the record of what a sent email cited, and a search answer nobody may
serve is a copy of somebody else's index with no audit value at all.

**The cache is per workspace, which is ADR-038's line held rather than widened.** That ADR made
`company_profiles` the one table in this schema with no `org_id`, and drew the boundary precisely:
a name, a careers URL and an ATS slug are public facts worth pooling; findings a draft goes on to
cite were paid for by one workspace out of its own credits and are not ours to pool. A search
answer is a provider's billable response to a question one workspace asked, so it sits with
`domain_research`. One exception to ADR-004 is a decision; two would be a pattern.

**The budget is per provider, and the number is ours rather than theirs.** Each member is a
separate account with its own allowance, so a pooled total would refuse a workspace's second key
for credits its first key spent. We cannot read anybody's plan, so 1,000 a month is a ceiling that
stops a loop spending somebody's month in an afternoon -- the same job `DAILY_LOOKUP_BUDGET` does
for contact lookups, and stated as that rather than as a claim about a free tier. The cache is
read **before** the ceiling is checked, deliberately: a cached answer costs nobody a credit, so a
workspace that has spent its month still gets the answer it has already paid for.

**Two provider families, one table, and the database cannot be what keeps them apart.**
`provider_credentials` now holds contact keys and search keys, because there should be one answer
to "where are the secrets and how are they protected?" and one key to rotate. The check
constraint is built from `ALL_PROVIDERS`, so adding a provider is one tuple and one migration --
but it accepts either family's names, so it cannot refuse a Tavily key stored through the contact
route, which would hand a search credential to a contact adapter and post it to somebody else's
endpoint. Each door narrows to its own family explicitly, the two name sets are asserted disjoint,
and there is a test for each direction.

**The verify button costs a credit and says so.** No provider here offers an "is this key valid?"
endpoint, so verification is a real search. ADR-021 is why it exists: a key pasted with a trailing
space, a green tick, and a fallback that quietly never runs is the failure that took a whole slice
to find the first time. It runs one member rather than the chain, because the question is whether
*this* key works and a chain would answer it by falling through to another provider and reporting
success.

**Brave and Serper have never been called**, and `docs/SEARCH_PROVIDERS.md` is the verified-versus-
assumed table, in `CONTACT_PROVIDERS.md`'s spirit. Each response is read in exactly one function,
so a wrong assumption is one function and one test. The differences are exactly the kind that fail
for one source only -- Brave nests under `web.results` and calls the snippet `description`, Serper
calls the URL `link` -- which is the ATS feeds' lesson, where Lever's bare array made a parser
that worked for two sources and read as a bad board slug for the third.

51 new tests (24 chain and adapter unit tests, 3 structural, 24 integration), ruff and mypy strict
clean, migration applied and reversed against the development database.

**Every job posting from a feed recorded the applicant tracking system as the company, and three
callers acted on it (ADR-047, slice S15.0).** `job_posts.board_slug`, one function that decides
what a company domain is, one that answers which company a posting is for, and a migration that
rewrites the rows already holding a vendor host.

**Found while designing F11's watchlist**, which has to join a signal to a company and would have
joined on that column. `job_posts.company_domain` was `urlsplit(apply_url).hostname`, and for
every posting that arrived through a feed that host is the board's:
`job-boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`.

**The evidence had been in the repository since F5 landed.** `tests/fixtures/feeds/` holds three
real trimmed responses and every apply URL in them is the board's. The parser was tested against
those fixtures and the storage was tested; **nothing tested what the stored value meant.** The
same gap as the platform router comparing `direction == "outbound"` against a column holding
`'out'` -- a value that is wrong rather than missing, in a column whose name says it is right.

**Three callers, and each of them spent something real.**

1. **A contact lookup searched the ATS vendor.** `lookup.run` took the posting's domain, so
   asking who is hiring at a company spent one of the user's **own provider credits** looking for
   people who work at Greenhouse. F9's entire design is about not billing somebody for work they
   did not ask for, and this billed them for work nobody could want.
2. **An imported contact became a lead whose `company_url` was the vendor**, which the research
   pipeline fetched, synthesised and cited in a draft -- and `cache_key()` reduces that host to
   `greenhouse.io`, so **every feed-sourced lead in a workspace shared one `domain_research` row**
   and the second was served the first's findings. That is ADR-011 exactly: the defect it was
   written to fix, arriving through a door that did not exist when it was written.
3. **The referral matcher looked for connections at "boards"**, taking the host's first label, so
   it matched anybody whose company name contains that word and offered to ask them for an
   introduction.

**The fix is NULL, not a cleverer guess.** A column that holds the ATS vendor for every feed
posting is worse than an empty one: empty makes a caller ask, and `job-boards.greenhouse.io` makes
three callers confident. One empty column is a feature request; one wrong column was three bugs
that each cost something.

**What a feed does tell us is the slug** -- `vercel`, `leverdemo`, `ramp`, the first path segment
of the apply URL, which is the company's own identifier on that board and is already the key
`watched_boards` uses. Read off the three real fixtures rather than from documentation.

**`company.resolve` answers in three steps and refuses rather than guessing.** The posting's own
domain; then `company_profiles` by `(ats_type, ats_slug)`, which is F19's record of exactly that
mapping and is a fact some workspace established from a careers page; then nothing, with the slug
as a readable label. `vercel` is not reliably `vercel.com` and `ramp` is not `ramp.com` for every
Ramp -- hard rule 6 forbids inventing facts about the sender, and this is the same rule pointed at
the recipient. `CompanyRef` keeps `domain` and `label` as separate fields so a caller cannot use
the readable one as a domain by accident.

**`BOARD_HOSTS` is deliberately longer than the three feeds we poll**, because the question it
answers is not "can we poll this?" but "would storing this host as a company's domain be a lie?"
For a pasted Naukri, LinkedIn or Workday posting it would be. Nothing fetches those; the list
exists so the column stays honest whatever somebody pastes. The match is on a suffix anchored at a
dot, so `greenhouse.io.example.test` stays somebody's real domain.

**The migration rewrites rows, which no migration here has done before.** These values are not
stale, they are wrong, and leaving them would mean the fix applied to postings fetched after the
deploy and not to those already stored -- one screen behaving two ways depending on when a row
arrived. The backfill's host list is narrower than the application's, covering only hosts the old
code could have written, because matching the full list in SQL would risk blanking a domain that
is genuinely a company's. **The downgrade deliberately does not restore them**: putting them back
re-arms a lookup that bills for contacts at Greenhouse and a research run that drafts an email
about a job board.

**The backfill imports `NOT_A_SLUG` from the parser rather than restating it in SQL.** Two copies
of one rule with its halves in two languages is the signature defect in its most durable form. The
SQL expression was also run against the real URL shapes on the development database and produces
the same answers as the Python -- including `embed` for Greenhouse's embedded form, which the
second statement then nulls, exactly as `slug_from_url` excludes it.

**And the test harness had a hole of the same shape.** `company_profiles` (ADR-038) and
`platform_operators` (ADR-024) are the two deliberate exceptions to ADR-004, so neither has a
foreign key for `TRUNCATE app.orgs CASCADE` to follow -- and neither was in the truncate list. Both
survived every test and leaked into the next. Found by a test writing one shared company row twice
and hitting its unique index, which is luck; a test asserting an absence would have passed while a
previous test's row sat there.

**A hard-rule-2 near miss, and the same one this file already records.** A failing assertion
printed a real `GROQ_API_KEY` into the terminal, because my test called `lookup.run` without
`settings=` and it fell back to the module-level `config` singleton -- built at **import** time,
which is before the session-scoped `hermetic_settings` fixture can detach `Settings` from `.env`.
That is the exact gap written up in F9's notes, hit again in the same way. The tests now pass
explicit settings. The key was already on the owner's list to rotate; it still is.

22 new unit tests and 10 integration, ruff and mypy strict clean, migration applied and reversed
against the development database.

**Two phases of work had no screen at all, and the dashboard reached 30 of 90 routes (ADR-048).**
`JobHunt.jsx`, `Sending.jsx` and `Operations.jsx`, three new nav items, and a guard that every
path the dashboard asks for is a path the API serves.

**Reported by the owner: "i can't see any changes in ui so far what we have built".** They were
right, and the number is the finding: the API served **90 paths and the dashboard called about
30**. Everything built in Phases 12 and 13 -- F5 job postings and the daily board poll, F6 the
candidate's own confirmed facts and answer bank, F9 hiring-contact lookups, F10 referrals from a
LinkedIn export, F20 tailored resumes and the application tracker -- was reachable **only with
curl**. So were F4's deliverability report, F7's mailbox pool, F15's booking webhook and F17's
search keys.

**This is Phase 8's failure repeated at ten times the size.** That one was found the same way:
the deployed dashboard signed in correctly, said "No campaigns yet", and offered no way to make
one -- so import, research, drafting and sending were all gated behind a record that could not
exist. *The system was deployed, correct and unusable, and only looking at it showed that.*
Eleven slices of backend later, nobody looked again.

**Three of the invisible features were not merely inconvenient, they were inert.**

- **The booking webhook needs a URL and a secret pasted into a third party's settings page.** An
  API-only feature means the webhook is never wired up, and the only symptom is a funnel that
  reports no meetings -- which is indistinguishable from nobody booking one. `last_delivery_at`
  was built precisely to tell those apart and had nowhere to be shown.
- **A tailored resume has to be read before it is attached.** ADR-043 rests on that: the
  validator refuses an invented number or skill, and what it cannot catch is a lower-case
  exaggeration, so a person reads the document. There was no way to read one.
- **A dead job needs a requeue button.** These notes already record three dead reply-drafting
  jobs recovered **by hand with SQL against production**, because the only way to see them was a
  query and the only way to retry one was an `UPDATE`.

**The screens keep the product rules rather than restating them.** Nothing on the job-hunt screen
contacts anybody: the referral note is text to copy, a contact lookup writes a proposal, and
marking an application applied records what the person did themselves. A fact mined from a resume
is shown as a proposal until they confirm it. A lookup spends their own credit, so it never
happens on load, on a refresh or on a tab change -- the same rule that keeps `GET /mailboxes`
non-networked. And the applied-to-planned move is simply not offered, because the API refuses it
and a button that always fails is worse than no button.

**The deployment's shared search key is named as shared on the screen**, with its month's usage
beside it, because a workspace told "search works" without being told whose credits it spends has
been told something misleading.

**The guard is the part that outlasts this slice.** `tests/unit/test_dashboard_calls_real_routes.py`
reads every call site in `frontend/src/*.jsx`, normalises the template holes, and compares them
against the live OpenAPI paths. A screen asking for a route that does not exist now fails in CI
rather than the first time somebody opens it -- which is how `/connect/google/status` came to be
called without the `mailbox_id` it accepts, and how a review queue was deployed calling a cursor
parameter the running API did not have.

**And writing that guard produced three of my own mistakes, each the shape it exists to catch.**

1. **It was blind to the code it was written for.** The first version matched only `api.js`'s
   helpers -- and both new screens load through a local `useLoad` wrapper, so pointing a panel at
   `/admin/deliverability`, which does not exist, did not fail it. A guard whose passing is read
   as evidence while it sees nothing is worse than no guard. `test_the_helper_list_is_complete`
   now reads `api.js` and fails if a path-taking helper is not in the list, so the next one cannot
   silently narrow the check.
2. **It reported working code as broken, twice.** A regex for `${...}` stopped at the first `}`
   and at the first inner quote, so `/leads/${lead.id}/${paused ? 'resume' : 'pause'}` and
   `${provider || providers[0]?.provider || ''}` were both reported missing. A guard that fails on
   correct code is one somebody deletes to get CI green.
3. **Set membership was the wrong comparison.** That same leads path has a *literal chosen at run
   time* as its last segment, which has to match `/leads/*/pause` or `/leads/*/resume`. It matches
   segment-wise now, with `*` on either side.

**`test_dashboard_state_is_declared` had the same scope problem**, and it is the one written after
a button shipped throwing `ReferenceError`. It read **`App.jsx` only**, so neither new file was
checked -- a guard scoped to one file stops applying the moment somebody splits a component out,
which is exactly when a new identifier gets typed. It globs `frontend/src/*.jsx` now and also
recognises an arrow component's destructured props.

**And I invented a second design vocabulary before noticing the first.** `panel`, `rows`, `row`,
`row-actions`, `stack`, `field-label`, `field-hint` and `small` do not exist in `app.css` -- so
every one of those elements would have rendered unstyled while the build stayed green. They are
`card`, `list`, `list-row`, `btn-row`, `form-label` and `hint` now, with `.tabs`, `.tab`, `.list`
and `.list-row` added to the stylesheet from the existing tokens, since tabs genuinely did not
exist. The rule that a component never reaches past the token layer is kept.

**What is honestly unverified.** The build is green, the frontend suite passes, both structural
guards pass and all 82 reachable paths are served -- but **these screens have not been rendered
in a browser against a live API**, because that needs Supabase auth and a running backend. Vite
does not resolve identifiers and the frontend suite renders nothing, which is exactly how a
`ReferenceError` button shipped here before; the two structural guards are the substitute, and
they are a substitute rather than a proof.

**Coverage went from 30 of 90 routes to 82 of 84.** The two remaining are duplicates of routes
that do have a screen (`/admin/jobs` lists what `/admin/dead-jobs` lists; `POST /mailbox` is the
singular form of `/mailboxes`).

**The Hiring contacts tab blanked the whole dashboard, and the blank page was the worse half
(ADR-048 addendum).** `/contact-candidates` returns `contact_candidates`; the screen I had just
written read `data.candidates`. `undefined.length` threw during render, React unmounted the
entire tree, and the operator got a white page -- no message, no sidebar, no way back except
reloading onto a screen that did it again.

**The signature defect once more, and one day after I wrote a guard for exactly this boundary.**
`test_dashboard_calls_real_routes` checks the *path* a screen asks for, and it passed, because
the path is right. Nothing compared the **field names** -- which is the half that fails silently,
and silently here means the tool stops working rather than one request failing. A correct route
and a correct screen with nothing checking they agreed, in a file I wrote while describing that
pattern.

**Three fixes, and the typo is the least of them.**

1. **The key.** One line.
2. **`ErrorBoundary.jsx`, because any single mistake in any panel should not be a total outage.**
   A render error is now caught at the screen boundary: the sidebar stays, the message names the
   screen and the error, every other screen still works, and the console has the component stack
   -- which is the one thing an operator can paste into a report. It is keyed on the current view
   so leaving a broken screen clears the error rather than latching it, which would have left
   most of the original problem in place. This is the only class component in the codebase,
   because a `try`/`catch` cannot reach inside React's render. The same reasoning as the 500 that
   arrived outside `CORSMiddleware` and made a duplicate key look like an unreachable backend: a
   real fault reported as something else, with the useful detail discarded.
3. **`tests/unit/test_dashboard_reads_real_fields.py`, which prevents the class.** It parses
   every `return {...}` of every GET handler with `ast`, reads every `const x = useLoad('/path')`
   and every `x.data.field` in the dashboard, and fails when a screen reads a field its route
   does not return. Run against the unfixed file it reports
   `JobHunt.jsx: candidates.data.candidates` by name. Two vacuity tests sit beside it, because a
   parser that stops understanding the handlers would otherwise make it pass trivially -- the
   `tools/list` lesson.

**The pair is the point**: one guard says the screen asks for a route that exists, the other says
it reads fields that route returns. Between them a screen cannot be deployed asking for something
imaginary, which is how `/connect/google/status` came to be called without the `mailbox_id` it
accepts.

**And the honest note from the slice before this one earned its keep immediately.** I wrote that
those screens had not been rendered in a browser and that the guards were a substitute for
looking, not a proof, and that the first operator to open them should expect to find something.
The first operator opened them and found something within a day. The guards were the wrong shape;
the warning was the right shape.

**Outreach has a reason to be sent today, and the screen shipped with it (F11, ADR-049, slice
S15.1).** `watchlist`, `signal_events`, a producer inside the poll that already runs,
`/api/v1/watchlist` and `/api/v1/signals`, and a **Signals** screen.

**One fetch, two readers.** The daily ATS poll has been reading public boards since F5 for the
job-hunt side. Nothing read them for outreach -- yet a company that has just opened three
engineering roles is a company with a budget and a problem, which is the difference between
writing to them this week and writing to them at random. A watch adds no second poll: it looks
for the board first, because `uq_watched_boards_org_source_slug` would otherwise raise an
integrity error from a path nobody expects one from.

**This is the slice ADR-047 was worth stopping for.** A feed posting carries the board's slug
and **no company domain at all**, so matching a posting to a watched company through
`company_profiles` would mean a watch only worked once somebody had researched that company.
The slug is stored on the watch and the match is direct. The domain stays the company's
identity; the slug is how the signal arrives.

**A signal proposes, and never creates a lead.** The send loop reads `leads` and nothing here is
in that table. A company hiring is a reason to write to somebody there -- it is not consent, not
an address and not a person, and a signal that created a lead would put somebody into a sending
pipeline because their employer posted a job. **Nothing here drafts or sends either**:
`review_all` means a person writes and approves every first email, and ADR-034's rule applies in
spirit -- a thing that acts on its own may only ever restrict, and this one only ever adds a row
to a list somebody reads. Asserted over the package and its router with `ast`: no send import,
no `save_draft`, no `Lead(...)`, no `transition`, with string constants stripped first because
the prose explaining each rule names every symbol it forbids.

**A lead is matched, never inferred**, through the same `cache_key` the research cache uses. No
fuzzy name matching: a reason-to-write attached to the wrong person is invisible to whoever
sends the email. **Terminal leads are excluded**, which is the judgement inside that function --
somebody who opted out is not made approachable again by their employer posting a job, and a
suggestion beside their name is the worst available place to be wrong.

**A signal with no lead is the interesting case, not the error case**, and the list reports the
count: a company worth approaching with nobody to approach is exactly what F9's lookup button
is for.

**The producer ships in the same slice as the table**, because a table with no writer is how
search came to be wired to nothing for three phases and how a `Draft(kind="followup")` was never
once created. It counts *updated* postings as well as new ones, since the first poll after a
watch is a poll of rows already stored -- a producer that only looked at new ones would produce
nothing until that company posted again, which for a quiet company is never. The unique key
`(org_id, kind, source, external_id)` is what stops that becoming duplicates, the booking
webhook's device for the same reason: the poll sees yesterday's postings again every morning.

**Forgetting a company keeps its signals.** `ON DELETE SET NULL` plus the signal's own `domain`,
because deleting the record that a company was hiring in March would quietly rewrite a funnel.
The board stays in the poll: it may be there for job hunting, and a watch is not its owner.

**And the screen shipped with the tables**, which is the standing rule now.

**Two things found by opening the deployed dashboard, which is the other half of that rule.**

- **The owner's Gmail connection has expired**, raising `mailbox_grant_expiring` every hour for
  two days. That mailbox can neither send nor fetch replies until somebody presses Reconnect.
  ADR-018's seven-day refresh token, working exactly as written down -- and now visible, which
  it was not before Operations existed.
- **Operations showed one problem as fifty rows.** The cool-down is per kind and the mailbox id
  is part of the kind, so one alert an hour for two days is correct; the screen rendered every
  one of them. A screen built to make a problem actionable had turned it into a wall to scroll.
  Alerts are grouped by kind now, with a count and the first and latest times, and acknowledging
  a group acknowledges every row in it -- acknowledging the newest and leaving forty-nine
  identical ones behind is the bug rather than the fix.

22 integration tests and 6 structural, ruff and mypy strict clean, migration applied and
reversed against the development database. mypy caught `Lead.email`, which is `email_norm` --
the same naming trap as `Event.at` and `SearchUsage.at`, for the fourth time, and found by the
type checker each time rather than by a screen reporting nothing for ever.

**A Gmail connection that had expired still read as connected, everywhere (ADR-050).** Reported
by the owner: the alerts said it had expired; the campaign screen and the sending-account panel
both said connected.

**Both were right about what they were computing, which is why it survived.** `invalid_reason` is
set only once a provider has **refused** a credential -- so a grant that lapsed on Friday and had
not been used since carried no reason at all, `unavailable_reason` returned None, and every
reader of it takes None to mean the mailbox works. `grant_status` separately knew it had nothing
left, and the hourly sweep was raising `mailbox_grant_expiring` about the same row every hour.

Two correct functions answering adjacent questions, with the screens asking the wrong one. The
same shape as the pool blocker that reported nothing for a **paused** mailbox, and as
`direction == "outbound"` against a column holding `'out'`.

**`GET /mailboxes` disagreed with itself inside one row**: `connected: true`, `usable: true`,
`days_left: 0`, `expiry_notice: "This Gmail connection has expired."` Four fields, two questions
-- and whoever reads a row like that believes the reassuring one.

**The fix lands in one function, which is the whole argument for that function existing.**
`unavailable_reason` is what the campaign blockers, the mailbox list and the send loop's pause
reason all ask, so consulting `grant_status` there fixes all three at once. It reuses
`GrantStatus.notice`, the sentence the dashboard and the alert already use, because somebody who
sees three wordings for one state assumes three problems.

**Reported from the arithmetic, not from Google**, deliberately. `login_for` still asks Google,
which is the authority on whether a token works -- but "should this person be told to reconnect?"
needs no network call, and a read path that minted a token to find out is the side effect ADR-021
refused. It errs conservative near the boundary: `days_left` rounds up so eleven hours left is
not expired, and reconnecting costs one click where the opposite error costs a silent outage.

**`connected` and `needs_reconnect` are two views of one answer now**, derived from a single call.
A row cannot contradict itself when only one thing is being asked, and the test asserts that
across four grant ages rather than the single instance that was reported.

**And a second defect found while reading the same function: `connection_status` returned
`invalid_reason` verbatim.** That column can quote a provider's own reply, and the response
reaches a browser, a screenshot and anything that logs a page. The rule is kept everywhere else --
`GET /mailboxes` carries a boolean and has a test asserting the stored text is absent -- and this
route was the one place still echoing it. It carries no `reason` field at all now, with a test.

10 integration tests. Two of them fail against the unfixed code, which is how they were checked.

**A claim with no source cannot be stored (F12, ADR-051, slice S15.2).** `entities`, `edges`,
a producer inside the signal intake, `GET /graph`, and a "What we know" panel on the Signals
screen.

**`ck_edges_provenance` is the slice.** An edge must carry a `source_url` or a `message_id`, so
"we know Acme is hiring" cannot be written down without the thing that says so.

**A check constraint rather than a rule in a service**, and the distinction is the whole point. A
service is bypassed by the next writer, and the next writer is always somebody in a hurry with a
reasonable-sounding exception -- "just this one, the source comes later". A constraint has no
sympathy. The test for it writes **raw SQL**, because testing it through the service would only
prove the service is careful today.

**The service refuses first anyway**, so a caller that forgets is told which claim it was rather
than handed an `IntegrityError` three frames from a worker pass that was storing postings at the
time. The constraint is the last line of defence, not the first.

**A model may phrase an edge and may never add one.** Nothing writes here from a model's output:
the producers are a job posting, whose apply URL is the source, and an inbound message, whose id
is. S15.3's brief will read these rows and may reword them; it cannot introduce a claim, because
there would be nothing to put in the provenance columns. That is stronger than asking a model not
to -- it makes the sentence unsayable. And the source travels with **every line** rather than
beside the set, which is what makes the rule checkable from outside: a sentence that arrives
without one did not come from this table.

**`uq_edges_claim` is `NULLS NOT DISTINCT`, and that detail is load-bearing.** Three of its seven
columns are null in any given row -- an edge names an object *or* a text, and carries a URL *or* a
message id -- and under Postgres's default rule two NULLs never collide, so the index would have
deduplicated **nothing at all**. The daily poll sees yesterday's postings again every morning, so
an index silently not working is a duplicate claim a day until a brief cites one sentence six
times. Postgres 15+; this project runs 17 in development and in production, checked rather than
assumed.

**`entities.lead_id` is `SET NULL` and `edges.message_id` is `CASCADE`**, which looks
inconsistent and is deliberate. Erasing a lead must not delete what we learned about their
*employer*; but an edge whose only provenance is a message must go with that message, because a
claim that outlived its source is exactly what this table refuses to hold.

**A fourth copy of "what company is this?" was caught by a test before it was committed.** Four
things key on it -- the research cache (ADR-011), the watchlist, the graph and the signal
producer -- and the graph handed a whole URL to `registrable_domain`, which wants a host and
quietly returned something that was not a domain. `https://www.acme.example/x` and `acme.example`
became two nodes for one company. The rule now lives once, in `net.company_domain`, beside
`registrable_domain` where it should have been from the start; the watchlist's own copy is gone
with it.

**And the live check that prompted the last fix found another one.** The Signals screen shipped in
the previous slice renders on the deployed dashboard and reports **Not Found** -- because
Cloudflare rebuilds the dashboard on every merge and **the API is deployed by hand**. The live
backend is an older image, so F17's search chain, ADR-047's board-host fix, F11's signals and
ADR-050's expired-grant fix are all merged and none of them is running. `/mailboxes` answers 401
and `/watchlist` answers 404, which is the whole diagnosis. This repository already recorded it
happening once, for PR #57; it has now happened across four slices, because a merged PR looks
shipped. The error boundary did its job -- the screen degraded to a message instead of blanking
-- but a 404 cannot say "this API is older than this screen", and that is the fix worth building
next: the API reporting its build and the dashboard comparing.

19 integration tests, ruff and mypy strict clean, migration applied and reversed against the
development database.

**A brief line carries its own source, and a model may only reword it (F12, ADR-052, slice
S15.3).** Five sections, every line with the page or the message that says so, and a `gaps`
section that names what the brief does not know.

**The sections are a fixed tuple and `Line.__post_init__` refuses anything outside it**, so a
section a model could invent is a section with nothing to put in it -- the same device as
`ProposedEdits` having no field for a heading and `propose_reply` having no `subject`. And a line
with neither a `source_url` nor a `message_id` is refused where it is built, which is
`ck_edges_provenance` one layer up, in the reader.

**The decision somebody will want to reverse is what is left out.**
`domain_research.findings.what_they_do` is the best prose this system holds about a company and
it is a synthesis of several pages, so no single page says it. A line citing "one of these four
URLs" is a line whose source cannot be checked, and somebody would repeat it on a call. So the
synthesis is reported in `gaps` as something we hold and cannot cite, and the reader is sent to
the research panel. The rule being protected is not "cite something", it is "a line and the page
that says it".

**The header is not a claim, and that is why it is above the sections rather than in one.** Who
the lead is came from the operator's own spreadsheet, and the brief says so. Putting it in a
section would have forced a fake citation on the one line that genuinely does not need one,
which is how a citation becomes decorative.

**A model may phrase and may never add, and the contract is what makes that true rather than the
prompt.** `{line_id: text}` in, `{line_id: text}` out. It cannot add a line, because an invented
claim has no id; it cannot change what a line says, because `domain.wording.invented` refuses a
number or a named thing the original does not contain; it cannot drop a line, because a line it
does not mention keeps its own wording -- silence is not deletion, and omitting the one awkward
line would be the quietest way to mislead. It never sees a source at all, so it cannot move one.

**The check was extracted rather than copied, because a second caller appeared.**
`domain/wording.py` is ADR-043's resume rewording check -- invented numbers, invented named
things -- asked here of the same kind of edit, with `resume/validate.py` delegating to it. Two
copies of that rule would have been the signature defect in the one place where a wrong answer
is a sentence somebody says out loud to a stranger.

**The free route and the spending route are different routes.** `GET /leads/{id}/brief` calls no
model, so a refresh costs one query set -- the rule that keeps `GET /mailboxes` from minting an
OAuth token and a contact lookup from spending a credit on a page load. A test asserts the GET
writes no `llm_usage` row. `POST /leads/{id}/brief/phrasing` is the button, and a refused
rewording is shown on the screen rather than swallowed, because a model trying to add a claim is
worth seeing.

**A model failure is not a brief failure**, which is the opposite trade from drafting. The
deterministic text is already a correct brief, so an outage leaves it exactly as read with a
note. There, a failed model means no email; here the work is done and the model is polish.

**And the MCP brief now reads the same builder** rather than composing the same rows a second
time. Two readers of one table eventually disagree -- this project has paid for that eight times
-- and these two would have disagreed about what we can *prove*.

17 unit tests and 10 integration. The two invention tests were run against the check removed, to
watch an invented `Kubernetes` sail through. mypy caught `Lead.name`, which is `full_name`: the
fifth instance of that naming trap and the fifth time the type checker found it rather than a
screen showing the wrong thing for ever.

**Two columns ADR-026 declared and nothing ever wrote now have a producer (F13, ADR-053, slice
S15.4).** `messages.variant`, `messages.warmup_day`, the `experiments` table and
`warmup_day_number` have all existed since the outcome dataset landed, and **every one of them
was read by the export and written by nobody** -- so the dataset has been exporting NULLs into
its two most useful dimensions. The twelfth instance of this project's signature shape, and the
cheapest kind to find: a grep for the producer returns the constant and its consumers.

**ADR-026 specified stickiness as a deterministic hash of (lead, experiment), and building it
showed that cannot work.** A hash is taken modulo the number of arms, so adding a third arm to a
running experiment moves most leads already in flight from one arm to the other -- silently,
after their first email has gone out, with every message already sent then attributed to an arm
its draft did not come from. And the funnel would look fine. The arm is **stored on the lead**
instead, written once, never recomputed; there is a test named after that exact scenario.

**Random rather than round-robin**, which is the other half of making a result mean anything.
Round-robin looks fairer and correlates the arm with arrival order: a sheet sorted by company
size, or an import done in two halves on two days, puts systematically different people in each
arm and the difference shows up as though the *email* caused it. The rule is pure and takes an
injected `Random`, so the spread is asserted with a seed rather than hoped for -- including the
absence of alternation, because round-robin would produce zero repeats.

**A fixed exploration share is always held back, with a floor in the database.** Weights let an
operator push most of the volume through the arm they believe in, and a weighted allocator with
nothing held back can drive an arm to zero -- which is exactly when it stops producing the data
that would show the belief was wrong. So a fifth of assignments ignore the weights entirely, and
`ck_experiments_exploration_floor` refuses a row below 0.1. The screen reports each arm's
**effective** share rather than the weight somebody typed, because the two differ once the floor
applies and being surprised by that is how somebody concludes the allocator is broken.

**An arm may vary exactly one thing: how to write this one.** Not the persona, not the cap, not
the window -- those change who is emailed and how much, so a difference in results would not be
a difference in the email. And the instruction cannot widen what may be said: the draft goes
through the same `validate_draft` and the same claim check, so an arm asking for a statistic the
research does not support fails validation instead of sending one. Hard rule 6 holding whatever
an operator types into a form.

**Pending is a column, not a rounding error.** A lead still inside its wait window has not
produced a result, and counting it as a failure would make every arm look worse the sooner
somebody looked -- the one way a funnel actively misleads. So the funnel reports `pending`
separately, `censored` for the leads watched to the end of their window, and rates over settled
leads only, with a sentence on the screen saying so. Counted from `messages` and `lead_outcomes`
rather than from a counter, for the reason the mailbox-health rollup already chose: a counter
incremented at send time drifts, and a crash between the send and the increment leaves it wrong
for ever with nothing to notice.

**And the outcome vocabulary is read from the enum rather than restated.** My first version of
the funnel hard-coded `"replied"`, `"bounced"` and `"opted_out"` -- none of which are values of
`Outcome`, whose vocabulary is `positive_interest`, `has_vendor`, `referral`, `bounce` and nine
others. Every bucket would have reported zero of everything while looking perfectly healthy,
which is `direction == "outbound"` against a column holding `'out'` for the fourth time. The
bucket now reports each outcome's own value and asks `Outcome.is_encouraging`.

**One real defect, caught by the test for the refusal rather than by anybody noticing.**
`float(payload.get("exploration_share") or EXPLORATION_SHARE)` -- `0 or 0.2` is `0.2`, so asking
for no exploration at all would have been silently granted the floor and the request would have
looked honoured. The identical defect as `daily_cap or 100` turning an explicit 0 into 100, which
this file already records from Phase 8.

14 unit tests and 20 integration, ruff and mypy strict clean, migration applied and reversed
against the development database.

**The daily poll can now say a role was filled, and a company with no ATS can be watched at all
(F22, ADR-054, slice S15.5). Phase 15 is complete.**

**`job_posts.status` has said `open | archived` since the first migration and had never once
held the second value.** The poll only ever added, so a posting that left a board sat there for
ever and "did they fill that role?" had no answer. `close_missing` archives the open postings of
one board that the current poll did not return.

**The safety argument is entirely about where that is called from.** `fetch_board` raises on any
failure long before, so the diff is never reached with an accidentally empty set -- which would
archive a company's entire board on one bad afternoon and report it as every role being filled.
The job-post intake has already paid for the neighbouring version of this: an unknown source with
a plausible payload returned zero postings, indistinguishable from an empty board. A *genuinely*
empty board is still an empty board, because a company that closed its last role is a real event
and refusing to believe it would be second-guessing the only evidence we have. Archived rather
than deleted, because `applications` may point at a posting somebody applied to. And a relisted
role **reopens** rather than becoming a second posting, since the id is the board's own.

**A careers page can be watched by its hash, and may only ever say that it changed.** Most
companies this product cares about are on no ATS, so the poll had nothing to read for them. Fetch
once a day, hash the visible text, record a signal when it moves.

**It produces a signal and deliberately no graph edge**, which is ADR-051 earning its keep one
slice later. "The text of this page is different today" is evidence that something happened and
no evidence of what, so there is nothing to put in the provenance columns beyond a URL -- and an
edge saying a company is hiring because their careers page changed is exactly the unsupported
claim that table refuses to hold.

**Scripts and styles are stripped before hashing**, because a build id or an analytics nonce
changes on every deploy and hashing it would report a change every single day -- the same as
reporting nothing, except somebody would act on the first few. **The hash and never the text**:
storing the page would make this a crawler with a cache, somebody else's copy of their own site
in our database for every company any workspace watches, and `PRIVACY.md` could no longer say we
keep no page text. **The first fetch never reports a change**, because different from nothing is
not a change.

**`robots.txt` is the permission model, and a disallowed page is not requested at all** -- the
test asserts the absence of the request rather than the presence of the refusal. A refusal is
stored as a sentence on the row, so the screen says why nothing arrives instead of showing a
watch that silently never updates; and it is a *different class* from a timeout, because
collapsing the two is how a site that said no gets asked again every day and how one bad
afternoon permanently stops a watch.

**Which sources we may poll is now a recorded human judgement rather than a guess.**
`watchlist.POLLABLE` was "every source except `pasted`" -- a guess about permission dressed as a
fact, which would have admitted any source added later on nobody's authority. `terms.py` holds
one entry per source saying what was read and when, **`UNKNOWN` is a refusal** (the opposite of
the deliverability report, and deliberately: there a person decides, here the next step is a
daily automated fetch for ever), and the check is made at the door *and* in the poll handler,
because a row written by hand would otherwise be polled for ever. The basis for each source is
shown on the screen, where the operator making that promise can read it.

**And the page watch ended up on the watchlist row rather than in a table of its own**, found
while wiring the producer: `signals.intake.record` takes a `WatchlistEntry`, because that is what
carries the pause, the domain and the lead match. A second table would have needed its own copy
of all three and produced two answers to "which companies do we watch" -- the signature defect,
in the slice whose own ADR is about not holding one fact twice.

18 unit tests and 6 integration, ruff and mypy strict clean, migration applied and reversed
against the development database.

**Your own Claude can read the four phases of work it could not see, and F6's scorer gets its
first caller (F6, F14, ADR-055, slice S16.1).** `list_job_posts`, `get_fit_score`,
`get_entity_edges` and `list_signals`, plus the press that produces a fit score at all.

**`fit.score_fit` had no caller anywhere.** F6 built the requirements extractor, the scorer and
the dataclasses, with tests for each, and nothing ever ran them against a posting -- a grep
returned the definition and one docstring mentioning it. So the feature existed, was tested, and
had never once produced a number. The thirteenth instance of this project's signature shape, and
again the cheapest kind to find.

**Computing is a press and reading is free, and they are different routes.** Extracting a
posting's requirements calls a model, so `POST /job-posts/{id}/fit` is a button and the `GET` and
the MCP tool read the stored row. A read that called a model would spend somebody's token budget
every time an agent looked or a screen refreshed -- the rule that keeps `GET /mailboxes` from
minting an OAuth token and a contact lookup from spending a credit on a page load. A test asserts
the read writes no `llm_usage` row.

**There is deliberately no `compute_fit_score` tool, and that absence is the design.** A tool an
agent can call that spends the user's model quota is exactly what F9 refuses (ADR-041), and a
catalogue is where that refusal is checkable rather than remembered. The test asserts the name is
absent along with the contact-lookup names.

**A score never travels without the match and the miss.** A number with nothing under it invites
whoever reads it to repeat it as a judgement about themselves; every match names the fact id and
the words that made it one, and the note says what the fraction is of. Only **confirmed** facts
are offered to the scorer, so ADR-037's rule holds one step before the comparison. An empty
profile is refused with a sentence rather than scored zero, because zero reads as a bad fit.

**And writing the first caller found a defect in the scorer itself.** `_terms` allows `.` inside a
token so `node.js` and `3.11` survive -- which meant **a word ending a sentence kept its full
stop**, so `postgres skills.` in a fact shared one term with `strong postgres skills` in a
requirement, and one term is below `MIN_OVERLAP`. The whole feature under-matched, silently,
and nothing noticed because nothing called it. A trailing dot is stripped and an internal one
kept, which is the rule `domain.wording.tokens` already applies.

**`MIN_OVERLAP = 2` means a one-word requirement never matches**, which is conservative on purpose
and is now asserted rather than assumed: a confident match on one common word is the worst kind of
wrong here, because what it is wrong about is the person's own credibility.

25 integration tests (16 for the tools, 9 for the producer), ruff and mypy strict clean, migration
applied and reversed against the development database. The MCP tests reuse
`test_mcp_endpoint.py`'s envelope helpers rather than rolling a second request builder: the
2026-07-28 revision mirrors body fields into headers and the server compares the two, so a second
implementation would be one more thing to keep honest.

**A connector token can now be given the part of the workspace it needs and no more (F24,
ADR-056, slice S16.2). Phase 16's buildable half is complete.** Four scopes, one vocabulary, and
tick boxes that each add exactly one thing.

**The first finding is a drift this slice had to fix before it could add anything.**
`ck_api_tokens_scopes` in `models.py` has said `scopes <@ ARRAY['mcp:read']` since the table was
created, while two migrations widened the live constraint to three values -- so **the model had
been stale for two slices.** A model that disagrees with the database is exactly what makes
autogenerate write a migration dropping something it cannot see, which has already happened once
here with an index. The vocabulary now lives once, in `models.API_TOKEN_SCOPES`, and the
constraint is generated from it; `protocol.SCOPES` and `api_tokens.ALL_SCOPES` both read it.

**The split is by what the holder may see, not by how much we trust them**, which is what makes a
tick box answerable at all. `mcp:read` is the workspace's own work. `mcp:profile` is the
*person's* confirmed facts -- a separate scope because it is a different kind of data, and the
holder of a token minted to help with outreach is often not the person those facts are about.
`mcp:notes` may record one note about a company. `mcp:propose` is unchanged.

**`required_scope` is still derived and a tool may only narrow it**, so a tool added later is
gated whether or not anybody gates it -- there is no permissive default to forget, and a test
asserts that an undeclared tool still gets the strict answer.

**The answer bank returns its questions and never its answers, and that exclusion is the whole
tool.** `answer_bank` holds expected pay, notice period and work authorisation: answers somebody
gives to a form *they* are filling in, on a page they opened. A model drafting a cover letter has
no use for the number and every opportunity to put it somewhere it should not be. Knowing which
questions are already handled is the useful half, and the screen says exactly this.

**A note is a graph edge, so the source is required by the database rather than by the handler.**
`ck_edges_provenance` refuses a row with no `source_url`, so a note an agent cannot point at
cannot be stored -- and this is the one path in the system where the thing offering a claim is a
model, which is where ADR-051 earns the most. It is deliberately **not** a research finding: a
hook in `domain_research` was verified against text we fetched and may be cited by the drafter,
while an edge is not in the evidence corpus at all, so a draft still cannot cite it.

**The acceptance test tests the mechanism rather than restating the data.** For each scope, mint a
token with only that scope and assert `tools/list` returns exactly the tools whose scope it is. A
hand-written map of names to scopes would be a second copy of the catalogue, and the first time
the two disagreed the test would be describing the wrong one. Both directions are closed: every
tool's scope is grantable, and every grantable scope has a tool -- a scope with no tool is a tick
box that does nothing, which is worse than no tick box, because somebody ticks it and believes
they granted something.

**Two of this project's own guards had to be restated, and one fired correctly on my code.**
`test_a_tool_that_writes_requires_the_propose_scope` asserted the exact derivation, which was a
proxy for the rule rather than the rule -- the same correction as "every tool is read-only", which
was a proxy for "nothing can send or approve" and had to break the moment anything wrote. **That
is the eighth test here found to be encoding something other than the rule it was named for.**
`test_the_scopes_are_read_and_propose_and_nothing_else` pinned the list, so it would fail whenever
a scope was added for a good reason while saying nothing about what matters; it now asserts no
scope names a send, an approval, a mode or an allowlist. And
`test_nothing_in_the_package_names_an_approved_status` fired on my own `"approved"` dict key,
which that guard reserves -- the field is `ready_to_use` now, because **renaming it was cheaper
than weakening the guard** and the guard owns the word.

**And CI found a third pinned list, which is the ninth instance.**
`test_the_scope_set_is_three_and_no_more` in the extension gateway's tests asserted the exact
tuple of three scopes -- a proxy that fails whenever a scope is added for a good reason while
saying nothing about the property that matters: that the extension's door is one scope, that it
is not an `mcp:` one, and that no scope exists which could open both. It asserts that now. Worth
recording that **two of the three were found by CI rather than by me**, because the integration
suite is unusable locally in bulk (DEV-1) and both lived there.

**The drift test earned its keep in the same run.** `ix_watchlist_page_due` was declared in
S15.5's migration and not in `models.py`, so `test_there_is_no_drift` failed with
`remove_index` -- which is the exact trap ADR-032 records, where autogenerate reads an
undeclared index as drift and writes a migration that DROPS it. It has happened once in this
repository already. Worth noting what that test does **not** catch: autogenerate does not
compare check constraints, which is why the `ck_api_tokens_scopes` staleness above survived two
slices with the drift test green the whole time.

30 integration tests and 25 structural, ruff and mypy strict clean, migration applied and reversed
against the development database. The downgrade revokes a token carrying one of the new scopes
rather than stripping it, as ADR-030's did: a credential whose permissions change under it while
it keeps working is worse than one that stops.

**The product has a front door, pages with addresses, and a brand (ADR-057, slice S17.1).**
Asked for directly: make it presentable, with colour and with pages rather than buttons, for
showing to investors.

**There were no addresses.** `view` was a `useState`, so every screen in the product was one URL
-- nothing could be linked to, bookmarked, opened in a second tab or reached with the back
button. There are thirteen pages now, behind a router written here in about a hundred lines with
no dependency: `frontend/wrangler.jsonc` already sets `not_found_handling:
"single-page-application"` and its comment says that is "what makes a refresh on /leads/123
work", so real paths were safe before any router existed. The nav items are anchors, because a
real `href` is what makes middle-click, copy-link and the back button work, and those three are
most of what "pages instead of buttons" means to whoever is using it.

**Colour in the chrome, calm in the content.** The way "add colour" goes wrong in an operational
tool is colouring the content: a red number here means a bounce and an amber one means a
deadline, so decoration in that space costs the semantic colours their meaning. The brand is in
the rail, the page headers, the funnel, the hero and the mark; a lead's row is untouched. And the
brand is deliberately **not** the semantic accent, because a brand that is the same colour as
"this is clickable" is not a brand -- which was the old rail's actual problem.

**Every claim on the landing page is checkable against the code**, and the ADR names where each
one lives. A landing page that overstates what the system does is the same defect as a draft that
invents a statistic, aimed at a different reader -- and this is the page the product gets pitched
from. The name is the deployment's own: `api.arkvane.in`, and "Arkvane" was already the agency
name in the live campaign's persona.

**Three defects, every one found by opening the page.** ADR-048's honest note -- that those
screens had never been rendered in a browser and the guards were a substitute for looking rather
than a proof -- was right again. The build was green, the frontend suite passed, all four
structural guards passed, and:

1. **The unset theme was a mixture of both.** `:root[data-theme='light']` matches only once
   somebody has *used* the toggle, so a visitor on a light system got the light base tokens and
   the **dark** brand tokens: a dark hero gradient under dark navy text. Invisible, no error.
2. **`align-items: center` on the hero** -- a centred grid item taller than its row overflows in
   *both* directions, so on a short window the headline was cut off above the top of the page.
3. **`<SignIn embedded />` was passed a prop that did not exist**, so the form brought its own
   80vh centred panel inside the card it was already in.

**And a fourth guard, for a class the other three could not see.** **Six `var(--x)` references
named tokens that do not exist** -- `--textMuted` against `--text-muted`, `--text-2` against
nothing -- across three files. `var()` with an unknown name and no fallback resolves to *nothing*:
the declaration is dropped and the element inherits, so the sign-in page's muted paragraphs and
the settings tick-boxes had been rendering in body colour. `index.css` states the rule that
drifted -- a component never uses a raw colour -- and the rule was followed; what drifted was the
*names*, which produces the same symptom and is much harder to see.
`test_dashboard_tokens_resolve.py` now asserts that every reference resolves, that both themes
define the same names, and that every light value also appears in the `prefers-color-scheme`
block -- which is the guard for defect 1, written after it and run against it to watch it name
`--grad-hero`.

**One `useLoad` where there were four**, each screen having grown its own copy and started to
differ. The shared one adds `asked`, meaning "we have heard back, including hearing an error",
which is the distinction that twice made this dashboard state something false while it waited and
which none of the four copies had.

**Verified in a browser rather than inferred from a green build**: both themes, the router, the
rail, the overview and the landing page.

1,399 unit tests, 5 of them the new token guard, ruff and mypy strict clean, no migration.

**Every screen now says which section it is in, and two surfaces stopped reading as unfinished
(slice S17.2).** The second half of the UI work, after ADR-057's frame.

**Twelve screens each rendered their own header** -- an `<h1 className="page-title">` and usually
a `<p className="page-note">` -- and they had drifted: some had the paragraph, some did not, none
said which section you were in, and the actions sat wherever each screen happened to put them.
**That is the difference between a set of screens and a product**: not any one of them being
wrong, but no two agreeing on where the title goes. They share `PageHeader` now, each with the
eyebrow naming its section and the same icon the rail uses for it -- which matters because
several titles are one word and one of them is whatever the operator called their campaign.

**And a self-inflicted one, fixed with a guard.** `.page-title` ended up defined in **both**
`app.css` and `chrome.css` -- the exact ambiguity `chrome.css`'s own header says that file exists
to avoid -- so which rule won depended on import order. `test_dashboard_tokens_resolve.py` now
fails if any class is defined at the top level of both files.

**Two small things that were quietly costing the most.** The empty-state icon was grey on grey,
which reads as a hole in the page rather than a place where something will appear -- and on a
first-run workspace an empty state is most of what somebody sees. And the review queue's progress
bar now carries the brand gradient: it is the bar a reviewer watches fill while working through a
queue, it is a progress indicator rather than a status, so it has no semantic colour to lose.

Verified in a browser, both themes. Six tests in the token guard, all four dashboard guards pass,
no migration.

**Connecting your own Claude no longer means pasting a token (F14, ADR-058, slice S16.3).** An
OAuth 2.1 authorization server of our own: discovery, a consent screen, PKCE, rotating refresh
tokens, and per-workspace clients.

**ADR-027 gated this on "when pasting tokens becomes the complaint, not before", and ADR-028
established why we cannot delegate it**: Supabase accepts the RFC 8707 `resource` parameter and
**ignores** it, issuing `aud: "authenticated"` whatever was asked for, so a token obtained for
any other service would be accepted here. That is the confused-deputy problem resource
indicators exist to prevent, and it left exactly two options: our own server, or no OAuth.

**An access token is a row in `api_tokens`, which is the decision everything rests on.** The tool
catalogue, the scope gate, the untrusted-text wrapper and the re-resolution of the membership on
every call are untouched -- OAuth is a different way to *obtain* the same credential rather than
a second kind of credential with parallel rules. A separate token table would be a second set of
rules to keep in step, and the first drift would be a credential outliving the membership behind
it. Two columns say which client holds it and which resource it is for.

**The refusals are the feature**, and they are in a pure module with an injected clock so each is
a test that needs no database: PKCE with S256 and `plain` refused in the validator *and* in a
check constraint; `resource` required, checked at authorize, again at exchange, stored on the
token and checked at `/mcp`; exact redirect matching; a single-use, sixty-second code; and a
refresh token that rotates, where **reusing one revokes the whole chain** -- a presented
already-exchanged token is a stale client or a stolen copy, and the safe answer to both is the
same.

**A test failure found a real multi-tenancy defect in my own design.** `oauth_clients` began with
**no `org_id`** -- a client is Claude's connector, which felt deployment-wide. What that costs is
that any owner could register a client with a plausible name and their own redirect URI, and
*another* workspace's user would be shown a consent screen saying "allow this". **A consent
screen is a phishing surface**, and the name on it has to come from somebody the person being
asked already trusts. Clients belong to a workspace now, which also keeps the table ordinary
under ADR-004 instead of a third exception to it.

**And an ordering bug the reuse test caught.** The route turns an `OAuthError` into a response
and rolls back -- right for every other refusal, since a refused request should leave nothing
behind. This one is the exception: **the refusal is itself a write**, and rolling it back
un-revoked the chain, so a stolen token kept working and the detection achieved nothing. The same
shape as the proposal that committed before its audit row (ADR-030): the ordering was the defect.

**The consent screen says only what our own rows say** -- the client's name read back by
`client_id`, the scopes described in the words the Settings tick-boxes already use. A screen
rendering a name the caller supplied is a phishing page with our domain in the address bar. It is
deliberately outside the shell: no rail, no campaign picker, nothing to click but Allow and
Cancel, because everything else is a thing that could be mistaken for the decision.

A hand-minted token keeps working unchanged, and a test says so -- that is the migration's real
risk. 24 unit tests and 35 integration, ruff and mypy strict clean, migration applied and
reversed against the development database.

**The system can notice what reviewers keep changing, and still cannot act on it alone (F16,
ADR-059, slice S16.4). The v3 plan is complete.**

`drafts` has created a new version on every edit since Phase 9, and `derived_from_id` has linked
each human rewrite to the model draft it came from -- ADR-026 added that column because version
numbers cannot tell an edit from a regeneration. **The preference data has been accumulating for
months and nothing had ever read it.**

**The describing is arithmetic and not a model, which is the whole decision.** A model asked
"what did the human change?" writes a fluent sentence that is sometimes about a change that did
not happen -- and the output of this feature goes *into a drafting prompt*, so a hallucinated
observation becomes a standing instruction and the system has learned something nobody did.
What `domain/edits.py` reports is computable from the two texts and checkable by reading them:
a removed sentence, a shorter subject, a body that lost more than fifteen per cent of its words.
It sees less than a person would, and a short list of things that are definitely true beats a
long list that is mostly true.

**Counted per draft, never per occurrence**: one reviewer deleting the same line three times
while editing one email is one opinion, and counting it three times would let a single session
invent a rule for everybody. **Three separate drafts** before anything is proposed -- once is a
Tuesday. That is not a significance threshold and is not presented as one; the ~1,050 sends per
arm in `DATASET_DESIGN.md` is about A/B outcomes, which is a different question.

**Nothing reaches a prompt until a person approves it, and that is the feature rather than a
precaution.** This is the only path by which the product's own behaviour changes on the strength
of what it observed. A loop that closed itself would be the system teaching itself from its own
output with nobody reading the lesson. The gate is a person rather than a threshold because no
threshold distinguishes "our reviewers prefer this" from "one reviewer was in a hurry on
Thursday". A dismissed lesson is **never proposed again** -- somebody looked and said no, and
asking every week trains them to stop reading.

**The screen shows the instruction before it is approved, not after**, because approving a
summary of a change is not the same as approving the instruction that summary becomes.

**A lesson is advice about how to write and never about what is true.** It goes through the same
prompt parameter as an A/B arm's instruction and is held to the same rule: every draft still
goes through the same `validate_draft` and the same claim check, so an approved lesson cannot
widen what may be asserted. A test asserts no generated instruction contains the words a factual
claim would need.

**And the producer ships with the table**, with a test that the handler actually passes the
lessons -- five things in this project have now been built and never called, and a lesson nobody
reads would have been the sixth.

14 unit tests and 11 integration, ruff and mypy strict clean, migration applied and reversed
against the development database.


**"Connect with Apollo" is a button where the provider offers one, and a sentence where it does
not (ADR-060).** Asked for directly: click, get redirected, come back connected.

**Three of the four answers were no, and each for a different reason.** Apollo *does* run an
authorization-code flow -- but only for approved partners, so the credentials come from their
developer portal after an application. Hunter has none at all. Snov's is `client_credentials`,
which has no redirect and no consent screen: you still paste two values, so it is a key wearing
OAuth's name. Google is the one that works that way, and it already does (ADR-018).

**So the catalogue declares the method and the screen asks it**, rather than the screen assuming
every provider is the same. Adding Apollo's redirect later is a changed `method` and an adapter
-- not a second screen and not a parallel concept to keep in step.

**A fake redirect was the tempting version and it is not built.** A button that opens the
provider's site and then shows a paste field looks like a connection and is a paste. That is
`List-Unsubscribe-Post` beside a bare mailto (ADR-033) -- announcing a capability that is not
there -- pointed at a user instead of a mail server, and worse, because somebody would read the
paste as the flow having failed.

**What is shipped is the part that actually wastes time, which is not the pasting.** Pasting a
key is ten seconds; finding the page of the provider's site that creates one is ten minutes of
settings menus. Every key provider carries a deep link straight to it, above the field, because
that is the order somebody does it in. Each card also says what the provider is *for* -- a list
of six names tells nobody which to connect -- what the free tier is, and, when there is no
button, why not, because "why can I connect Gmail in one click but not this?" is a reasonable
question whose unanswered version reads as the feature being unfinished.

`ALL_PROVIDERS` builds the database's check constraint and `CONNECTIONS` renders the screen, so
`test_connection_catalogue.py` asserts both directions: a provider in one and not the other is
either a card nobody can fill in or a key nobody can be told where to get.
`test_nothing_claims_one_click_that_does_not_have_it` is the one that keeps the claim honest, and
it will hold Apollo to having no key page the day its method changes.

**And one-click unsubscribe needed no implementation at all.** It was built in S11.4 and has been
waiting on one value; setting `PUBLIC_API_URL` turned it on. Verified live:
`https://api.arkvane.in/u/<token>` answers a page with a button for an unknown token exactly as
it does for a real one, which is the no-oracle rule holding in production.

8 new unit tests, ruff and mypy strict clean, frontend builds, 35 dashboard and catalogue guards
pass.


**The v3 plan is complete, so the question became what in the API has no screen -- and the
answer was two things that were inert rather than missing (ADR-061, slice S17.3).** Found by
diffing the served routes against the paths the dashboard calls: 102 served, 94 called, and of
the eight the only real gap was `/oauth/clients`.

**ADR-058's authorization server could not be used by anybody.** It shipped so that connecting
your own Claude would stop meaning *paste a token*; a flow needs a registered client; the route
existed and nothing called it. So the only way to set the flow up was a `curl` by somebody who
could already paste anything -- ADR-048's failure exactly, one slice after that ADR was written.
**Settings -> Connected applications** now does it, owner-only, beside the token panel it
completes. The stored redirect URIs are shown, because an exact-match rule cannot be debugged
without seeing what was actually saved: a wrong port or a trailing slash is the commonest way
one of these dies, and from the outside it reads as our bug.

**And a merged pull request still looked like a shipped one.** Cloudflare rebuilds the dashboard
on every merge; the API is deployed by hand. This file records the consequence five times, most
recently for four slices in a row -- the Signals screen answered **Not Found** because the live
backend was an older image, and `/watchlist` 404ing cannot say *this API is older than this
screen*, so the investigation started at DNS, CORS and connectivity each time. This file's own
note named the fix as the next thing worth building.

`GET /version` answers `commit`, `built_at` and `started_at` and nothing else -- an inventory of
what is installed is a list of which advisories to try. `started_at` answers what the commit
cannot: *did the deploy restart anything?* A rollback that quietly left the old container running
reports an uptime in days beside a commit that looks current.

**Baked into the image rather than set on the container**, because the identity must travel with
the artifact: a restart passing a different value would have the deployment confidently report a
build it is not running, which is worse than reporting none. Verified end to end rather than
inferred -- `docker build --build-arg` then `docker run`, and `Settings` inside the container
reads both values back. An image built *before* this change has no such field at all, which is
also why the banner will stay silent against the currently-deployed API until it is redeployed.

**Silence is the default and most of the design.** Either side unknown means no claim: a
development run bakes nothing and `local` is the Compose default, so without that a developer
would see the banner on every page load for ever -- and then nobody reads it on the day it is
true. The same reasoning as alerting only after three consecutive worker failures. And **"older"
is claimed only when both build times parse and the API's is earlier**: two hashes are different
and never older, and the times come from two machines' clocks, so the banner shows both rather
than only the conclusion. *The API is behind*, asserted wrongly, sends somebody to redeploy
something that was already current.

The comparison is a pure function in its own module with eight tests, not a condition inside a
component -- the dashboard's suite renders nothing, which is exactly how a button once shipped
throwing `ReferenceError`.

**`test_dashboard_calls_real_routes` caught my first version on its first run.** `/version` is
the one route the dashboard calls that lives *above* `/api/v1`, and `api.js` prefixes every
path -- so my `useLoad("/version")` would have asked for `/api/v1/version`, which does not
exist. It has its own client function now rather than a `path` argument that would let any
screen escape the prefix.

**And `PUBLIC_PATHS` was read by nothing.** Found while adding `/version` to it. What makes a
route public here is declaring no identity dependency; the frozenset was a comment with
parentheses -- so a route that forgot its auth would not appear in it, and a route named in it
could grow an auth dependency with the list still claiming otherwise. The signature defect, in
the one place where being wrong is a security question.

`tests/unit/test_public_paths.py` compares the two in both directions, and writing it produced
three findings of its own:

1. **A single-level scan finds zero routes.** This FastAPI keeps `_IncludedRouter` wrappers in
   `app.routes` rather than flattening them, so `isinstance(route, APIRoute)` matches nothing --
   a guard that scans nothing, passing trivially while reading as evidence. It walks the
   wrappers and rebuilds each path from the include prefix, and states its own preconditions.
2. **Keying by path alone was wrong, and the guard found it.** `/mcp` also serves `GET` and
   `DELETE` stubs that answer 405 by design, and a 405 needs no credential to be correct. Keyed
   by method and path now -- which is also the only way to express the unsubscribe endpoint's
   deliberate split, where `GET` renders a button and `POST` acts.
3. **Four routes authenticate in the handler body**, not through a dependency: `/mcp` and the
   three extension endpoints. That is ADR-028's two-doors rule -- each door must refuse the
   wrong *kind* of key, which means reading the header itself where a shared `Depends` would
   accept every credential the other doors accept. Exempting them is only honest if something
   checks they do the work, so a second test parses each handler with `ast` and asserts it
   resolves a bearer. Parsed rather than grepped, because the comment explaining each door names
   the very call being asserted.

Run against `/api/v1/me` with its `CurrentUserDep` removed, it names `GET /api/v1/me`.

**And the five routers reading the `config` singleton are fixed too (ADR-061 addendum).**
ADR-041 found that gap and named it rather than closing it: a router reaching for the
module-level singleton means an app constructed with its own `Settings` -- which is every test
app -- is partially ignoring them. In production the two are the same object, which is exactly
why it survived four phases.

Both symptoms are the silent kind. **`unsubscribe` signs and verifies a public URL** with a key
derived from `CREDENTIAL_KEY` while the send loop builds that URL from its own object -- and
`POST /u/{token}` answers 200 after doing nothing for *every* refusal, deliberately, so the
endpoint is not an oracle (ADR-033). That correct decision is what makes a wrong key
indistinguishable from a working one: the recipient sees a success, nothing is logged, and the
suppression is never written. An opt-out we silently refuse is one we will breach. And
**`operations` called `effective_send_mode(config, override)`**, so the route that answers "what
happens if I approve this draft?" resolved from a different object than the gate -- hard rule 1
says the mode is decided in exactly one function, it is, and the screen was asking it about a
different deployment. The bare `config` argument survived a `config.` search, which is why it
took reading the call rather than grepping for it.

`connect._back` read the dashboard URL from the singleton while being called only from a route
that now has settings injected; it takes the URL as an argument, because a helper and its only
caller looking at two different configuration objects is the same defect one frame down.

Two legitimate readers remain and the rule is about *where*: a worker has no request to inject
from and reads the process configuration at start, and `app.py` builds the application. Neither
is a router, so the guard is scoped to `app/api/routers/` and asserts both directions -- no
router imports the singleton, and enough routers do take the dependency, because deleting every
`SettingsDep` in the tree would otherwise satisfy the first on its own.

14 new tests (8 on the comparison in the frontend suite, 6 integration) plus 7 structural,
ruff and mypy strict clean, frontend builds, 22 frontend tests pass, no migration.


**A Setup page, and four defects found by setting the product up in a browser (ADR-062).**
`/setup` lists what a workspace still has to connect, in order, each row linking to the screen
that does it. It re-derives nothing: every state is a field the owning route already computes,
and a step whose request has not come back is `unknown`, never to-do.

The defects, all found by using the live site rather than by a test:

1. **An Apollo key could not have worked.** The adapter called `mixed_people/search`; Apollo's
   own key form lists only `mixed_people/api_search`, and says people search is excluded from
   the Free plan even with a master key. The endpoint is corrected and the card now says the
   plan limit before anybody makes a key. The response shape is still unverified.
2. **Every Apollo address was called verified** because it was present. It is verified only on
   Apollo's own `email_status` now; anything else is a guess.
3. **"Connect null".** The provider card's heading fell back to the provider id when the API
   sent no label; the button, the key link and the hint did not. One name now.
4. The browser tab said "Funding Outreach Automation" on a product called Arkvane, and the
   Apollo key link pointed at an address that only redirects.

**Still open, deliberately not in this change:** `deps.current_user` resolves a person with two
memberships to whichever row the database returns first (no ordering, no unique index on
`user_id`). ADR-022 describes it; nothing prevents it. It changes how sign-in resolves a
workspace, so it wants its own reviewed PR.
