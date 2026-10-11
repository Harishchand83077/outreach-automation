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
