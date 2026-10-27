# Outcome dataset

What we record so that, later, we can answer *what actually worked* — and can tell **why** it
worked rather than only that it did.

Nothing here trains anything. This document and the tables it describes exist to make a decision
possible in six months that is impossible today, because the evidence was never kept.

---

## 1. The three principles this is built on

**A non-reply is not a "no".** It is *censored*: we watched for a while and nothing happened. The
difference matters enormously. Treating silence as rejection would teach any future model that
every email that did not get an instant answer was a bad email, when the commonest reason for
silence is that somebody was busy on Tuesday. So a lead with no reply carries `outcome = NULL`
and `censored = true` until the wait window passes, and only then becomes
`no_reply_after_window` — which is still recorded as *censored*, never as a negative class.

**A reply is not automatically good or bad.** "We already have an agency", "not this quarter",
"wrong person, talk to Priya" and "this is irrelevant" are four different results, and two of
them are good news. A single positive/negative axis would throw away exactly the distinctions
worth learning from.

**Separate the dimensions, or you cannot attribute anything.** Five independent things decide
whether an email works, and if they are collapsed into one row of "it worked / it didn't" the
dataset can only ever produce correlations:

| Dimension | What it answers | Where it lives |
|---|---|---|
| **Lead fit** | was this a sensible person to write to? | `leads` (sector, size, role), `research` |
| **Content** | was this a good email? | `drafts` (the AI version), `messages` (as sent) |
| **Timing / signal** | was this the right moment? | `messages.sent_at`, the trigger that prompted it |
| **Deliverability** | did it even arrive? | `messages.status`, bounce class, spam complaints |
| **Sender context** | whose reputation carried it? | mailbox, warm-up day, send mode, domain |

---

## 2. What already exists, and is reused rather than rebuilt

This is the important half of the design. The temptation with a dataset task is to build a
warehouse beside the application and copy everything into it, which immediately starts to drift
from the system it describes. Almost everything needed is already recorded:

| Already captured | Table | Note |
|---|---|---|
| Subject and body **as sent** | `messages.subject`, `.body_text` | the real artefact, not a reconstruction |
| The **AI draft** and the **human-approved version** | `drafts`, versioned | **an edit already creates a new version** rather than mutating, so both survive — this is the preference data and it was free |
| Which claims rested on which evidence | `drafts.claims` | ids into research hooks or persona proof points |
| Validator verdict | `drafts.validation` | why a draft was refused |
| Prompt version | `drafts.prompt_version`, `classifications.prompt_version` | |
| LLM provider and model | `llm_usage.entry_name`, `.task`, tokens | joinable by `lead_id` |
| Reply label, confidence, reasoning | `classifications` | written **before** anything acts on it (§10.3) |
| Every state change, append-only | `events` | the audit spine |
| Send time, status, threading | `messages` | |

**Three things are genuinely missing**, and only those are added:

1. **The lead's outcome** — the slow-moving result, distinct from any single reply.
2. **Which A/B arm a send used**, and the warm-up day it went out on.
3. **An explicit link from a human-edited draft back to the AI draft it came from.**

---

## 3. Reply taxonomy

The system already has a ten-label `ReplyLabel` enum (`app/domain/reply.py`), each mapped to an
action, and that enum is load-bearing: it decides suppressions and opt-outs. It is **extended, not
replaced** — a parallel taxonomy would be a second source of truth about the same email.

| Label | Definition | Example | New? |
|---|---|---|---|
| `interested` | wants to continue; asks for a call, time or detail | "Sure, how does Thursday look?" | existing |
| `question` | engaged but asking something first | "Do you work with Series A companies?" | existing |
| `objection` | a specific stated obstacle | "Too expensive for us" | existing |
| `not_now` | positive-to-neutral, wrong timing | "Revisit after March" | existing |
| `not_interested` | not relevant to them | "Not something we need" | existing |
| `has_vendor` | already served by somebody else — **not** the same as not interested; it is a timing and displacement signal | "We already work with an agency" | **new** |
| `wrong_person_referral` | not them, but points elsewhere — **good news** | "Talk to Priya, cc'd" | existing |
| `opt_out` | asks not to be contacted | "Remove me" | existing |
| `out_of_office` | automated absence | vacation responder | existing |
| `hostile` | angry, abusive, or threatening to report as spam | — | **new** |
| `bounce` | delivery failure; hard vs soft already distinguished at ingest | — | existing |
| `other` | unclear; a person decides | — | existing |

`objection` additionally carries `objection_kind`: `price` | `timing` | `trust` | `fit` | `other`.
Splitting it into separate labels would have changed the action table, which decides suppressions;
a sub-type cannot.

**`meeting_booked` is deliberately not a reply label.** It is set only by a human click or a
booking webhook and never inferred from a positive reply — a locked product rule since Phase 1.
It belongs to the outcome, below.

### Per kind

`outreach` and `job_hunt` share every label. What differs is the **funnel stage** they map onto:

- `outreach`: `sent → replied → meeting → proposal → won`
- `job_hunt`: `sent → replied → screen → interview → offer`

One `stage` column, validated per kind, so a cross-kind query still works and a per-kind funnel is
one `WHERE`.

---

## 4. `lead_outcomes` — one current result per lead

One row per lead, updated in place, with `events` remaining the append-only history of how it got
there. (A second append-only table beside `events` would duplicate the audit spine.)

| Column | Why |
|---|---|
| `lead_id` **UNIQUE** | one standing answer per lead |
| `campaign_id`, `campaign_kind` | the kind is denormalised **on purpose**: a campaign can be deleted, and an exported row must still say what kind it was |
| `outcome` | NULL until resolved. Never guessed |
| `censored` | **true means "nothing had happened by `observed_until`"**, which is not a negative |
| `stage` | funnel stage reached, per kind |
| `objection_kind`, `not_now_until` | only meaningful for their label; `not_now_until` is what makes "revisit in March" actionable |
| `confidence`, `confirmed_by` | `system` or a person's address. A human confirmation outranks a model's and must be distinguishable |
| `first_sent_at`, `resolved_at`, `observed_until` | the survival-analysis triple: when the clock started, when the event happened, how long we watched |

The `Scheduler` resolves `no_reply_after_window` once `observed_until` passes — the same hourly
pass that already closes silent leads, so no new moving part.

---

## 5. Experiments

`experiments` (per campaign: name, arms, `exploration_share`, active) and `messages.variant`.

Assignment is a **deterministic hash of (lead_id, experiment_id)** so a lead stays in its arm
across a sequence — an A email followed by a B follow-up would make the result uninterpretable.
A small `exploration_share` is assigned at random instead, so there is always a slice that is
causal rather than merely correlated.

---

## 6. Privacy and compliance

- **Minimise.** No new copy of any lead's text: the dataset joins to the rows that already exist.
- **Per org.** Every table carries `org_id` (ADR-004). There is no cross-org query, anywhere.
- **Pooling is opt-in and currently unused.** `orgs.dataset_pooling_opt_in` defaults to `false`
  and no code reads it yet. It ships now so that pooling can never be switched on retroactively
  over data gathered without consent — the flag has to predate the data.
- **Export hashes identifiers.** Email, name and company are SHA-256 with a per-export salt; the
  lead's own text is excluded unless explicitly requested.
- **Erasure keeps the anonymised features.** `erase_lead` already removes the person; the outcome
  row keeps the non-identifying features and loses the FK. A deletion request must not silently
  destroy the fact that *an* email of this shape got *this* result.
- Retention follows `LEAD_RETENTION_MONTHS`, as everything else does.

---

## 7. Export

`python -m app.scripts.export_dataset --org <id> --out <path>` writes versioned JSONL with a
`schema_version` on every row and a manifest naming the code revision, the window and the row
count. Reproducible: re-running it over the same window produces the same file.

Parquet is deliberately **not** added — it would mean `pyarrow`, tens of megabytes of dependency,
for a dataset that is tens of thousands of rows. JSONL is readable by everything, diffable, and
costs nothing to carry.

---

## 8. Deliberately not built

- **Any training, or any model that consumes this.** Far too little data.
- **A separate analytics database.** The data is small; a second store is a second thing to keep
  in sync and a second place for personal data to live.
- **Open and click tracking.** Still banned (§5.4): it hurts deliverability and privacy, and it
  would be the easiest signal to add and the one most likely to be mistaken for interest.
