# Contact providers (F9)

Three providers, each called on **the workspace's own key**, only when somebody presses a button.
There is no shared key and no deployment credential; a workspace with no key gets a refusal that
says so.

## What is verified and what is not

`docs/ATS_FEEDS.md` records the three ATS feed shapes as **live-tested** — those endpoints are
public and were called, and the one surprise (Lever returns a bare array) is in that file because
of it.

**None of these three providers can be called without a paid account**, and hard rule 5 forbids
buying one without a decision from the owner. So the table below says, per provider, exactly what
these adapters rest on. This matters more than it looks: a wrong assumption here produces either a
refusal somebody can read, or — if the code were written to shrug at a shape it does not
recognise — *zero contacts*, which is indistinguishable from "nobody works there". That is why
`ProviderUnreadable` exists and why a missing field raises instead of returning an empty list.

| | Hunter | Apollo | Snov |
|---|---|---|---|
| Endpoint | `GET api.hunter.io/v2/domain-search` | `POST api.apollo.io/api/v1/mixed_people/search` | `POST /v1/oauth/access_token`, then `GET /v2/domain-emails-with-info` |
| Endpoint confidence | **Documented**, confirmed by Hunter's own help centre | **Disputed** — community sources give both `/mixed_people/search` and `/mixed_people/api_search`; Apollo's own reference was not reached | **Disputed** — Snov also documents an asynchronous `start` / `result/{task_hash}` flow, and the sources disagree about which is current |
| Auth | Key. Docs show `api_key` as a **query parameter**; we send `X-API-KEY` | Key in `X-Api-Key`. One source says query parameters stopped being accepted in September 2024 | Client id + secret exchanged for a one-hour Bearer token |
| Credential kind | `api_key` | `api_key` | `oauth` (the user still copies both values) |
| Addresses returned | Yes, with a `confidence` and sometimes a verification status | **No** on most plans — revealing one is a separate enrichment call that spends a credit | Yes, with a status |
| Rate limit noted | 15 requests/second on domain search | not established | not established |
| Cost noted | 1 search credit per 1–10 emails per domain | search is credit-free; enrichment is not | API on premium plans only |

**Each provider's response is read in exactly one function**, so a shape that turns out to be
wrong is one function to correct and one test to update. That is the whole reason the adapters are
this thin.

## Two decisions that are about safety rather than shape

**The key travels in a header, never in the URL.** Hunter's documentation shows `api_key` as a
query parameter. A credential in a URL is a credential in an access log, a proxy log, a browser
history and an error report. `test_the_key_travels_in_a_header_and_not_in_the_url` asserts the key
does not appear in the request URL at all. If a deployment ever finds Hunter refuses the header,
the fix belongs in that adapter — not in a decision to put the key in a query string.

**Apollo's locked placeholder becomes no address at all.** Apollo returns the literal
`email_not_unlocked@domain.com` for a contact whose address needs an enrichment credit. Stored as
`NULL`: a placeholder sitting in an email column is an address something will eventually try to
mail, and it would bounce and count against the mailbox's bounce rate (ADR-034). The proposal is
still useful — a name and a title are something a person can act on — and the import route refuses
it with a sentence explaining why rather than a validation error.

## The `mock` provider is not only for tests

It is in `CONTACT_PROVIDERS` and in the database's check constraint. The owner holds **no provider
key** (plan question 3, answered "none"), so without `mock` the whole feature would be unreachable
and untestable, and the first time any of this code ran would be against somebody's paid account.

It returns one **verified** address and one **guess**, which is exactly the pair the review screen
has to tell apart.

## Caps, and whose they are

- **25 lookups per workspace per 24 hours** (`lookup.DAILY_LOOKUP_BUDGET`). This is **our** limit,
  not the provider's, and the refusal says so — somebody told "you are out of lookups" will check
  their provider dashboard, find credit there, and report a bug. It exists so a loop cannot spend
  a month of somebody's credits in an afternoon.
- **10 contacts per lookup** (`providers.MAX_RESULTS`). A provider page is 100; nobody reviews 100
  strangers, and a larger page costs more for results that go in the bin.
- The budget is counted from `events` rows of type `contact.lookup`. `events` refuses UPDATE and
  DELETE by trigger, so the count cannot drift — and the budget and the audit trail are then the
  same fact rather than two that can disagree.

## Hard rules this feature sits against

- **Hard rule 8 — one account per provider per org, never rotate keys to dodge a quota.** Enforced
  by a partial unique index on `(org_id, provider) WHERE revoked_at IS NULL`, so holding several
  live keys for one provider is not expressible. Saving a new key revokes the old row rather than
  deleting it, so the change stays auditable.
- **No scraping, of LinkedIn (hard rule 4) or of a provider's own site.** Every call here is to a
  documented JSON API. A `profile_url` a provider returns is stored for a person to open and is
  **never fetched**.
- **Nothing in the background.** No job kind, no scheduler pass, no worker import.
  `tests/unit/test_contacts_button_press_only.py` asserts all three over the source tree, and
  asserts positively that the only caller of `lookup.run` is the router.
- **No key or provider message is ever returned.** A row carries a boolean `usable`;
  `invalid_reason` is read to decide whether a lookup may run and is never serialised, because it
  can quote a provider's reply and a response reaches a browser and a screenshot.

## Adding a Connect button later

`provider_credentials.kind` is `api_key` | `oauth` from the first migration, which is the answer to
the owner's question about whether a Connect button could fetch a key instead of the user pasting
one. The answer differs per provider — key-based for Hunter, client credentials the user still
copies for Snov, partner-gated for Apollo as far as can be established without an account — so
adding a Connect flow is a handler, not a migration.
