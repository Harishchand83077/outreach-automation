# Outreach Platform v3 — Competitive Research, Target Architecture and Roadmap

Oct 29, 2026 · @Harish Chand

## Executive summary

**Recommendation: stay a grounded, human-approved outreach engine with two campaign kinds (agency outreach, job hunt), and do not compete on contact databases or sending volume.** The repo is further along than the earlier plan assumed, so the next work is closing the go-live gate, then adding the pieces competitors do not combine.

- **What already exists.** `outreach` and `job_hunt` campaign kinds (ADR-025), an outcome dataset that treats silence as censored (ADR-026), a read-only Claude connector with seven tools (ADR-028), per-user Gmail OAuth with no deployment mailbox (ADR-023), invitations, a platform-operator scope, and a single send gate. Source: the repo's `PROGRESS.md` and `DECISIONS.md`, read 2026-10-29.
- **How the market splits.** Data providers (Apollo, Clay), sending infrastructure (Instantly, Smartlead), multichannel engagement (lemlist), and job-search tools (Huntr, Careerflow, Simplify). Among the products I opened, none pairs claim-checked, cited drafting with mandatory human approval, and none shares one engine between sales outreach and job hunting.
- **Where we are behind, on purpose or not.** No contact database, no waterfall enrichment, one mailbox per campaign with no rotation pool, no warm-up network, no job-post intake, no browser extension, no signal triggers.
- **Build order.** (1) Go-live gate and deliverability hardening. (2) Job-hunt intake from public ATS feeds plus a candidate answer bank. (3) A free Chrome extension that reads the page and suggests, never submits. (4) Signals, graph and brief. (5) Claude connector phases 2 and 3, only after real traffic.
- **Money.** Everything proposed runs on the free tier. Optional one-time items are listed in the paid-options section; the credit runway in the repo notes is about $0.64 a day, so nothing recurring should be added without a decision.
- **Three risks that can stop the project.** Google's restricted mail scope keeps refresh tokens at 7 days while the OAuth client is unpublished; LinkedIn forbids automation, so every LinkedIn step stays user-initiated; and India's DPDP Rules are in phased force.

Vendor figures below come from third-party reviews unless marked otherwise, and are labelled as such. Items I only saw in search listings, not opened, are named in the open-questions section.

## Competitive landscape

**The eight products I opened sit in four layers, and only the engagement layer overlaps what we are building.** Read the last column as the decision each row forces on us.

| Product | Layer | What it does (source) | Price seen | Implication for us |
| --- | --- | --- | --- | --- |
| [Apollo](https://knowledge.apollo.io/hc/en-us/articles/19331318468621) | Data + sequencing | Fills its database from a contributor network (over 2 million contributors), reply and bounce signals, public-web crawling and third-party providers; runs a seven-step email check; refreshes on signals and monthly (vendor page) | Not confirmed | Do not build a database. Accept a user-supplied sheet and optional connectors |
| [Clay](https://university.clay.com/lessons/enriching-company-data) | Enrichment | Queries providers in an order you set and stops at the first hit, so credits go only to providers actually called; test on 10 rows first (vendor course) | Not confirmed | Copy the waterfall idea for our own research steps, not the provider marketplace |
| [Instantly](https://www.salesforge.ai/blog/instantly-email-infrastructure) | Sending | Sends through your own Gmail or Outlook inboxes over SMTP and reads replies over IMAP; rotates inboxes with per-inbox limits; shared warm-up pool, private options extra; text-only and stop-on-reply at platform level (a competitor's blog, not Instantly) | Not confirmed | Same protocol choice as ours. Their edge is the pool of inboxes and the warm-up network |
| [Smartlead](https://bouncezero.io/blog/smartlead-review-2026) | Sending | Weighted rotation across mailboxes, follow-ups kept on the original sender, provider-matched warm-up, master inbox, API and webhooks from the Pro plan, AI reply classification (third-party review) | About $39, $94 and from $174 a month | Sticky sender and per-mailbox caps are worth copying. Its own built-in check is syntax and MX only |
| [lemlist](https://www.lemlist.com/lemlist-alternatives) | Multichannel | Email, LinkedIn, calls, WhatsApp and SMS in one sequence, a unified inbox, a 650M+ lead database, AI agents and an MCP server (vendor page) | Not confirmed | Confirms MCP is becoming standard. Multichannel is out of scope for us |
| [Huntr](https://huntr.co/blog/huntr-vs-careerflow) | Job search | Application tracker, resume and cover-letter tools with reviewable AI suggestions (Huntr's own blog) | Free tier exists | Tracker is table stakes, and our funnel already covers it |
| [Careerflow](https://huntr.co/blog/huntr-vs-careerflow) | Job search | LinkedIn profile tools, faster AI with less control per edit, more cleanup at 50 to 200 applications (same blog, so Huntr's view) | Limited free tier | Do not copy the LinkedIn profile tools |
| [Simplify Copilot](https://help.simplify.jobs/articles/1749022-installing-and-setting-up-copilot) | Job search | Browser extension that detects an application form, fills fields from your profile and lets you review and submit; named support for Workday, Lever, Greenhouse, Ashby, iCIMS and Taleo (vendor help page) | Not confirmed | The extension pattern is proven. Our difference is cited answers and a human gate |
| LinkedIn | Network | Help centre lists prohibited software and extensions; page not opened, only seen in a search listing | n/a | All LinkedIn steps stay user-initiated (hard rule 4) |

**Where we already differ.** Competitors send fast and personalise with a model; this repo refuses a draft whose claims do not resolve to a persona proof point or cited research (`_check_claims_resolve`), and a person approves every first email. That is slower and safer, and it is the position worth defending.

**Sources:** the links in the table, all opened on 2026-10-29 except LinkedIn. Prices are third-party and approximate.

## How competitors are built

**None of these vendors publishes its internals, so the architecture below is partly inference. Each row separates what a page states from what I conclude.** I found no engineering write-ups for queues, storage or tenancy at any of them, and I am not claiming any.

| Decision | Stated by a source | My inference | What we do |
| --- | --- | --- | --- |
| Send through the customer's own inboxes | [Instantly sends via SMTP and reads replies via IMAP](https://www.salesforge.ai/blog/instantly-email-infrastructure) | They run no mail server of their own, so sender reputation is the customer's problem and theirs only through shared warm-up | Same: XOAUTH2 over SMTP and IMAP (ADR-018). Keeps Sent-folder recovery working |
| Rotate inboxes, keep the sender sticky | [Weighted round-robin; follow-ups stay on the original mailbox](https://bouncezero.io/blog/smartlead-review-2026) (third-party review) | The mailbox is written onto the lead at first assignment and read back for follow-ups | Today the cap is per mailbox and campaigns round-robin inside it. A pool per campaign is a Should (F7) |
| Warm-up network | [Shared pool by default, private on request](https://www.salesforge.ai/blog/instantly-email-infrastructure) | Customers' inboxes send and reply to each other, which depends on the providers tolerating it | Do not build. Keep the ramp (20 a day, plus 10 every 2 business days) |
| Ordered, short-circuiting enrichment | [Providers are tried in order and the rest skipped after a hit](https://university.clay.com/lessons/enriching-company-data) | A per-field provider list plus a credit ledger | Research already fetches the site first and searches only when evidence is thin (ADR-011). Add provider steps behind the same interface |
| Contact data from users | [Contributor network, engagement signals, crawling, third-party feeds](https://knowledge.apollo.io/hc/en-us/articles/19331318468621) | Legal and consent exposure rests on them; it is not replicable by a small team | Skip. User supplies the sheet |
| Email verification | Apollo describes a seven-step check; Smartlead's built-in check is [syntax and MX only](https://bouncezero.io/blog/smartlead-review-2026) (third-party) | A paid verifier is where bounce control really lives | We check MX at import. Bounce auto-pause is the real control (F3) |
| Form autofill | [Detects the form, matches fields to the profile, user reviews and submits](https://help.simplify.jobs/articles/1749022-installing-and-setting-up-copilot) | Content scripts with per-ATS mappers plus a heuristic fallback | Same shape, plus a cited answer per field and an explicit approve step |
| Extension code rules | [Functionality must be discernible from the submitted code; no remote logic; remote config is allowed when the logic ships in the package](https://developer.chrome.com/docs/webstore/program-policies/mv3-requirements) | Field-mapping rules must ship inside the extension and update through store releases | Server returns data only, never rules or scripts |
| Job intake | [Greenhouse, Lever and Ashby expose public JSON job feeds](https://cavuno.com/blog/ats-platforms-public-job-posting-apis) (third-party article) | Aggregators poll these and link out to the employer's apply URL | Poll feeds for companies the user names, store the apply URL, never submit |

**What this changes in our design.** Three decisions follow directly: keep the single send gate rather than add a mailbox pool before deliverability is measured; ship extension rules in the package, not from the server; and treat public ATS feeds as the legitimate substitute for scraping job boards.

## Gaps, positioning and priorities

**We win on trust and fit, not on reach, so the roadmap buys safety first and breadth second.** The positioning is four claims we can defend today and two we must earn.

- **Defensible now.** Every claim in a draft resolves to a proof point or a cited page. A person approves every first email and every reply. One engine serves agency outreach and job hunting. Silence is stored as censored, never as a rejection.
- **To earn.** Measured deliverability (no spam-rate reading exists yet) and a learning loop with enough sends to say anything.
- **Deliberately not competing on.** Contact data, volume, multichannel, open tracking.

### Prioritised features

MoSCoW: **M** must before the first real campaign, **S** should, **C** could, **W** will not.

| ID | Feature | Class | Why | Phase |
| --- | --- | --- | --- | --- |
| F1 | Go-live gate: aligned From and DKIM, rehearsal, Postmaster Tools, a booking link that books | M | Repo notes say unauthenticated mail already landed in Spam | 10 |
| F2 | Off-box backup of the database | M | Done: nightly S3 copy and a restore drill on 2026-10-13 (GO\_LIVE.md) | 10 |
| F3 | Bounce and complaint auto-pause (Postmaster is a link only, ADR-029) | M | [Gmail and Yahoo cap spam complaints at 0.3%](https://customer.io/learn/deliverability/email-sending-requirements-gmail-yahoo) (secondary summary) | 11 |
| F4 | Sender-requirement check: SPF, DKIM, DMARC, one-click unsubscribe (RFC 8058) | M | Same source; `List-Unsubscribe` exists in `message.py`, one-click form unverified | 11 |
| F5 | Job-post intake from public ATS feeds and a pasted URL | S | Replaces scraping job boards | 12 |
| F6 | Candidate profile, answer bank, cited fit score | S | Extends `CandidateProfile`; keeps the no-invention rule | 12 |
| F7 | Mailbox pool per campaign with sticky sender | S | Smartlead-style rotation, only after F3 gives a health signal | 14 |
| F8 | Chrome extension: capture a page, suggest form answers, never submit | S | Simplify proves demand; our gate is the difference | 13 |
| F9 | Hiring-contact lookup through the user's own provider keys | S | Apollo, Hunter or Snov keys supplied by the user | 13 |
| F10 | Referral finder from the user's LinkedIn connections export | S | User-initiated file upload, no scraping | 13 |
| F11 | Signal triggers: a watchlist of companies and a hiring-post feed | C | Gives outreach a reason to be sent today | 15 |
| F12 | Knowledge graph and cited meeting brief | C | `get_meeting_brief` exists as a tool; the graph deepens it | 15 |
| F13 | A/B variants with an exploration share | C | `variant` column exists; no allocation logic yet | 15 |
| F15 | Booking webhook that sets `MEETING_BOOKED` | S | The only legitimate producer of that outcome besides a click | 14 |
| F14 | MCP phase 2 (own OAuth server) and phase 3 (propose drafts) | C | Gated by ADR-027 on production traffic | 16 |
| F16 | Learning from edit diffs and outcomes | C | Needs volume first | 16 |
| F17 | Ordered provider lookups behind the research interface | C | Clay-style, on the user's keys | 14 |
| F18 | Multichannel, open or click tracking, contact database, auto-apply, warm-up network | W | Off-strategy, riskier, or against hard rules | none |
| F19 | Shared public-company cache with per-workspace fair share | S | Saves search credits; public facts only | 12 |
| F20 | Resume tailoring by validated edit operations, immutable versions, application tracker | S | Keeps the no-invention rule on resumes | 13 |
| F21 | Job-queue hardening: dedupe keys, lease reclaim, per-workspace caps, version stamps | M | New modules multiply job kinds; each must be safe to re-run | 11 |
| F22 | Daily hiring-signal poll of public ATS feeds with diff and page-hash fallback | C | Feeds F11 | 15 |
| F23 | Pool of hashed bounce and opt-out lists across workspaces; pooled uploaded emails | W | Consent and DPDP risk; revisit only after legal advice and a measured domain overlap | none |
| F24 | Claude connector with scopes the user ticks: read, profile (minus excluded fields), research notes as proposals, propose drafts | C | Never send, approve, submit or change SEND\_MODE | 16 |

### One-time or prepaid options

The repo's rule 5 says to stop and ask before spending, so these are options, not plans.

- **Chrome Web Store developer registration.** A one-time fee, about $5 by my earlier research; confirm at sign-up. Required only for public distribution of F8.
- **Prepaid email-verification credits.** A pay-as-you-go pack would back F3 and F9. I have not priced any provider.
- **A separate sending domain.** The target architecture recommends one so cold-mail reputation cannot touch the main domain. A domain renews yearly, so it is not one-time.
- **Not one-time, so excluded:** seed-inbox testing tools, a CASA security assessment for a published Google client, and a larger VM.

The honest result is that only one item is truly one-time. Everything else recurs.

## The system as it is today

**One FastAPI service and one worker process share a Postgres database, and every row belongs to an org.** This section is from the repo at commit `b3572eb` (the MCP change, #116), read 2026-10-29; counts are from `models.py`, the migrations folder and `PROGRESS.md`.

### Runtime

- **Hosting.** Docker Compose on one AWS EC2 `t3.small` in Sydney: api, worker, Caddy (automatic TLS) and Postgres, with no published database port. Frontend: React and Vite on Cloudflare. Supabase provides sign-in only; tokens are verified locally against its JWKS and mapped to our own `members` row.
- **API.** `app/api/routers`: campaigns, review, imports, operations, connect (Gmail OAuth), team, platform, me, health, plus a separate `/mcp` router.
- **Worker.** Five asyncio tasks: `JobRunner`, `SendLoop`, `ImapLoop`, `Scheduler`, `Heartbeat`. `SendLoop` is the only code that calls SMTP.
- **Queue.** A Postgres `jobs` table claimed with `FOR UPDATE SKIP LOCKED` (ADR-014 rejected a broker because a job is enqueued in the same transaction as the state change that justifies it).
- **Model access.** `LLMRouter` over an ordered, free-first chain with cool-downs, per-entry caps and `llm_usage` accounting. Search is Tavily, used only when fetched evidence is thin.

### Lead lifecycle

`NEW → ENRICHING → RESEARCHED → DRAFTED → AWAITING_APPROVAL → APPROVED → SENT → FOLLOWUP_SENT → REPLIED → IN_CONVERSATION`, ending in `MEETING_BOOKED`, `NOT_INTERESTED`, `OPTED_OUT`, `BOUNCED`, `NO_RESPONSE`, `SKIPPED` or `FAILED`. One function, `domain/state.transition()`, writes `leads.state` and the audit event in the same transaction. Pausing is a flag, never a state.

### Data: 27 tables in schema `app`, 22 migrations

| Group | Tables |
| --- | --- |
| Tenancy and access | `orgs`, `members`, `invitations`, `platform_operators`, `api_tokens`, `send_allowlist` |
| Mail | `mailboxes`, `mail_credentials` (Fernet-encrypted), `oauth_states`, `imap_cursors`, `suppressions` |
| Campaign work | `campaigns` (with `kind` and `mode`), `leads`, `drafts` (versioned), `messages`, `attachments`, `jobs` |
| Research | `domain_research` (90-day cache, hooks age after 30 days), `research`, `search_usage` |
| Replies and learning | `classifications`, `lead_outcomes`, `experiments`, `events` (append-only) |
| Operations | `llm_usage`, `alerts`, `worker_heartbeats` |

### Controls that already hold

- **Send gate.** `SEND_MODE` (`disabled`, `allowlist`, `live`) resolves in one function, `effective_send_mode()`, beside `allowlist_permits()` in `smtp.py`. A tree-wide test asserts nothing else decides. Going `live` needs owner role, production and a typed confirmation.
- **Cap and window.** 100 a day per mailbox across all outbound; warm-up 20 a day plus 10 every two business days; Monday to Friday, 10:00 to 17:30 IST, jittered. Job-hunt campaigns default to 25 a day.
- **At-most-once sending.** Claim row with a pre-generated `Message-ID`, send, mark; stuck rows are recovered by searching the Sent folder.
- **Grounding.** A draft may claim only what resolves to a persona proof point (`pp\d+`) or cited research; template mode renders operator text and refuses unresolved merge fields.
- **Privacy.** Per-lead erasure that leaves a hashed suppression, an hourly retention sweep (12 months default), and page text cleared after 30 days.
- **Tenant isolation.** Every repository call takes an `OrgContext`; the platform operator type has no `org_id` by construction (ADR-024).
- **Connector.** `POST /mcp`, stateless, seven read-only tools (`list_campaigns`, `get_campaign`, `list_leads`, `get_lead`, `get_research_with_sources`, `list_reply_queue`, `get_meeting_brief`), own `oa_` tokens with a database-enforced `mcp:read` scope.

### What the repo does not have yet

A job-post table or intake, a candidate answer bank beyond the persona, any browser extension, signal triggers, a mailbox pool per campaign, A/B allocation (the `variant` column is unused by logic), a booking webhook, bounce-rate auto-pause (the target architecture lists it; I did not verify it is built), and a one-click unsubscribe link (the header says one-click but points at a mailto). Off-box backups now exist; see the update at the end of this section.

### Known open items from the repo's own notes

1. The mailbox `from_email` was not the authenticated SMTP account, so SPF and DKIM for `arkvane.in` did not apply to test sends. ADR-023 later removed the deployment mailbox, which should resolve this once an `@arkvane.in` Workspace address is connected through OAuth.
2. Google Postmaster Tools was set up by hand (ADR-029), but Google shows nothing at our volume; DMARC reports are the signal that works.
3. The persona booking link opened the reader's own calendar and booked nothing.
4. Phase 10, the rehearsal and go-live gate, has not started. Credits were $118.08 of $120 with about $0.64 a day spent at the time of that note.

**Caveat.** `PROGRESS.md` carries an old header date (2026-10-11) over content that runs to 2026-10-28, and my local clone may lag the live branch. Update 2026-10-31: commit a78e78a added Postmaster setup and a live smoke check (ADR-029), and GO\_LIVE.md records a nightly S3 backup and a restore drill done on 2026-10-13, so F2 is done.

## Target architecture

**The target keeps one engine and one send gate, and adds five modules that only ever write proposals into the existing review queue.** Nothing new may send, approve or touch `SEND_MODE`; that is the rule every addition below is checked against.

*(Diagram: target architecture — clients, APIs, services, send path. See the Claude doc for the picture.)*

Every new box feeds the existing pipeline or the review queue; the only route to Gmail still runs through the worker and the one send gate, and deliverability can only pause a mailbox.

### New components

| Module | Job | Writes to | Never does |
| --- | --- | --- | --- |
| Job intake | Poll public ATS feeds for companies the user lists; accept a pasted posting URL | `job_posts` | Scrape a job board or LinkedIn |
| Candidate core | Profile facts, approved answer bank, cited fit score | `profile_facts`, `answer_bank`, `fit_scores` | Invent a fact; fill an answer it cannot cite |
| Extension gateway | A second API surface for the Chrome extension, with its own token scope | `captures`, `suggestions` | Submit a form; return rules or scripts |
| Contact providers | Ordered lookups through the user's own provider keys | `provider_credentials`, `contact_candidates` | Hold a shared key; verify by sending |
| Signals and graph | Watchlist, hiring-post events, entities and edges with provenance | `watchlist`, `signal_events`, `entities`, `edges` | Create a lead without a person approving |
| Deliverability | Per-mailbox health, auto-pause, sender-requirement checks | `mailbox_health` | Raise a cap; override the gate |
| Booking intake | Signed webhook from a booking tool | `lead_outcomes` | Mark a meeting from a reply |

### Data model additions

All additive, all carrying `org_id`, all behind the existing `OrgContext` rule.

| Table | Key columns | Notes |
| --- | --- | --- |
| `job_posts` | `org_id`, `source` (greenhouse, lever, ashby, pasted), `external_id`, `company_domain`, `title`, `location`, `apply_url`, `posted_at`, `body_text`, `fetched_at`, `status` | Unique on `(org_id, source, external_id)`; body text cleared by the retention sweep |
| `profile_facts` | `org_id`, `fact_id` (`pp1`, `pp2`...), `kind` (skill, project, role, education), `statement`, `evidence_url` | Same ids the claim check already resolves |
| `answer_bank` | `org_id`, `question_pattern`, `answer`, `fact_ids[]`, `approved_at` | Only approved rows are offered to the extension |
| `fit_scores` | `job_post_id`, `profile_version`, `score`, `matched[]`, `missing[]`, `evidence[]` | Computed by rules over extracted requirements; a model only extracts text |
| `captures` | `org_id`, `token_id`, `page_url`, `page_text`, `captured_at`, `kind` | Created only by a user action in the extension; short retention |
| `suggestions` | `capture_id`, `field_key`, `proposed_answer`, `fact_ids[]`, `state` (proposed, accepted, edited, rejected) | The accept and edit signal feeds the dataset |
| `provider_credentials` | `org_id`, `provider`, sealed key, `daily_cap` | Same Fernet scheme as `mail_credentials` |
| `contact_candidates` | `lead_id` or `job_post_id`, `email`, `source`, `confidence`, `fetched_at` | Never enters sending until imported as a lead |
| `watchlist` and `signal_events` | company domain, signal kind, source URL, `seen_at` | A signal proposes a lead; it does not create one |
| `entities` and `edges` | `type`, `name`, `domain`; `src`, `dst`, `relation`, `source_url`, `observed_at`, `confidence` | Provenance is mandatory; an edge without a source is refused |
| `mailbox_health` | `mailbox_id`, `day`, `sent`, `bounced`, `complaint_rate`, `source` | Feeds auto-pause; no manual overrides |
| `campaign_mailboxes` | `campaign_id`, `mailbox_id`, `weight` | Plus `leads.mailbox_id` for the sticky sender |
| company\_profiles | domain (unique), name, careers\_url, ats\_type, ats\_slug, size\_band, summary, source\_url per field, content\_hash, fetched\_at, version | Shared across workspaces: public facts only, no org\_id, no lead fields. Exception to ADR-004; needs its own ADR and a structural test |
| resume\_versions | org\_id, base\_resume\_id, edit\_ops, rendered\_text, pdf\_key, fact\_ids\[\], created\_at | Immutable: a change creates a new row. Every sentence must map to a confirmed fact id |
| applications | org\_id, job\_post\_id, resume\_version\_id, status (planned, applied, interviewing, closed), applied\_at, follow\_up\_due | Only the user sets applied; the extension cannot |
| jobs.dedupe\_key | new column on the existing jobs table; unique on (org\_id, kind, dedupe\_key) | Makes enqueueing and handlers idempotent |

### Flow A: from a posting to a sent email

1. A feed poll or a pasted URL creates a `job_posts` row.
2. A model extracts requirements from the posting text, treated as untrusted data.
3. Rules compare requirements with `profile_facts` and write a `fit_scores` row listing each match with the fact it cites.
4. The user picks a posting worth pursuing; contact providers propose hiring contacts.
5. A `job_hunt` lead is created, researched and drafted through the existing pipeline; claims resolve to profile facts.
6. The user approves; `SendLoop` sends under the same gate, cap and window.

### Flow B: form assistance in the browser

1. The user opens an application page and presses a button in the extension.
2. The extension sends the visible question labels and the page URL to the gateway.
3. The gateway matches labels to approved `answer_bank` rows, drafts any gaps from `profile_facts` with fact ids, and flags the rest as needing the user.
4. The extension shows the proposals; the user accepts, edits or rejects each one, then submits the form themselves.

### Boundaries that stay fixed

- A new route that can reach a send, an approval or a send mode fails the existing tree-wide guard.
- The extension token cannot read `/api/v1`, and a Supabase JWT cannot read the extension gateway, repeating the two-doors rule from ADR-028.
- Provider keys belong to the user and are used only for lookups the user triggered.

## Job-hunt module and Chrome extension

**Job hunting needs four additions on top of the existing `job_hunt` kind: where postings come from, a fit score the candidate can audit, a way to reach a person, and a browser helper that only suggests.** Each uses a path the platforms permit.

### Where postings come from

| Source | Method | Status |
| --- | --- | --- |
| Greenhouse, Lever, Ashby | Public JSON feeds, per [this survey](https://cavuno.com/blog/ats-platforms-public-job-posting-apis): `boards-api.greenhouse.io/v1/boards/{token}/jobs`, `api.lever.co/v0/postings/{site}`, `api.ashbyhq.com/posting-api/job-board/{name}` | Verified in a third-party article; test each endpoint before building |
| Workable, Recruitee, Personio | Public feeds per the same article | Optional, later |
| A posting the user is reading | Pasted URL or text, or captured by the extension on a button press | Primary path for Indian boards and LinkedIn posts |
| Naukri, Instahyre, Cutshort, Wellfound | No public API found; terms not checked | Capture by the user only |
| LinkedIn | No scraping, no automation (hard rule 4; LinkedIn's help centre lists prohibited software, page not opened) | Prefilled search links and user-pasted text |

### Fit score the candidate can audit

A model extracts the posting's requirements as plain text, and that output is treated as untrusted. Deterministic rules then compare each requirement with `profile_facts`. The result lists every match with the fact id it rests on, every gap, and no single opaque number without them. This reuses the claim-resolution idea: nothing is asserted that does not point at something the candidate wrote.

### Reaching a person

- **Hiring contact.** The user pastes their own Apollo, Hunter or Snov key; lookups run only when they press the button, and results land in `contact_candidates` for review.
- **Referral.** The user uploads their LinkedIn connections export. Matching connections at the target company produce a suggested note and a link the user opens and sends themselves. Nothing is sent from LinkedIn by us.
- **Cold email.** The existing `job_hunt` pipeline: 25 a day by default, one follow-up, claims limited to the candidate's facts.

### The answer bank

Common application questions (notice period, expected pay, work authorisation, links) are answered once by the user and stored with the facts they rest on. Anything not in the bank is marked as needing the user. This is the direct answer to tools that guess screening answers, which I saw described in third-party reviews but did not verify.

### Resume tailoring and the application tracker

The resume is edited by rule, not rewritten freely. The model proposes edit operations (include or drop a bullet, reorder, rephrase inside one fact); a validator then checks that every sentence in the result maps to a confirmed fact id, and rejects a new number, skill or employer that is not in `profile_facts`.

- **Facts first.** The master resume is uploaded once and split into facts. Facts mined from GitHub start as proposals and count only after the user confirms them.
- **Immutable versions.** Each tailored resume is a `resume_versions` row that is never edited; a change makes a new row. PDF rendering runs as its own memory-limited job so a large file cannot stall the worker.
- **Application tracker.** An `applications` row ties a posting to the exact resume version used, with status and a follow-up date. Only the user marks a row as applied; the extension never does.
- **LinkedIn.** Profile data enters only through the user's own export file. Nothing is scraped and nothing is automated, because [LinkedIn's terms forbid it](https://www.linkedin.com/legal/user-agreement)(not rechecked in this pass).
- **Data providers.** We do not scrape Apollo-style sites. Contact lookups run on the user's own provider keys, as in the previous section.

### Chrome extension (free)

| Decision | Choice | Reason |
| --- | --- | --- |
| Manifest | MV3 with a side panel UI | Current store requirement; side panel keeps the page visible |
| Page access | `activeTab` on a button press, optional host permissions for named ATS domains | Narrowest access that works; design choice, confirm in the Chrome docs while building |
| Rules | Field mappers ship inside the package | Store policy: [no remotely hosted logic](https://developer.chrome.com/docs/webstore/program-policies/mv3-requirements) |
| Auth | A token minted in Settings and pasted once, scope `ext:suggest`, hash-stored like MCP tokens | Reuses `api_tokens`; avoids a browser-identity flow in v1 |
| Writes | Suggestions only. It fills fields the user accepts and never clicks submit or uploads a file | Keeps the human gate |
| Tooling | TypeScript with WXT or CRXJS (from earlier research, not rechecked) | Fast build and reload |
| Testing | Playwright with the extension loaded against saved copies of Greenhouse, Lever and Ashby form pages | Playwright is already available in this environment |
| Distribution | Load unpacked first; public listing later | Listing needs a privacy policy and a single-purpose description |

**What the extension sends.** Question labels and the page URL for form help; the visible posting text only when the user presses Capture. It never reads other tabs and never stores page contents locally.

**What can go wrong.** Pages built from shadow DOM or iframes (Workday is known for this; not tested) will defeat simple field readers, so those get capture-only support first. An ATS redesign breaks a mapper, so each mapper has a fixture test and a fallback to labelled manual entry.

**Forms that span several pages.** The extension keeps one application session per browser tab: answers already accepted stay in that tab's memory until the tab closes. It reads the page structure (labels and field types), not screenshots, and it never reads values the user has typed.

## Shared data, offline work and failure handling

**Four rules decide what is shared, what runs in the background, and what happens when something crashes.** They apply to every phase below, and each becomes an acceptance test.

### Shared company cache

Researching the same company for every workspace wastes search credits. A `company_profiles` table keyed by domain holds public facts only: name, careers page, ATS type and slug, size band, a short summary, and the source URL and fetch time for each field.

- It has no `org_id`. That is an exception to the org-scoping rule (ADR-004) and needs its own ADR.
- A structural test asserts the table has no lead, contact or message columns, and that no code path writes workspace data into it.
- Workspace-specific notes (why this company matters to this user) stay in org-scoped tables.
- Each workspace gets a daily fair share of search and fetch slots; a cache hit costs no slot. Entries are refreshed when older than an assumed 30 days, and only if the page hash changed.

### What is not pooled

Email addresses a user uploads in a sheet are **not** pooled across workspaces. Reasons: the user gave them for their own campaign (consent and purpose limits under the DPDP Rules), users would stop trusting the product if their list fed someone else's, and the data goes stale. A pool of hashed bounce and opt-out lists, or of domain facts such as catch-all or dead domains, could help deliverability, but only after (a) legal advice and (b) a measurement of how much the domains of different workspaces actually overlap. Until then each workspace keeps its own suppression list. This is feature F23, class W.

### Offline and online work

| Work | Where it runs | Rule |
| --- | --- | --- |
| Approve a draft, settings, reads, answer-bank lookup for the extension | Online, in the request | Database lookups only; target under about 300 ms (a design target, not measured) |
| Anything that calls a model or the web: research, fit score, draft, resume tailoring | Offline, as a queued job | The request returns a job id; the page polls |
| Sending, inbox polling, posting-feed polling, signal polling, retention sweep, backup | Offline, on a schedule | Same worker; no user waiting |
| Resume PDF rendering | Offline, separate job | Memory limit set below the host's; failure leaves the old version in place |

### Rules every handler follows

1. **Idempotent.** Every job has a `dedupe_key`, unique per `(org_id, kind, dedupe_key)`. Running a handler twice produces the same rows, not two.
2. **Crash-safe.** A claimed job has a lease and a heartbeat; a job whose lease expires is returned to the queue. Sending keeps its at-most-once outbox. A test kills the worker mid-job and asserts nothing is sent twice or lost. (Confirm what the current queue already does before adding this.)
3. **Versioned results.** Fit scores, company profiles and tailored resumes store the version of their inputs and are recomputed only when an input changed.
4. **Capped and fair.** Each workspace has a cap on queued jobs and on provider calls per day. Retries use backoff with a ceiling, then land in a visible dead-letter state.

Parallelism follows the data: companies and workspaces are processed in parallel; sends are sequential per mailbox (one in flight, sticky sender); state changes on one lead are serialised by a row lock. Worker concurrency stays small on the 2 GB host (assumption to be tuned by measurement).

### Daily hiring signals

For each company on a watchlist, the worker fetches the public Greenhouse, Lever or Ashby job feed once a day and compares it with the last snapshot by `external_id`. A new posting becomes a `signal_events` row that proposes a lead; it never creates one. Where no feed exists, a hash of the careers-page text is stored and a change is flagged for review. Each site's terms and robots rules are checked before a company is added to the poll (not verified here).

## Knowledge graph and meeting brief

**Build the graph as two Postgres tables with mandatory provenance, not as a graph database.** The volume per workspace is small, the data already lives in Postgres, and tenancy is one `org_id` filter. A two-hop question (who do I know at this company, and who posted about hiring there) is a recursive query, not a reason for a second datastore.

### What goes in

| Entity | Created from | Edges it can have |
| --- | --- | --- |
| Person | A lead, a connection-export row, a named author on a researched page | `works_at`, `connected_to` (from the user's own export), `replied_to`, `referred_by` |
| Company | A lead's registrable domain (the research cache key, ADR-011), a job post | `posted_job`, `mentioned_in` |
| Page | A researched or captured URL | `states` (a hook or fact the page supports) |
| Message | An inbound or outbound message id | `answers`, `mentions` |

**Rule: an edge without a source is refused.** Each edge stores `source_url` or `message_id`, `observed_at` and a confidence. A brief can then cite every line, and a stale edge can be shown as old rather than silently trusted. Hooks already age after 30 days in the research cache, so the graph follows the same clock.

### The meeting brief, today and next

Today, `get_meeting_brief` composes the lead and its cited research into one read, built from the other read paths so it cannot disagree with them. The target brief adds, each with its citation:

1. **Who they are:** role, company and the page that says so.
2. **What changed lately:** signals from the watchlist, newest first, with dates.
3. **How we are connected:** shared connections from the user's export, or a prior thread.
4. **The conversation so far:** every message and its classification label.
5. **Open questions for the call:** gaps the research marked, so the user knows what to ask.

The brief is assembled by queries and templates; a model may phrase it but may not add a line that has no edge behind it.

### What this does not do

- It does not enrich people from private sources or from LinkedIn pages.
- It does not merge people across workspaces; entities are per org.
- It is not needed for the first real campaign, so it sits in the Could tier.

## Claude connector

**Phase 1 is built and read-only; the next step is more read tools, and the write phase waits for real traffic.** Letting a user's own Claude work on their workspace moves model cost onto their quota, which matters because the free model chain is this system's real ceiling (ADR-027).

### Built (ADR-028, `docs/MCP_PLAN.md`)

- A single stateless `POST /mcp` plus `server/discover`, following the 2026-07-28 revision as recorded in the repo's notes. The repo verified it; I did not re-read the specification.
- Seven read-only tools, with third-party text wrapped in `<untrusted>` tags that escape their own closing tag.
- Own `oa_` tokens, hash-stored, shown once, scope `mcp:read` enforced by a database `CHECK`. Membership is re-resolved on every call, so removing a person revokes their tokens.
- A Supabase JWT is refused here and an `oa_` token is refused everywhere else, because Supabase's OAuth server ignores the RFC 8707 `resource` parameter (open issue [supabase/auth#2610](https://github.com/supabase/auth/issues/2610), per ADR-028).
- Every call writes an `mcp.tool_called` event.

### Next

| Step | What | Gate |
| --- | --- | --- |
| Read tools for new data | `list_job_posts`, `get_fit_score`, `get_entity_edges`, `list_signals` | Only after those tables exist; same untrusted wrapper |
| Phase 2: sign-in by OAuth | Our own minimal authorization server with PKCE, RFC 9728 metadata and audience checks | A larger build than the endpoint it protects; do it only when pasting tokens becomes the complaint |
| Phase 3: `propose_draft` | Creates the ordinary `Draft` as `pending_review`, `created_by='mcp:<user>'`, through the same validator | ADR-027 requires a decision taken with phases 0 to 2 in production |
| New scope | `mcp:propose`, added by migration to the `CHECK` | Not before phase 3 is approved |
| Scoped grants | At connect time the user ticks scopes: read; read profile, with fields they exclude; research notes written as proposals; propose drafts. There is no scope for sending, approving, submitting forms or changing SEND\_MODE | Each scope is a migration to the CHECK plus a test that the tool list matches the granted scopes |

### The rule that keeps it safe

**The catalogue is the containment.** There is no send tool, no approve tool and nothing near `SEND_MODE`. A fully hijacked agent can read one workspace back to its owner, and after phase 3 it can also leave a bad draft in a queue a person reads. The structural tests (`test_mcp_safety.py` and the tree-wide send guard) keep reading the catalogue, so adding a tool that breaks the rule fails CI.

### Why it is worth keeping

[lemlist's product page](https://www.lemlist.com/lemlist-alternatives) lists an MCP server and a CLI among its AI features, so a connector is becoming expected rather than special. Ours differs by promising what it cannot do.

## Outcome dataset and learning loop

**The recording half is built; the learning half should wait until there are about 2,000 sends per comparison.** Nothing has been sent from this schema yet, so every row captured from the first real send onward cannot be recreated later (ADR-026).

### What is recorded now

- **Outcome per lead** in `lead_outcomes`: positive interest, meeting booked, referral, not now, not relevant, has vendor, objection, auto-reply, unsubscribe, hostile, bounce, or no reply after the window. Silence is `NULL` while watched, then `no_reply_after_window` with `censored = true`; a database constraint ties the two together.
- **Five separable causes:** lead fit, content, timing, deliverability and sender context, each held in a different table so none is overwritten.
- **Human edits:** a changed draft becomes a new `drafts` version, and `derived_from_id` links it to the model's original, so the edit is free preference data.
- **Context:** `variant`, `warmup_day`, prompt version, model entry from `llm_usage`, and claim ids.
- **Export:** salted SHA-256 over addresses and company names, a fresh salt per run, one org per run; `orgs.dataset_pooling_opt_in` exists, defaults to false and is read by nothing.

### Gaps to close

| Gap | Fix |
| --- | --- |
| The outcome list I read has no interview or offer member, which a job hunt needs | Add `interview_scheduled` and `offer`, settable only by a click, like `meeting_booked` |
| `MEETING_BOOKED` has only a click as a producer | Add the signed booking webhook (F15) |
| `variant` is stored but nothing allocates it | Add random assignment with a fixed exploration share, recorded on the message |
| Extension suggestions are not captured | Store accept, edit and reject on `suggestions` |
| The dashboard shows no breakdown by dimension | Add funnel views by variant, weekday, hour and warm-up day |

### How much volume the comparison needs

At the 100-a-day cap, a month holds about 2,200 sends. To tell a 5% reply rate from an 8% one with 80% power and a 5% false-positive rate takes about 1,050 sends per arm (my calculation, using the standard two-proportion formula; the 5% and 8% are illustrative, not measured). So the first honest A/B result needs roughly one month of full-volume sending, and a job hunt at 25 a day will not reach it alone.

### Order of learning

1. **Describe.** Funnel charts by dimension, with censored leads shown as pending, not failed.
2. **Compare.** One variant test at a time, 10% to 20% of sends reserved for exploration (a proposed setting).
3. **Use edits.** Summarise recurring human edits into drafting guidance, reviewed by a person before it enters a prompt.
4. **Only then consider a trained model.** Not before thousands of replies, and not on pooled data without the opt-in.

### Rules that must not slip

- Never train on or report no-reply as a negative class.
- Never pool across workspaces without the opt-in; under DPDP, treat the export as personal data, which the manifest already states.

## Security, compliance and risks

**The highest risks are deliverability and consent, not code.** All three project-stoppers sit outside the repo: Google's token expiry, platform terms and the data-protection rules.

### Rules the outside world sets

- **Gmail and Yahoo.** Senders need SPF, DKIM and DMARC, a one-click unsubscribe that follows RFC 8058, and a spam-complaint rate under 0.3%; a rate near 0.1% already harms reputation with some providers. A sender who meets the bulk criteria once is permanently treated as bulk. Source: a [secondary summary](https://customer.io/learn/deliverability/email-sending-requirements-gmail-yahoo); confirm against Google's and Yahoo's own pages before go-live.
- **India's DPDP Rules.** Notified 13 November 2025. Consent Manager registration applies after one year; most other obligations (notice, security, erasure) apply 18 months after a start date the [Mondaq article](https://www.mondaq.com/india/data-protection/1708164/digital-personal-data-protection-rules-2025-notified) did not state. A notice must be plain-language and itemised, and a breach report to the Data Protection Board is due within 72 hours. Penalties were not in the page I opened.
- **Chrome Web Store.** Extension logic must be discernible from the submitted code, with no remote logic ([policy page](https://developer.chrome.com/docs/webstore/program-policies/mv3-requirements)).
- **LinkedIn.** Automation and scraping are against its terms; I only saw the help-centre page in a search result.

### Risk register

| Risk | Impact | Mitigation | State |
| --- | --- | --- | --- |
| OAuth refresh tokens expire after 7 days while the Google client is unpublished | A mailbox silently stops sending and reading replies | Grant countdown, warnings at one day and zero, a Reconnect state (ADR-021). Exits: publish with a paid annual assessment, or an app-password path | Built; exit undecided |
| Mail lands in Spam from an unauthenticated sender | Replies and reputation lost | Connect an `@arkvane.in` Workspace mailbox by OAuth; verify DKIM pass in Show original (F1) | Open |
| Spam complaints pass 0.3% | Domain blocked for a period | Cap and ramp, plain text, one-click unsubscribe, auto-pause on bounce and complaint (F3, F4) | Partly built |
| Prompt injection through replies, researched pages or captured postings | A draft says something false or hostile | Untrusted wrapper, claim check, human review, no send tool anywhere | Built; extend to captures |
| Extension permission creep or store rejection | Feature unavailable | `activeTab`, named hosts only, package-bound rules, privacy policy before listing | Design |
| Stolen API or extension token | Read access to one workspace | Hash-only storage, per-device tokens, immediate revoke, membership re-check each call | Built for MCP |
| Credential key and database on one VM | One breach exposes mail access | Keep `CREDENTIAL_KEY` out of backups, rotate on suspicion, move off-box backup (F2) | Open |
| Erasure does not reach backups already taken | A deleted person persists up to the retention of the dumps | Say so in `PRIVACY.md` (already stated); age dumps out | Documented gap |
| Cold email to people who never consented | DPDP exposure once in force | Get legal advice on lawful basis for B2B outreach; record notice and opt-out; honour suppressions | Open question |
| LinkedIn restriction on the user's account | Candidate loses profile access | Only user-initiated actions, prefilled links, uploaded exports | By design |
| Free model quota or terms change | Drafting stalls | Ordered multi-provider chain; hard rule 8 forbids rotating accounts | Built |
| Postal address missing from footers | CAN-SPAM gap for US recipients | Audience is India only for now; revisit before widening (QUESTIONS Q2) | Accepted |

### Controls to add for the new modules

1. Extension and provider-key endpoints get their own rate limit and an audit event per call.
2. Captured page text is retained for days, not months, and listed in `PRIVACY.md`.
3. Every new table is included in the erasure path and the tenant-isolation test.
4. A structural test asserts the extension gateway imports nothing that can send.

## Cost and capacity

**Every proposed module adds storage measured in tens of megabytes and no recurring fee; the binding limits are the model quota and the AWS credit runway.** Figures from the repo's notes are as they stood when written, so check the billing console before relying on them.

### Running cost, from the repo's notes

| Line | Figure | Source |
| --- | --- | --- |
| AWS credits left | $118.08 of $120 | `PROGRESS.md`, capacity entry |
| Daily spend | about $0.64 | same entry |
| Runway at that rate | about 184 days (118.08 divided by 0.64) | arithmetic |
| `t3.small` after credits | about $19 a month in Sydney | `PROGRESS.md` |
| Cheaper home | a roughly 4 to 5 euro Hetzner box; nothing is AWS-specific | `PROGRESS.md` |
| Database on disk | about 34 KB per lead; 22 MB at the time of that note | measured by the repo, from a few rows |

The per-lead figure rests on very few drafts and research rows, so treat it as an order of magnitude.

### Added storage (my estimates, assumptions stated)

| Addition | Assumption | Six-month size |
| --- | --- | --- |
| `job_posts` | 2,000 postings a month at about 8 KB of text, body cleared after 30 days to about 1 KB | about 20 MB |
| `captures` | 20 a day at about 20 KB, kept 7 days | under 5 MB |
| `entities` and `edges` | 50,000 edges at about 0.3 KB | about 15 MB |
| `suggestions` and `answer_bank` | small | under 5 MB |

Together under 50 MB, against 23 GB free on the instance. Storage is not a constraint.

### The real constraints

- **Model calls.** Extracting requirements from every polled posting could mean thousands of calls a month. I did not verify any free-tier limit, so the design extracts only for postings the user shortlists or opens, and the router already tracks quota burn per entry.
- **Search credits.** Tavily is used only when fetched evidence is thin (ADR-011); keep that rule for new research steps.
- **Provider lookups.** Run on the user's own keys and caps, so they cost this project nothing.
- **Review time.** The ceiling the repo itself names is a person reading drafts, not the machine.

### Decision triggers

1. Credits under about 30 days of runway: move to the cheaper host and tell the owner first.
2. More than 10 active workspaces: decide between publishing the Google client and an app-password path (MULTI\_TENANT\_PLAN §4, which I did not re-read).
3. Database over 5 GB: revisit backups and retention before any move.

## Phased roadmap

**Seven phases (10 to 16) fall into four bands, and a gate must be passed before the next band starts.** Phase numbers continue the repo's own count; Phase 10 is the one already pending there.

*(Diagram: roadmap — 4 bands, 3 gates. Bands: go-live and deliverability (P10–P11); job-hunt core and extension (P12–P13); scale and signals (P14–P15); connector and learning loop (P16).)*

Read left to right; each gate is a condition the earlier work has to meet, not a date. Dates are not set because the work is solo and credit-bound.

### Acceptance criteria

| Phase | Done when | Features |
| --- | --- | --- |
| P10 Go-live gate | A message from a connected `@arkvane.in` mailbox shows DKIM pass in Show original; an allowlist rehearsal sends only to listed addresses and records every message; the go-live check script returns GO; a booking link books | F1, F2 |
| P10 Backups (done) | Already true in the repo: a nightly dump is copied to S3 and a restore into a scratch database was drilled; nothing left to build | F2 |
| P11 Deliverability | A mailbox auto-pauses when its bounce or complaint reading crosses a configured limit and an alert is raised; a check reports SPF, DKIM, DMARC and one-click unsubscribe per sending domain | F3, F4 |
| P11 Queue hardening | Enqueueing the same job twice creates one row; a worker killed mid-job leaves nothing sent twice or lost; a workspace over its queue cap cannot starve another | F21 |
| P12 Job-hunt core | A pasted posting or a polled Greenhouse, Lever or Ashby feed creates a `job_posts` row; a fit score lists each match with its fact id; a draft claiming an unlisted skill fails the existing claim check | F5, F6 |
| P12 Shared company cache | A second workspace researching a cached company makes no search call; a structural test fails if company\_profiles gains a lead or org column; the ADR for the ADR-004 exception is merged | F19 |
| P13 Extension | On saved copies of three ATS forms, the extension proposes answers only from approved entries, flags unknown questions, and never submits; its token cannot read `/api/v1` | F8, F9, F10 |
| P13 Resume tailoring | A tailored resume containing a number or skill not in confirmed facts is rejected; each application points to an immutable resume version; the extension cannot set applied | F20 |
| P14 Scale sending | Two mailboxes share a campaign, a lead's follow-ups stay on its first mailbox, a signed booking webhook sets `MEETING_BOOKED` and a reply cannot | F7, F15, F17 |
| P15 Signals, graph | Every edge has a source; the brief cites each line; variant assignment is random and recorded on the message | F11, F12, F13 |
| P16 Connector, loop | `propose_draft` creates only `pending_review` drafts with `created_by='mcp:<user>'`; recurring edits are summarised and reviewed by a person before use | F14, F16 |

Each phase also keeps the existing guard tests green, with a new structural test per new router, and updates `PROGRESS.md` and `DECISIONS.md` in the same pull request, as the repo's conventions require.

## Open questions and sources

**Seven decisions are yours, and one limit on this research you should know about.**

### Decisions needed

- [ ] Build for yourself first, or for other users from the start? It decides whether the Google client is published and how much onboarding to build.
- [ ] Do you want the Chrome extension, and is a public store listing needed or is loading it yourself enough?
- [ ] Which provider keys do you already hold (Apollo, Hunter, Snov)? They set what F9 can do for free.
- [ ] Will you take legal advice on the lawful basis for B2B cold email under the DPDP Rules before widening beyond your own use?
- [ ] Should the sending domain stay `arkvane.in`, or should a separate look-alike domain carry cold mail?
- [ ] Is the shared public-company cache (F19) acceptable as an exception to per-workspace data isolation, given it holds public facts only? It needs its own ADR.
- [ ] Does the legal advice you take also cover pooling hashed bounce and opt-out lists (F23)? Until it does, nothing is pooled.

### What I did not verify

- **Competitor internals.** No source I opened describes any vendor's queue, storage or tenancy, so the architecture comparison is inference, marked as such.
- **Not opened, names only.** Teal, Jobright, LazyApply, LoopCV, Amplemarket, Reply.io, Outreach, Salesloft, 11x, Artisan, Lavender, Naukri, Instahyre, Cutshort and Wellfound appeared only in search listings or in no listing, so I make no claims about them.
- **LinkedIn's terms.** The help-centre page blocked automated fetching; the policy stance in this document rests on the repo's hard rule 4 and general knowledge.
- **Prices.** Third-party figures only; none from a vendor's pricing page.
- **Repo currency.** I read a local clone at commit `b3572eb`; the live branch may be ahead.
- **Endpoints.** The ATS feed URLs come from one third-party article and must be tested before use.

### Sources opened (2026-10-29)

| Topic | Page |
| --- | --- |
| Instantly infrastructure | [Salesforge blog on Instantly](https://www.salesforge.ai/blog/instantly-email-infrastructure) |
| Smartlead rotation and plans | [Octave on inbox rotation](https://www.octavehq.com/post/smartlead-inbox-rotation-multiple-senders), [BounceZero review](https://bouncezero.io/blog/smartlead-review-2026) |
| Clay waterfall | [Clay University lesson](https://university.clay.com/lessons/enriching-company-data) |
| Apollo data sources | [Apollo Data Overview](https://knowledge.apollo.io/hc/en-us/articles/19331318468621) |
| lemlist features | [lemlist alternatives page](https://www.lemlist.com/lemlist-alternatives) |
| Job tools | [Huntr vs Careerflow](https://huntr.co/blog/huntr-vs-careerflow), [Simplify Copilot help](https://help.simplify.jobs/articles/1749022-installing-and-setting-up-copilot) |
| Public ATS feeds | [Cavuno survey](https://cavuno.com/blog/ats-platforms-public-job-posting-apis) |
| Gmail and Yahoo rules | [Customer.io summary](https://customer.io/learn/deliverability/email-sending-requirements-gmail-yahoo) |
| Chrome policy | [Manifest V3 requirements](https://developer.chrome.com/docs/webstore/program-policies/mv3-requirements) |
| DPDP Rules | [Mondaq, 20 Nov 2025](https://www.mondaq.com/india/data-protection/1708164/digital-personal-data-protection-rules-2025-notified) |
| Supabase OAuth audience issue | [supabase/auth#2610](https://github.com/supabase/auth/issues/2610), as cited in the repo's ADR-028; not opened here |
| Your system | `outreach-automation` repo: `PROGRESS.md`, `DECISIONS.md`, `CAMPAIGN_KINDS.md`, `DATASET_DESIGN.md`, `MCP_PLAN.md`, `ARCHITECTURE_TARGET.md`, `models.py`, `mcp/tools.py` |
