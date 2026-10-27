# Campaign kinds: `outreach` and `job_hunt`

Two jobs, one engine. An agency selling projects and a candidate looking for work are doing the
same mechanical thing — import people, find something true about them, write, get a human to
approve it, send on a safe schedule, handle replies and follow-ups — and almost nothing about
that machinery differs.

**This document is the analysis, written before the design.** The question it has to answer is
not "how do we add job hunting" but "what is genuinely different", because everything that is not
genuinely different must stay shared. A forked pipeline would double every future bug.

---

## 1. What is actually different

| | `outreach` | `job_hunt` |
|---|---|---|
| **Who is sending** | an agency or team, with an offer | one person, with a history |
| **What may be claimed** | services, proof points, case studies | skills and experience **present in the profile** — nothing else |
| **Who is receiving** | a decision maker who can buy | a founder, hiring manager or recruiter who can hire or refer |
| **Extra target field** | — | the job posting URL, when there is one |
| **What is being asked for** | a meeting about paid work | a conversation, a referral, or an interview |
| **Daily cap default** | 100 | **25** |
| **Follow-ups default** | 1 | 1 (ceiling 2) |
| **Attachments** | off; a link is preferred | off; a résumé **link** is strongly preferred |
| **Opt-out wording** | commercial: "reply and I won't email again" | personal: "if you'd rather I didn't follow up, just say" |
| **Funnel** | sent → replied → meeting → proposal → won | sent → replied → screen → interview → offer |

### Why the lower cap

A candidate sends from a personal Gmail with no domain reputation, no SPF/DKIM alignment of their
own, and no warm-up history. The agency case at least has a domain whose reputation is an asset
worth building. 25/day is not a product opinion about ambition; it is the volume at which a
personal account does not look compromised. The warm-up ramp (§9.2) still applies on top.

### Why the grounding rule is stricter, not looser

`outreach` already forbids inventing facts about the sender (hard rule 6). For `job_hunt` the
same rule bites harder, because the failure is worse: an email claiming three years of Kubernetes
that the candidate does not have is not a weak email, it is a lie told in their name that they
will be asked about in an interview. So the drafter may assert **only** what is in the profile —
no inference, no "clearly you also know", no rounding two years up to three.

The good news is that this needs no new machinery. The existing claim check already requires
every assertion to cite an evidence id that resolves (`_check_claims_resolve`), and the evidence
corpus is already a closed set. The candidate's facts simply become that set.

---

## 2. What is deliberately identical

Everything else, and this is the longer list: import and dedupe, research, the review queue and
its evidence panel, the state machine, the send window and cap and pacing and jitter, threading,
suppression and opt-out handling, reply classification and its action table, follow-up scheduling,
the outcome dataset, every safety gate. A `job_hunt` email goes through `SEND_MODE`, the
allowlist, the daily cap and human review exactly as any other.

**Content modes are not forked either.** `campaigns.mode` already distinguishes `research` (read
the company, write a grounded email) from `template` (the operator's own text with merge fields,
no model). That *is* "researched draft" and "fixed draft", and both kinds get both. The only
difference is what the UI asks for and what the defaults are:

| | `research` | `template` |
|---|---|---|
| `outreach` | company research → grounded draft citing hooks and proof points | agency's own copy, merge fields |
| `job_hunt` | company/role research → draft citing hooks and **profile facts** | candidate's own covering letter, merge fields |

So `kind` and `mode` are **orthogonal**: four combinations, one pipeline, no new concept.

---

## 3. How the sender is modelled

`Persona` (today's, unchanged) already does the agency case and is wired into the drafter, the
validator, the footer and the claim check. `CandidateProfile` is a different *input form* for the
same contract:

| Profile field | Becomes |
|---|---|
| `full_name` | `sender_name` — signs the email |
| `headline` ("Backend engineer, 4 years") | `sender_role` |
| `facts[]` — each a single checkable statement | `proof_points[]`, ids `pp1`, `pp2` … |
| `target_role` | `offer`, phrased as what they are looking for |
| `links` (portfolio, GitHub, résumé) | the one permitted link |
| `goal` / `cta_text` | unchanged |

**`facts` carry the same stable ids as proof points on purpose.** It means `evidence_corpus`,
`resolve_evidence` and `_check_claims_resolve` work untouched, and a `job_hunt` draft that invents
experience fails the same check, with the same message, as an `outreach` draft that invents a
statistic. One rule, enforced in one place, for two kinds.

`agency_name` is empty for a candidate, and the footer already skips an empty agency line.

---

## 4. Validation differences

Only two, and both are about what the fields *mean* rather than new machinery:

1. **Required-before-drafting differs.** `Persona.missing_for_drafting()` (ADR-013) already owns
   this and already belongs to the drafter rather than to the data. `outreach` requires
   `sender_role`, `agency_name`, `offer`, `goal`. `job_hunt` requires `full_name`, `headline`, at
   least one `fact`, and a `target_role` — and must **not** require an agency, which is exactly
   the bug ADR-013 was written to fix for `template` mode.
2. **The link rule.** `outreach` permits one link and it must be the booking URL. `job_hunt`
   permits one link and it may be the booking URL *or* a profile link, because "here is my work"
   is the whole point of the email.

Everything else — forbidden phrases, unfilled merge fields, plain text only, subject length,
unsupported numbers — is shared and unchanged.

---

## 5. Compliance and tone

Both kinds are cold email to a person who did not ask for it, so both carry an honest sender
identity and a working opt-out, and neither uses tracking.

The difference is register. Commercial outreach is a business proposition and says so; a job
enquiry is a person asking another person for a conversation, and borrowed sales language reads as
insincere. That is a prompt and copy difference, carried in the kind's prompt fragment and
opt-out line — not a different compliance posture.

`job_hunt` is also less likely to be a regulated commercial communication in most jurisdictions,
and the system deliberately does **not** rely on that: the footer, the opt-out and the suppression
list behave identically. Claiming a lighter obligation would mean the one time the classification
was wrong, nothing would have protected the recipient.

---

## 6. Outcomes

`docs/DATASET_DESIGN.md` already carries a per-kind funnel (`stages_for_kind`), and the reply
taxonomy is shared: `has_vendor` means "we already have an agency" for one kind and "we have
filled the role" for the other — the same signal, which is why it is one label. `not_now` for a
candidate means "no headcount this quarter", which is the same shape of answer and the same thing
worth revisiting.

---

## 7. Migration

Existing campaigns become `kind='outreach'` and nothing about them changes: the column defaults to
it, the defaults for that kind are today's values, and a test asserts an existing campaign
produces byte-identical behaviour.

---

## 8. Deliberately not built

- **A second pipeline, or a second drafter.** The difference is configuration and a prompt
  fragment, and anything more would double every future bug.
- **Résumé parsing.** The candidate writes their own facts, which is the only version in which
  the grounding rule means anything — a parser inventing a fact would be inventing it on the
  candidate's behalf, and they are the one who gets asked about it in an interview.
- **A third kind** (fundraising, partnerships). Two kinds is enough to prove the seam is in the
  right place; a third should be added when somebody actually wants it.
