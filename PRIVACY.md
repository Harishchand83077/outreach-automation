# Privacy and data handling

What this system holds about people, who else sees it, how long it keeps it, and how to make it
forget. Written to be checkable against the code rather than reassuring: every claim below names
the file that implements it, so a claim that stops being true should fail a test or show up in a
diff.

This describes the software. The organisation running it is the data controller and owns the
decisions — lawful basis, notices, and answering requests. This document is not legal advice.

---

## 1. Whose data, and what

**Leads** are business contacts at companies: a name, a work email address, a company, a job
title, a LinkedIn URL, and a website. They are imported from a spreadsheet the operator supplies.

Alongside each lead the system stores what it did and found:

| Kind | What it is | Where |
|---|---|---|
| Research | Facts found on the company's own public pages, with the URL each came from | `research`, `domain_research` |
| Drafts | Generated email subject and body, and the claims they make | `drafts` |
| Messages | What was actually sent, and every reply received, in full | `messages` |
| Classification | A label for each reply (interested, not interested, opt-out…) and why | `classifications` |
| Events | An append-only audit trail of every state change and who caused it | `events` |

**`linkedin_url` is stored and displayed, never fetched.** Scraping LinkedIn is against their
terms, and the field exists so a human can click it.

**Users** of the dashboard are a separate matter: authentication is handled by Supabase, and this
system stores only the opaque user id, an email address and a role, in `members`.

---

## 2. Who else sees it

| Processor | What reaches them | Why |
|---|---|---|
| **LLM providers** (Groq, Cerebras, OpenRouter, Google AI Studio) | See the PII rule below | Writing drafts, classifying replies |
| **Google Workspace** | Everything in an email, as any mail provider sees | Sending and receiving |
| **Supabase** | Dashboard users' email addresses — never lead data | Authentication only |
| **AWS** | The database and application, as any host holds them | Hosting |
| **Cloudflare** | The dashboard's static files only | Serving the dashboard |
| **Tavily** (optional) | A search query, which may contain a company name | Finding a company's site |

**The PII rule, and its limits.** Each provider in the chain is marked `allow_pii` true or false.
An entry marked false — Google AI Studio, because its free tier may train on prompts outside the
EEA and UK — receives **public page text only**: never a lead's name, address, or the content of
a reply. Enforced in `app/services/llm/router.py` and tested in `tests/unit/test_llm_router.py`.

Providers marked `allow_pii` true do receive a lead's name and company, and reply text when
classifying. That is the trade for drafting anything specific. If it is not acceptable, remove
those entries from `LLM_CHAIN`; the system degrades to weaker drafts rather than failing.

**No analytics, no tracking pixels, no link rewriting.** Outbound mail is plain text. The system
cannot tell whether a message was opened, by design — open tracking means putting a beacon in
somebody's inbox without asking.

---

## 3. How long

| Data | Kept | Set by |
|---|---|---|
| Leads in a terminal state | **12 months**, then erased in full | `LEAD_RETENTION_MONTHS` |
| Page text behind a research finding | **30 days**, then cleared; the finding and its sources stay | `RESEARCH_TEXT_RETENTION_DAYS` |
| Leads still in conversation | Until they reach a terminal state | — |
| Suppressions | Indefinitely, and deliberately | §4 |
| Database backups | 14 days on the VM, plus a weekly off-box copy | `deploy/backup.sh` |

The sweep runs hourly inside the worker (`app/services/privacy/retention.py`) and goes through
exactly the same code path as a deletion request.

**Leads still in conversation are never aged out.** Retention removes finished work; somebody
mid-thread is not finished, and dropping them because a clock expired would be worse service, not
better privacy.

---

## 4. Erasure, and the one thing it cannot undo

`DELETE /api/v1/leads/{id}` erases a lead: the row, its drafts, its messages, its research and
its events. Restricted to `admin` and above, because it is the only irreversible action in the
API. Implemented in `app/services/privacy/erasure.py`.

**What survives, and why.** A **hashed** form of the address stays on the suppression list.

This is the one decision here worth arguing with, so here is the reasoning. A lead who opted out
and then asked to be erased presents a conflict: honouring the erasure literally means deleting
the record of their opt-out, and the next import of the same spreadsheet would mail them again.
That turns a privacy request into the exact harm it was meant to prevent. So the address is kept
as a SHA-256 digest: enough to recognise it if it is offered again, not enough to read it out of
the database.

**A hash is pseudonymisation, not anonymisation.** An email address has little entropy, so anyone
holding both this table and a candidate address can confirm a match by hashing it. That is
unavoidable in any scheme that must recognise the address again, and recognising it again is the
point. What it does buy: a copy of the database does not disclose who was contacted.

Erasure with `?keep_suppressed=false` leaves nothing at all. That is for a lead imported in error
and never contacted — no opt-out to honour, so no reason to retain even a digest.

**Backups are the honest exception.** Erasure removes somebody from the live database, not from
dumps already taken. Those expire on their own schedule — 14 days on the VM. Anyone promising
immediate erasure from backups is describing a system that does not take backups.

---

## 5. Opting out

Every message carries a `List-Unsubscribe` header and a plain-text opt-out line. Replies are
scanned for opt-out language, including the Hinglish forms common in the audience this is aimed
at (`app/domain/reply.py`). An opt-out is honoured by adding the address to the suppression list
in the same transaction that records the reply, and suppression is re-checked immediately before
every send — so an opt-out that arrives between approval and sending still stops the message.

`OPTED_OUT` and `BOUNCED` are terminal states a human cannot reopen.

---

## 6. Security

- **The database publishes no network port.** It is reachable only from the other containers.
- **TLS everywhere**, with certificates obtained and renewed automatically.
- **Containers run as a non-root user**; both the api and the worker parse input chosen by
  somebody else.
- **Secrets live in `.env`** on the server, never in the repository, and are redacted from log
  lines as a second defence (`app/core/logging.py`).
- **Tenancy is enforced on every query** by an `OrgContext` the request cannot influence: the
  organisation comes from the caller's membership row, never from a parameter they send.
- **An unknown id answers 404, not 403**, everywhere — confirming an id exists in somebody else's
  organisation is itself a disclosure.

---

## 7. Sending restraint, because it is a privacy matter too

- 100 emails per day per mailbox, counting every outbound message.
- One follow-up, three days later. Not a sequence of five.
- Weekdays only, 10:00–17:30 IST, with jittered spacing.
- Every first email and every reply is read by a human before it goes.
- `MEETING_BOOKED` is only ever set by a human or a booking webhook, never inferred.

---

## 8. Known gaps, stated rather than hidden

- **No postal address in the footer** (`QUESTIONS.md` Q2). The owner has chosen to proceed
  without one for now. CAN-SPAM binds US recipients and the audience is India-only, which is why
  this is tolerable meanwhile; it becomes a real exposure the moment anyone outside India is
  mailed.
- **No automated data-subject request intake.** Erasure is an API call an operator makes, not a
  form a recipient can submit.
- **Research is limited to a company's own public pages** and, optionally, a search result. The
  system does not build profiles of individuals from social media.

---

## 9. Contact

Data or security concerns: open an issue in the repository, or write to the address in the footer
of any message this system sent you. An opt-out reply to that message is honoured automatically
and does not need a human to act on it.

## The referral finder (F10, ADR-042)

A user may upload their own LinkedIn connections export so the system can say who they already
know at a company. That file is a few thousand people who agreed to connect with **them** and
have never heard of us, so three things are stated plainly.

**The `Email Address` column is discarded before anything is written.** LinkedIn includes it for
connections who allow it. Keeping it would make this a contact database rather than a referral
finder, and it is the one dataset here that a later owner could pool or sell. The parser drops
it, the `connections` table has no column for it, and a test uploads a file containing an address
and checks the stored row.

**Nothing is fetched.** Not the profiles, not the company pages, nothing. The package imports no
HTTP client at all and no LinkedIn hostname appears anywhere in it, both asserted over the source.
A profile URL is stored and shown so the user can open it themselves -- the same rule
`linkedin_url` has followed since the first migration (hard rule 4).

**Nothing is sent.** The suggested note is text the user edits and sends themselves on LinkedIn.
It is built by substitution from stored values, with no model involved, so it cannot invent a
shared project in a message to somebody who would know it had not happened.

What is stored per connection: their name, their public profile URL, the company and job title
they wrote on their own profile, and when they connected. `DELETE /connections` removes all of it
for real -- unlike a suppression, forgetting a connection cannot cause anybody to be contacted,
so there is nothing to keep a tombstone for.

## Contact lookups (F9, ADR-041)

A workspace may connect its own Apollo, Hunter or Snov key and press a button to ask who hires at
a company. What comes back is personal data about people who have not heard of us, arriving from a
third party, so three things are stated plainly.

**It is the workspace's own key and its own request.** There is no shared key and no deployment
credential. Nothing in the background runs a lookup -- no job kind, no scheduler pass -- so a
lookup exists only because somebody pressed a button, and `events` records each one.

**We keep the fields we chose, not the provider's payload.** A name, a job title, an address with
whether the provider calls it verified or a pattern it inferred, a confidence number, and a public
profile URL. **The raw response is not stored**, although it would be the fastest way to debug a
shape change. A `profile_url` is kept for a person to open themselves and is **never fetched** --
hard rule 4 covers LinkedIn and nothing here fetches any of them.

**A proposal is not a lead and cannot be mailed.** It sits in `contact_candidates` until somebody
imports it, which goes through the ordinary import path -- including the suppression check, so
somebody who opted out cannot be re-added by a provider that has no idea they did. Dismissing a
proposal keeps the row, so the same person is not proposed again next week; that is a record kept
in order to respect a decision, which is the same bargain as the hashed suppressions above.

The provider key itself is Fernet-encrypted in `provider_credentials`, under the same key as
mailbox credentials. No route returns it, no log line holds it, `__repr__` does not print it, and
it travels to the provider in a header rather than in a URL -- a credential in a query string is a
credential in an access log.

## The Chrome extension (F8, ADR-039)

The extension sends two things, and only when the user presses a button:

- **Form help:** the visible field *labels* and the page URL. **Not the values the user has
  typed**, and not the rest of the page. Only labels are needed to match a stored answer, and the
  rest of an application page is the user's own data and somebody else's copy, so a form capture
  stores no page text at all -- which a test asserts.
- **Capture:** the visible text of a job posting, when the user asks for that posting to be
  stored.

It never reads another tab, never watches a page in the background, and never submits a form or
uploads a file.

**That is enforced by the package rather than promised by it**, and the distinction is the whole
point of this section. The extension declares **no content script**: the code that reads a page
is injected by the side panel when the button is pressed, under `activeTab`, which Chrome grants
for one tab because the user clicked this extension's own button. There is therefore no code
path by which it could read a page nobody asked about. It also ships with **no host
permissions** — the address of the user's own API is requested at the moment they save it, so
the install prompt covers nothing and the grant names one host.

Four APIs are asserted to be absent from the package's source, with comments stripped first so
the prose explaining the rule cannot satisfy it: form submission, assignment into a page's
field, file upload, and any read of a field's *contents* (including `.checked`). The panel also
has **no channel back into the page** — nothing in the package sends a message to the content
script and the content script registers no listener — so even a panel that wanted to fill the
form has nothing to ask. `extension/tests/safety.test.ts` and
`tests/unit/test_extension_package.py` check this, deliberately twice: the second runs in the
suite that gates every change to the repository.

A page the extension cannot fully read is reported as **incomplete** rather than as a short
complete list, and a question with no approved answer comes back blank and marked as needing the
user. **No model is reachable from this path at all.**

The token is stored in `chrome.storage.local` and never `chrome.storage.sync`, which would push
a bearer credential through the user's Google account to every browser they sign into.

`captures` rows are kept for **days, not months**, and are removed by the retention sweep.
`suggestions` records what the person did with each proposal -- accepted, edited or rejected --
which is the only reason to keep one after it has been shown.

The extension's token carries the scope `ext:suggest` and nothing else. It **cannot** call the
dashboard API and **cannot** call the Claude connector, and neither an `mcp:` token nor a
dashboard sign-in can call the extension gateway. Three credentials, three doors, one key each.

## Tailored resumes and the application tracker (F20, ADR-043)

Everything here is the user's **own** data about themselves: the facts they recorded, the resume
versions assembled from them, and the postings they are pursuing. No third party's personal data
enters this feature.

**A resume version is immutable, which is a retention statement as much as an audit one.** An
`applications` row points at the exact document that was sent, and the row cannot be edited -- a
change makes a new version. Erasing one is still possible: the trigger lets a DELETE through when
the session sets `app.allow_resume_version_delete`, so a data-subject request can be honoured
while an accident cannot. The same bargain `app.events` already makes, and `DELETE /leads/{id}`'s
erasure path does not touch these rows, because a resume is not a lead's data.

**A posting's text goes to a model; the facts go only to a provider cleared for personal data.**
Tailoring sends the candidate's confirmed facts, which are their career, so that call is marked
as containing personal data and the LLM router will not route it to an entry with
`allow_pii=false`. The posting itself is public text about a role and is wrapped in `<untrusted>`
delimiters, because somebody else wrote it (hard rule 7).

**The PDF is stored in Postgres beside the version and nowhere else.** It is covered by the
nightly dump; it is not uploaded to any object store or third party.

**Only the user records an application as sent.** The routes are on the dashboard sign-in, and the
browser extension's `ext:suggest` token cannot reach them (ADR-039). A tracker whose dates were
written by something other than the person is a record of claims they did not make.

## The booking webhook (F15, ADR-045)

A workspace may connect its own booking tool so that a meeting somebody books is recorded without
an operator having to notice the calendar invite and click a button.

**What arrives and what is kept.** The booking tool's payload carries the provider's own ids, the
invitee's answers to the booking form and often the organiser's calendar. **Only four things are
stored**: the provider's booking id, the invitee's email address and name, and the start time. The
payload is not retained -- a column nobody reads is a column nobody is protecting.

**The address is the point of it.** It is what ties the booking to a lead, and without it the
meeting cannot be attributed to the campaign that produced it. It is the same address that is
already in `leads`, from the same person, for the same purpose.

**A booking from somebody who is in no campaign is kept and shown.** Usually they replied from a
second address or somebody forwarded the link. Dropping it would hide a meeting that happened;
keeping it means holding one address for somebody who booked time with the workspace's owner,
which is why `DELETE /bookings` is not needed -- the whole table goes with the workspace.

**Erasing a lead clears the booking's address.** The row survives, because erasing somebody must
not erase the record that a meeting happened -- that is a business event and the funnel it feeds
has to stay auditable -- but the invitee's name and address are cleared. Without that, erasure
would be decorative for exactly the leads who got furthest.

**No lead is ever created by a booking.** Somebody who books a meeting has asked to talk, not to
be added to a sending pipeline.

**The secret, and what the URL is.** The webhook URL contains a signed token identifying the
workspace; it is not secret and is pasted into a third party's settings page. The secret that
signs each delivery's body is generated here, shown once, and stored encrypted with the same
Fernet key as mailbox credentials. A leaked URL alone cannot record a booking.
