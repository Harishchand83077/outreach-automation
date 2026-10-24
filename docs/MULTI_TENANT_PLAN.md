# Turning this into something ten people can sign up for

Written 2026-10-25. The ask: anyone can sign up, connect **their own** Gmail without typing an
app password, run their own campaigns, and never see anybody else's data. Under ten users, on
free tiers.

This document records the decisions and why. `DECISIONS.md` ADR-017 and ADR-018 carry the two
that deviate from the target architecture.

---

## 1. The good news: tenancy is already built

ADR-004 put `org_id` on **every** table from the first migration, and every repository call takes
an `OrgContext`. `app/api/deps.py` resolves the org from the `members` row and **never** from the
request — not from a path, a query string, a body or a header. 17 of 18 tables carry `org_id`;
the 18th is `orgs` itself.

So "every user's campaigns should be separate" is not a thing to build. It is a thing to
**turn on**. The work is in the two places that are still single-tenant:

| Thing | Today | Needs to become |
|---|---|---|
| Who may sign in | one `members` row, inserted by hand with SQL | self-serve: first sign-in provisions an org |
| Which mailbox sends | `SMTP_EMAIL`/`SMTP_PASSWORD` from the **environment**, one per deployment | per-org credentials, held in the database |

Everything else — campaigns, leads, drafts, research, suppressions, the review queue, the send
loop's candidate query — is already scoped and already tested for it.

---

## 2. One org per user, and that user owns it

**Decision.** A new sign-up creates a **new org** with that person as its **owner**.

The alternative — everybody joins one shared org as `member` — was rejected. "Give them user
access only" is about not letting strangers near *each other's* data, and separate orgs give that
completely, through the mechanism that is already load-bearing and already tested. Roles inside
an org answer a different question: who on *your own team* may change *your own* sending rules.

A consequence worth stating: an owner can change their own send window, upload their own
attachment and delete their own campaigns. They cannot see that any other org exists. The things
that must stay out of their hands are **global**, and they already are — `SEND_MODE` comes from
the environment and is checked in one function; raising it to `live` is refused outside
production and has no route that can set it for another org.

**Invites come later.** One person, one org covers ten users. `members` already has the shape for
more, so adding a second person to an org is a row, not a redesign.

---

## 3. Signing up is gated by a setting, and the default is closed

**Decision.** `SIGNUP_MODE` = `closed` (default) | `allowlist` | `open`, with
`SIGNUP_ALLOWED_DOMAINS` for the middle one.

**The default has to be `closed`**, for the same reason `orgs.send_mode` was added as a nullable
column: deploying this change must alter nobody's behaviour until somebody deliberately turns it
on. A migration that quietly opened public sign-up on a system that sends email would be the
worst possible default.

**And `open` deserves a clear head about what it means.** Anybody on the internet could then
attach their own Gmail and send cold email through software you host. They would be sending as
*themselves*, with their own quota and their own reputation at stake — this is not an open relay,
and nothing lets one user send as another. But the abuse case is real enough that `open` is a
decision rather than a default, and `allowlist` by email domain is what most small teams actually
want.

---

## 4. Connecting a mailbox: OAuth, not an app password

**Decision.** "Connect Gmail" runs a Google OAuth consent flow. We store a **refresh token**,
encrypted. The user never types a password into our software and can revoke us from their Google
account page at any time.

### Why not the Gmail API

Because we do not need it. Scope `https://mail.google.com/` authorises **SMTP and IMAP over
XOAUTH2**, which means every line of the existing send loop, outbox, IMAP poller, reply matcher
and ADR-002 recovery pass keeps working unchanged. Switching to the Gmail API would mean
rewriting all of that to get the same result — and the Gmail API cannot do one thing our IMAP
path does, which is read the Sent folder to resolve a send whose outcome we never learned.

What changes is one function: how the SMTP and IMAP clients authenticate.

### The scope problem, stated honestly

Google sorts scopes into tiers, and the tier decides what it costs to publish:

| Scope | Tier | What publishing costs |
|---|---|---|
| `gmail.send` | **sensitive** | brand verification — free, days |
| `gmail.readonly`, `gmail.modify`, `https://mail.google.com/` | **restricted** | verification **plus an annual third-party security assessment (CASA)** — thousands of dollars |

**Reading a mailbox is restricted. Sending is not.** So an app that detects replies cannot be
published for free, and this project's hard rule 5 says not to spend money.

The way out, for ten users, is that **an unpublished app does not need any of it**: an OAuth
client in **Testing** status may have up to **100 test users** and request restricted scopes
freely. The price is that its refresh tokens **expire after 7 days**.

**Decision: Testing status, both scopes, and treat the 7-day expiry as a product feature rather
than a bug to hide.** When a token expires the system does not fail silently — it pauses that
org's mailbox with a reason, raises an alert, and the dashboard shows "Reconnect Gmail" where the
campaign status would be. A weekly click is acceptable for ten people. Silence would not be.

### The path past ten users, so the choice is not a trap

When this needs more than ~100 users, or weekly reconnection becomes intolerable, there are two
honest options and neither is a rewrite:

1. **Pay for CASA**, publish with restricted scopes, tokens stop expiring. Costs money. Not now.
2. **Drop to `gmail.send` only** (sensitive, free to verify, no expiry) and detect replies by
   routing them to a mailbox *we* own: `Reply-To: r+<token>@replies.arkvane.in`, with the reply
   forwarded on to the user so it still reaches them. Costs one DNS record and some routing code,
   and gives up reading the user's Sent folder — so ADR-002's recovery pass would need the
   `Message-ID` match to happen on our side instead.

Option 2 is the one to take, and the design is written down here so that choosing it later is a
day's work rather than a discovery.

### Storing the token

A new table, `mail_credentials`, one row per mailbox:

- the refresh token **encrypted with Fernet** (`cryptography`), key from `CREDENTIAL_KEY` in the
  environment — so a database dump, which is taken nightly and shipped to S3, does not contain
  usable Gmail credentials;
- never returned by any route, never logged, never in an event payload. The API answers
  *whether* a mailbox is connected, as what address, and when the token was last refreshed;
- its own table rather than columns on `mailboxes`, because `mailboxes` is read on every send-loop
  cycle and secrets should not be dragged through a query that does not need them.

`kind` distinguishes `google_oauth` from `app_password`, so the existing environment-credential
path stays valid and the current deployment keeps sending while all of this lands.

---

## 5. What the send loop does differently

Almost nothing, which is the point.

`SmtpSender` today reads `settings.SMTP_EMAIL` and `settings.SMTP_PASSWORD`. It gains a
resolution step: **if the mailbox has a credential row, use it; otherwise fall back to the
environment.** The fallback is what keeps the live deployment working and keeps every existing
test meaningful.

**The send gate does not move.** `effective_send_mode()` and `allowlist_permits()` stay exactly
where they are and keep deciding for every send, whoever it belongs to — hard rule 1, and the
structural test over the whole tree still asserts that nothing else decides.

One new refusal joins the existing list: a mailbox whose token could not be refreshed is paused
with a reason, which surfaces through `blockers` like every other reason a campaign is not
sending.

---

## 6. Capacity, since ten people will not fit the current numbers

Measured per-row costs (PROGRESS.md): ~34 KB per lead, permanently.

| | 1 user | 10 users |
|---|---|---|
| Leads/month at 100 emails/day | ~1,100 | ~11,000 |
| Storage/month | ~37 MB | ~370 MB |
| Six months | ~250 MB | ~2.2 GB |

Against 23 GB free on the instance, ten users for six months is fine. Two things do **not** scale
and are named now rather than discovered later:

- **The free LLM quota is shared.** `llm_usage` already accounts per org, but the *quota* belongs
  to the API key, which is one key for the deployment. Ten research-mode campaigns drafting at
  once will exhaust a free tier that comfortably serves one. **Template mode calls no model at
  all**, which is why it matters more than it looked: it is the mode that scales to ten users for
  free. Research mode needs per-org API keys before it serves ten people, and the `llm_usage`
  table is already the place that would meter it.
- **One worker process.** Five asyncio tasks, one `t3.small`. The send loop sends one message per
  cycle per mailbox, so ten mailboxes interleave rather than queue — but ten orgs' research jobs
  compete for the same `JobRunner` concurrency caps. Fine at this size; the fix when it is not is
  more worker processes, which the `FOR UPDATE SKIP LOCKED` queue already supports with no code
  change at all.

---

## 7. Order of work

1. **Why a lead is not sending** — surface the send loop's own refusal per lead. *(The thing that
   made "why did no follow-up go out?" take a week to answer.)*
2. **Campaign scheduling** — `starts_at` / `ends_at`, so a campaign can be set up today and begin
   on Monday.
3. **Self-serve sign-up** — `SIGNUP_MODE`, org provisioning on first sign-in, default closed.
4. **`mail_credentials`** — the table, Fernet encryption, and the send/IMAP paths resolving
   per-org credentials with an environment fallback.
5. **The OAuth flow** — authorise, callback, refresh, revoke, and the reconnect prompt.
6. **Google Postmaster Tools** — not code: a DNS record and a sign-in, so deliverability stops
   being guesswork.

---

## 8. What only the owner can do

I cannot create these, and must not try: they need a Google account signed in, and I never enter
credentials.

- **A Google Cloud project with an OAuth client.** Gives the `GOOGLE_CLIENT_ID` and
  `GOOGLE_CLIENT_SECRET` that step 5 needs. Consent screen in **Testing**, scope
  `https://mail.google.com/`, every user added as a test user.
- **Google Postmaster Tools.** A TXT record at GoDaddy and a sign-in. Until this exists, nothing
  tells you your spam-complaint rate or whether your authentication is passing in the wild — and
  this project already had one reply land in Spam.

Exact steps for both are in `docs/OWNER_SETUP.md`.
