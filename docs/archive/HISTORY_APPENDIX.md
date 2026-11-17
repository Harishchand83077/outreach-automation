# Implementation History: Appendix (ADR Notes Post-v3)

Extracted from `docs/PROGRESS.md`. Contains implementation notes appended after the main v3 plan: ADR-011 (research cache key), ADR-012 (template mode), ADR-013 (persona requirements), import undo, reply engine fixes, architecture review (ADR-014/015), dashboard redesign, follow-up threading, Postmaster monitoring (ADR-029), MCP smoke check.

---

**The research cache was keyed on the wrong domain, and one company's findings could be mailed
to another (ADR-011).** Asked as a storage question — keep `domain_research` to save Tavily
credits and tokens, or flush it after 30 days? Measuring it answered a different and worse
question.

**The key was `leads.domain`, which is the domain the founder writes *from*, not the company's
site.** A founder on a free-mail provider is imported with a warning rather than rejected —
deliberately, because founders do use Gmail — so that column is routinely `gmail.com`. Every
Gmail-addressed lead in an org therefore shared **one** `domain_research` row keyed `gmail.com`,
and the second such lead was served the **first company's findings**, which the drafter goes on
to cite as fact. An email to one company describing another is the worst output this system is
capable of, and it was one shared key away. The key is now the registrable domain of
`company_url`, which fixes both directions: unrelated companies never collide, and two founders
at one company are one fetch however either of them happens to write to us.

The same root cause was spending Tavily credits on nothing: the search fallback queried
`f"{lead.company_name or lead.domain} company news"`, so a nameless free-mail lead searched for
*"gmail.com company news"* out of a budget of 1,000 a month.

**On the storage question itself: it is not a tradeoff.** `evidence_text` is capped at 20,000
characters and nulled after 30 days; what survives is ~2 KB of findings and sources. At 100
emails a day — about 1,100 new companies a month in the worst case where no two leads share one
— keeping every company's research **forever** costs ~3 MB a month, **40 MB a year**, against 23
GB free, plus a constant ~22 MB for the rolling month of un-purged text. Three orders of
magnitude from mattering.

**And the 30-day expiry was the worst of both.** Nothing has ever deleted a `domain_research`
row — the retention sweep only nulls the text and lead erasure sets the FK to NULL — so the
expiry saved no storage at all. It only stopped the reuse.

**What actually constrains reuse is staleness, and it is not uniform.** What a company does, and
what it is built with, reads the same in April as in January. What it announced last week does
not, and a cold email opening on a stale "saw you're hiring two backend engineers" is *worse*
than one that opens on nothing, because the recipient knows whether it is still true. A single
expiry date cannot express that. So `CACHE_DAYS` is 90, rows are kept, and **hooks age rather
than rows**: past 30 days a hit keeps only its durable hooks, `news`/`hiring`/`launch` are
dropped with a note in `gaps` so the reviewer can see it happened, and **if that leaves no hooks
the row is treated as a miss** and the company is researched again — the durable findings alone
are not worth an email.

**The campaign form did not offer the field its own validator required.** Creating a campaign
failed with *"set booking_url or cta_text: a draft needs a concrete next step to ask for"*. The
rule is right and stays. The form offered no `booking_url` field at all and labelled `cta_text`
*optional*, so the obvious way through the screen was refused by a sentence naming a field that
was not on it. Both are now offered under a heading saying to fill in at least one, the pair
rule is checked in the form as well as the API, and `booking_url` is omitted rather than sent
empty — `''` is not a URL and would have traded the error for a different one.

**A campaign now has a mode, so this is two tools (ADR-012).** `research` is unchanged: read the
company's site, find something specific and true, ask a model for a grounded email. `template`
is new: the operator writes the email once with merge fields, and each lead's own details fill
the blanks. No research and no model for a first email or a follow-up, which makes a sheet of a
thousand drafts a matter of seconds rather than of a free tier's daily model limit. Replies stay
with a model in both modes, because a reply has to answer what the person actually said.

**The mode changes who writes the email and nothing else about what happens to it.** Both
drafters return the same `DraftResult`, so a rendered email is stored as a draft row, approved by
a person, and sent under the same cap, window, suppression check and `review_all` rule. A test
asserts a template lead still cannot reach `SENT` without a human.

The renderer's rules, and the one that matters most: **an unresolved placeholder never reaches a
recipient.** `Hi {{first_name}},` is the most recognisable failure of mail-merge and `Hi ,` is
the second — both worse than a generic opening, because both announce that nobody read it. So a
placeholder resolves, or has a fallback like `{{first_name|there}}`, or the render fails and the
draft is stored unsendable naming the field. It is checked twice independently: at substitution,
and again by the existing placeholder check reading the finished text. Substitution is one pass,
so a spreadsheet cell containing `{{booking_url}}` renders as those characters; nothing is
evaluated; and another tool's syntax (`{first_name}`, `%NAME%`, `${name}`) is refused when the
template is *saved* rather than when it is sent.

**Validation is narrower for a rendered template, on purpose.** `validate_draft` mostly catches
*invention* — unresolved claims, numbers absent from the evidence, a subject over six words —
and none of that applies to text a person typed. Hard rule 6 forbids the **drafter** inventing
facts about the sender; an operator is the authority on their own offer, and failing their email
for saying so would be the system refusing to send what it was asked to send. What still applies
is the shape of the message, because that is about the recipient: merge fields, forbidden
phrases, plain text, one link and only the booking URL.

**Two real gaps found while wiring this up, and neither has ever worked.** Both are the same
shape as every other defect in this project — a correct table and a correct consumer with
nothing producing the thing in between:

1. **No follow-up draft is ever created.** The send loop looks for an approved draft of kind
   `followup`, `domain/scheduling` computes when one is due, `SendLoop` sets `next_action_at`
   after a first send, the state machine has `SENT -> FOLLOWUP_SENT`, and `CampaignSettings` has
   `followup_after_days` and `max_followups` — and **nothing anywhere writes a
   `Draft(kind="followup")`.** A grep for it across `app/` returns only the constant and its
   consumers. So the locked product rule "one follow-up three calendar days after the first
   email" has never once happened. It is invisible so far only because the first real sends were
   two days ago.
2. **Nothing ever sets `NO_RESPONSE`.** `next_silent_close` computes the date; no code writes the
   state. A lead that never replies stays `SENT` for ever, so `final_wait_days` does nothing and
   the lifecycle has no end.

Both are the next change rather than this one: the `Scheduler` gains a pass that queues follow-up
drafts when they come due and closes silent leads, and the follow-up drafter serves both modes —
`generate_draft(kind="followup")` already exists for research campaigns, and
`CampaignTemplates.followups` for template ones.

**A draft that failed validation could not be rejected, so the lead had no way out.**

```
draft 452f5d21-dd3b-4ad0-b7a7-45552a935853 is validation_failed, so it cannot be rejected
```

A failed draft is shown in the review queue deliberately — a reviewer has to be able to see that
a weak model produced something unusable, rather than wonder where the lead went. But
`validation_failed` was not in `LIVE_STATUSES`, so both `approve` and `reject` refused it. The
only control the screen offered was the one the API would not do, and the screen even promised
*"reject it with a note and it will be rewritten"*. **Nothing available to a reviewer could move
that lead.**

`JUDGEABLE_STATUSES` is now the question that was actually being asked. `validation_failed` is
judgeable; the statuses that stay shut are the ones where the decision has already been made —
`approved`, `rejected`, `superseded`, `sent` — where acting again means the queue on screen is
stale.

**Approving a failed draft requires an edit.** Not because a human needs permission:
`approval_mode = review_all` already means a person decides, and an edit to an *accepted* draft is
unvalidated too, deliberately, because the reviewer is the authority on what leaves. It is because
the validator objected to something concrete in *this text* — a forbidden phrase, a claim resting
on nothing, an unfilled merge field — and the reasons are on screen beside the button. Approving
it untouched is a reviewer clicking past a warning rather than answering it, so Approve stays
disabled until something changes, which turns "I accept the risk" into "I fixed it". Bulk approval
still skips failed drafts entirely: the failure mode of a bulk button is somebody clearing a
hundred without reading any, and the weak ones are exactly the ones worth reading.

**And a second defect behind it.** A failed draft leaves the lead in `DRAFTED`, because the
handler only moves it to `AWAITING_APPROVAL` once a draft passes — so approving a reviewer's
fixed-up version died on `DRAFTED -> APPROVED is not a legal transition`, *after* the edit had
been written. The missing step is written rather than the table widened: `DRAFTED -> APPROVED` as
a legal move would let anything skip review, and the two-step version is also what really
happened — a person was shown this draft, and then approved it. That is the fourth time this
project has hit a transition the table did not describe, and the second time the right answer was
to add the step rather than loosen the rule.
**The first reply this system ever received went to Spam, and the reply engine could not see
it.** Reported as: the lead replied to both addresses, the dashboard shows nothing, and in the
Gmail account the reply is in spam.

`ImapLoop` polled `FOLDERS = ("INBOX",)`, with a comment saying Spam was worth adding *"once a
real campaign has run, because replies do land there"*. A real campaign ran; the first reply it
produced landed in Spam; and the loop could not see it. The dashboard showed no reply, which is
indistinguishable from no reply having been sent — and `DEFAULT_FOLDERS` in `imap.py` carried the
same comment above the same INBOX-only tuple, and was referenced by nothing at all.

**Why it was in Spam is the sending-identity problem, arriving as predicted.** The message went
out as `From: rahul@arkvane.in` authenticated as an unrelated Gmail account, so it carried no
SPF or DKIM alignment for `arkvane.in` — `GO_LIVE.md` §1 records this, and the owner chose to
defer it. An unauthenticated message to Gmail is a spam-folder message, and a *reply* to one
inherits that judgement. Polling Spam makes the reply visible; it does not make the mail
authenticated, and at real volume unauthenticated mail is not a spam-folder problem but a
suspended-account one.

**The folder is discovered, not hardcoded.** Gmail calls it `[Gmail]/Spam` in an English account
and translates it otherwise, so a hardcoded name is correct for exactly one locale. The loop asks
the server (`LIST`, RFC 6154's `\Junk` attribute), falls back to the names providers actually use,
and **only ever polls a folder the server listed** — polling one that does not exist is an error
on every cycle for ever, which is a worse failure than not reading spam. The parsing is a pure
function, `junk_folder_from_list`, so the part that actually breaks is testable without an
account. The list is cached for six hours, because a `LIST` is a login and the provider counts
those.

**Reading out of Spam is safe here.** A reply is matched to a lead by `In-Reply-To`/`References`
against a `Message-ID` we generated, before any model is called, so unrelated junk costs one row
and nothing more — already demonstrated when 1,400 unrelated messages were ingested from a
personal inbox and classified none of them.

`ImapLoop` had **no test at all**, which is how it came to read one folder in production. It has
five now, including one asserting that a *paused* mailbox is still read: pausing stops sending,
deliberately, and an opt-out we refused to read because we had stopped sending is the one we
would breach on starting again.
**A campaign with an unreadable persona crash-looped the send loop, and it was my regression.**
Reported as "test campaign 2 is running, I approved a draft, nothing sent, and it says something
about the persona" — while a draft approved in the *first* campaign on the same mailbox went out
fine.

PR #67 gave each candidate its own campaign so that a lead could not go out signed by a different
campaign. It did that with two **lazy properties**, `Candidate.persona` and `Candidate.settings`,
which called `model_validate` when read — and they are read inside `send_one`, *after* the
candidate has been chosen, with no `try` between there and `WorkerTask.run`. So a campaign whose
persona was missing a field raised, the error escaped `tick()`, the task recorded a failure and
backed off, and **no mailbox sent anything at all**, with the reason visible only as a traceback.
Which campaign's lead happened to be chosen first decided whether anything went out that cycle,
which is exactly the behaviour the owner described.

**That is the identical shape as the `send_days` incident that produced per-mailbox reporting in
the first place**, and a lazy property put it straight back. The lesson generalises better than
the fix: *validation that happens where the failure cannot be contained is the bug, not the
invalid data.* Both values are now resolved once inside `find_candidates`, in the same guarded
block that already handled unreadable settings, and a campaign that cannot be read contributes no
candidates while the others have their turn.

**The fallback was the other half of it.** `send_one` resolved
`chosen.persona if chosen.campaign.persona else persona`, so a campaign with an *empty* persona
borrowed the mailbox's first campaign's identity — the very thing PR #67 was written to stop,
surviving in the one branch meant to handle its absence. `send_one` no longer takes a `persona`
parameter at all: a message is signed by the campaign the lead belongs to and by nothing else, so
the only correct source is the chosen candidate, and passing one in is what made the mistake
expressible. `SendLoop._send_for` no longer resolves a persona either, and no longer returns
early when one is missing — it reads the mailbox-level defaults for the cap's day boundary and
falls back to the product defaults, because one campaign's bad row is not the mailbox's problem.

**Why 1,290 tests could not see it: the fixture created a campaign the API would refuse.** The
shared `campaign` fixture had `persona={}`, and every send test passed its own persona straight
into `send_one` — so the campaign row never needed one, and the production path, which reads the
row, was never exercised. The fixture now carries a persona, which is what `POST /campaigns` has
always required. Four tests were added against `send_pass` rather than `send_one`, because the
crash-loop lived in the layers between them: the same lesson as PR #54, which is now the second
time a defect has hidden in exactly that gap.

**And the dashboard could not have told anyone.** The worker's reasons go to the log and nowhere
else, so a campaign whose persona the send loop could not read showed a running status, an
attached mailbox and approved drafts, with no explanation anywhere. `GET /campaigns/{id}` now
returns **`blockers`**: every reason an approved draft will sit still — no mailbox, not running,
paused, an unreadable persona, unreadable settings, unreadable templates, the org paused, the
send mode disabled — each computed **from the same Pydantic models the worker validates with**
rather than from a second set of rules that would answer differently the first time either side
changed. Field names only, never stored values: this travels to a browser and into screenshots.
The campaign screen lists them, replacing two hand-written banners that covered the two causes
somebody had thought of.

**One more defect fell out of writing that test.** `campaign_summary` rendered the settings
through `CampaignSettings`, so an unreadable row made it raise — turning `GET /campaigns/{id}`
into a 500 and, because the same function builds every row of the list, taking out the campaigns
list for every healthy campaign beside it. **The one campaign an operator most needs to open was
the only one they could not.** It falls back to the product defaults for display now; the fault is
reported by `blockers`, and the detail route still returns the row exactly as stored, because the
settings form edits what is actually there.

**Template mode asked for an agency and an offer from people who have neither (ADR-013).** The
owner's second use for this is job hunting, and `Persona` required `sender_role`, `agency_name`,
`offer`, `goal` and a booking link or an ask — so the only way through the form was to type
something that passes. **A persona filled in to satisfy a validator is a persona the drafter
would go on to quote**, so the rule meant to stop invention was requiring it.

Those requirements are real, and they belong to the **research** drafter: it asks a model to
write on our behalf, so it has to be told who that is, or the model makes it up (issue A3, the
worst thing the prototype did). They were expressed as model validators, which made them a
property of the data instead — and so they applied to a campaign that never calls a model.

`sender_name` stays required: it signs the message and it is in the footer, so a recipient can
always tell who wrote to them. The rest moved into `Persona.missing_for_drafting()`, which
returns each missing field **with why it matters**, and is asked at the three places that need
it: creating or patching a `research` campaign, `handle_draft_lead` before the model is called,
and `blockers` so an existing campaign in that state says so rather than silently drafting
nothing. The footer already skipped an empty agency line, so nothing at send time changed.
**An import can be undone, and the reason it needed its own operation is the interesting part.**
Asked for as: what happens if someone uploads the wrong sheet?

Half of it already existed — `/imports/preview` writes nothing and the screen shows every row's
verdict before anything is committed — but there was no way to **abandon** a preview short of
choosing a different file, and no way back at all once committed. The preview now has a Cancel
button (which also clears the file input, or choosing the *same* file again fires no change event
and looks broken).

**The undo could not reuse `DELETE /leads/{id}`, and that is the decision worth recording.** That
route is the erasure path for a data-subject request, and it deliberately leaves a **hashed
suppression** behind — so that somebody who opted out and then asked to be forgotten cannot be
mailed again by the next import of the same sheet. Undoing a *mistaken upload* through it would
permanently block every address in the sheet: re-uploading the corrected one would skip exactly
the people it was meant to reach, and nothing on screen would say why. The test that proves an
undo suppresses nothing is the most important one in the file, and the one after it re-imports the
same address to show it.

`leads.import_batch_id` stamps each upload, returned by the commit so the dashboard undoes the
batch it actually made rather than hunting for "the most recent" — which would be the wrong one
for anybody with two tabs open. Leads imported before this column existed have NULL, which is the
truth: there is no record of which upload they came from, and inventing one would make an undo
delete rows it was not asked about.

**An undo does not unsay anything.** A lead that has been emailed is kept, whatever the operator
meant to upload — the record of what we sent to whom is the one thing here that must not be
editable, because a reply can still arrive, an opt-out has to be honourable and a bounce has to be
attributable. A message still `sending` counts as emailed, since ADR-002 is explicit that we never
assume such a message did not leave. So is a lead that has replied or bounced: something happened
to that person. The response says what was kept and why — *"Removed 9 lead(s). Kept 1: 1 already
emailed."* — rather than reporting a silent partial success.

**A proposed reply has never once been written, and the deploy's own logs said so.** Found by
reading the worker log straight after deploying everything above:

```
"job permanently failed" ... kind: "draft_reply", error: "job payload has no lead_id"
```

**Two callers queue a `draft_reply` job, with two different payload shapes, and neither matched
what the handler read.**

- `reply.service` queues on ingest with `payload={"message_id", "label"}` and puts the lead in
  the `jobs.lead_id` **column**. The handler read `payload["lead_id"]`. So **every reply to a
  real inbound message died**, permanently — a missing key is not retryable. The lead reached
  `REPLIED` and nothing ever proposed an answer, which from the dashboard is indistinguishable
  from a reply nobody has got to yet.
- `drafts.reject` queues a regeneration with `payload={"lead_id"}` and no `message_id`, so
  rejecting a reply draft would have died the same way on the other key.

Each side was individually right and individually tested. Nothing checked that they agreed —
which is now, unmistakably, the dominant shape of defect in this project: the rejection path,
`NEW -> RESEARCHED`, `RESEARCHED -> AWAITING_APPROVAL`, `DRAFTED -> DRAFTED`, research queuing
nothing, the free-mail cache key, and now this.

The handler resolves the job from whatever each caller does provide: the **lead from the
column** (every caller sets it, and it is the FK that cascades a deleted lead's work away), the
**message** from the payload or else the newest inbound message for that lead — which is what a
regeneration is answering — and the **label from `classifications`**, which is the authoritative
record, written before anything acts on it precisely so the answer does not depend on re-running
a model (issue B17). Reading it back beats storing a second copy on the draft that could
disagree with it.

Two things it still refuses, both permanently: a lead with no inbound message, and a message
with no classification. There is no safe default for a label — it decides whether a reply may be
drafted at all, so guessing could propose an answer to somebody who asked to be left alone.

**The new tests drive the real producers rather than a payload a test author wrote.** That is the
only shape of test that could have caught this: both sides were already covered in isolation.

**And the first reply draft ever written was then refused for ending its last sentence with a
full stop.** The message, from the live database:

```
the only link allowed is the booking URL https://calendar.google.com/calendar/u/0/r?pli=1,
not https://calendar.google.com/calendar/u/0/r?pli=1.
```

The two strings are identical except for a trailing `.`. `_URL` matches greedily, so a sentence
ending *"book here: https://cal.example.com/asha."* captures the stop as part of the URL, and
`_check_links` then compared it against the persona's booking URL and found a mismatch. A correct
draft, refused, with a message nobody could act on — which is the worse half: an operator reading
that has no way to tell what is wrong. `found_url()` takes the sentence's punctuation back off
before a URL is counted or compared. The rule is unchanged: one link, and it must be the booking
URL.

**Worth the owner's attention, separately:** the booking URL on that campaign is
`https://calendar.google.com/calendar/u/0/r?pli=1`, which opens *the reader's own* Google
Calendar. It books nothing. A recipient clicking it sees their own week. That is a persona value,
not a bug, and it should be a real booking link before any of this reaches a stranger.

**A failed draft stayed in the review queue beside its own replacement.** Seen on the live
dashboard the moment regeneration started working: one lead showing a `validation failed` reply
at v1 *and* a `pending_review` reply at v2, two rows for one lead, one of them unjudgeable.

`_supersede_live` retired the previous draft only when it was in a *live* status, and
`validation_failed` is not one — so every failure accumulated in the queue for ever, beside
whatever eventually succeeded. Keeping a failure visible is deliberate, so a reviewer can see
that a weak model produced something unusable rather than wonder where the lead went; keeping it
visible *after* a newer attempt exists is just noise in the one screen that must stay scannable.
It is superseded now. Nothing is lost: the row keeps its failures and `save_draft` writes a
`draft.created` event per attempt, so the lead's timeline still shows every version and why each
one failed.

**Approving a reply was a 500, and it was the third link in the same chain.** The operator saw
only *"Something went wrong. Quote the request id if you report this."*

`approve()` always moved the lead to `APPROVED`. A reply is sent from `REPLIED` and a follow-up
from `SENT` — `outbox._VALID_FROM` says so and the send loop checks it — so moving either to
`APPROVED` would make the message **unsendable by the very act of approving it**. It never got
that far: `REPLIED -> APPROVED` is not a legal transition, so it raised inside the route.

Only a first email moves the lead now. What makes a draft sendable is the *draft* being
approved; the lead's state says where the **conversation** is, and approving our answer does not
move the conversation — sending it does. A reply or follow-up approval writes a `draft.approved`
event instead, so a decision a person made is still in the trail.

Three defects in a row on one path, each hidden behind the last: the job was never queued in a
runnable shape, the one draft that got written was refused over a full stop, and the one that
passed could not be approved. Every one was a correct component meeting another correct
component on a contract nothing checked.

**And why `test_campaign`'s reply produced no draft at all: it is working as designed, and that
was invisible.** The reply was classified `other`, whose policy is "unclear — a person decides",
which deliberately proposes no draft. Five labels do that. Nothing recorded it, so the lead sat
in `REPLIED` with no draft, no job and no explanation an operator could see — indistinguishable
from drafting being broken, which it also was at the time, so the two could not be told apart. A
`reply.no_draft_by_policy` event now names the label, the action and the reason.

**The architecture review, and the two P0s it named are fixed (§27 of `ARCHITECTURE_TARGET.md`,
ADR-014, ADR-015).** §21's failure table was written before anything ran; §27 is written after a
deployment, three real sends, a real reply and sixteen production defects, and it says what the
code does rather than what it was meant to do.

**The two gaps that mattered are closed.** `app/services/lifecycle.py` is the producer that was
missing at both ends of a lead's life: it queues the follow-up drafts that have come due, and it
closes as `NO_RESPONSE` the leads that have had everything we were going to send and said
nothing. Neither had ever run. The follow-up is drafted and reviewed like any other email — it is
not auto-sent — and a template campaign renders the operator's own follow-up text.

**On adding RabbitMQ: no, and the reason is structural rather than a preference (ADR-014).**
Every job here is enqueued *in the same transaction as the state change that justifies it*, which
is why a crash anywhere loses no work and duplicates none. A broker destroys that property —
`commit()` and `publish()` are two systems with no shared transaction — and the standard remedy,
a transactional outbox, is the table we already have. At 0.005 jobs per second we are five orders
of magnitude from `SKIP LOCKED`'s limit, and the ceiling we are actually near is the free-tier LLM
quota, which a broker does nothing about. **No new infrastructure.**

**Corrections to assumptions that were raised.** Reply matching is **threading-first** — our own
stored `Message-ID` against `In-Reply-To`/`References`, then a DSN's `Final-Recipient`, then
sender-plus-subject within 30 days as a bounded fallback — and quoted text *is* stripped before
classification. "Sender address only, no threading, no stored Message-ID" describes the
prototype (issues B10, B11) and has not been true since Phase 6. Soft bounces are already
distinguished from hard ones and deferred a day rather than suppressed. Bulk approval already
does not bulk send: it makes leads eligible, and the send loop still sends one message per cycle
under the cap with a jittered gap.

The rest of §27.8 is recorded rather than fixed: the importer's single-transaction commit, the
unstaggered fan-out of a large import against daily LLM limits, uncapped reviewer-driven
regeneration, no campaign scheduling, no dashboard route to requeue a dead job, and four minor
items. Each has a named fix.

**The §27.8 gaps are closed, and one of them was a correctness bug rather than a load concern.**

**The same person in two campaigns was sending two cold emails.** `UNIQUE(campaign_id,
email_norm)` is per campaign on purpose — the prototype's *global* unique key made re-importing a
corrected sheet silently do nothing (B16) — and the cost of that choice is that two campaigns can
hold the same person and both send. Worse, if they reply to one, the other sends anyway, because
it is looking at its own lead row and that row knows nothing about the conversation beside it.

The guard is now **per person per org**, in `claim_send` beside the suppression check, and it
covers exactly the scenario described: campaign A sends, A is paused, B runs with the same person
— pausing a campaign does not unsend its email. A live conversation anywhere in the org blocks a
cold approach whatever the date says, which is the reply-mid-switch case. Follow-ups and replies
are deliberately not checked: they belong to a thread already open with that person. It is a
**cool-down, not a suppression** — ninety days, after which a second approach reads as a fresh one.

Writing that test exposed something smaller and worth keeping: **`claim_send` caught every
`IntegrityError` and called it "already claimed by another worker"**, so a foreign-key violation
on `draft_id` reported a concurrent worker that did not exist. Only the idempotency key means
that now; anything else raises.

**The load gaps.** An import no longer makes every job runnable at once: `run_after` spreads
research over the days the leads could actually be emailed, derived from the campaign's own daily
cap with one day of head start. There is no value in researching a thousand leads today when at
most a hundred can be sent. Template imports are untouched — rendering is local, instant and
free, and a thousand drafts in seconds is the point of that mode.

**Orphaned inbound mail is no longer kept for ever.** Unmatched mail is stored on purpose so a
reply we failed to match is not lost, and `match_lead`'s fallback only looks back 30 days — so
after 45 an orphan is not a reply we might still place, it is somebody else's correspondence in
our database. That is a privacy surface before it is a storage one; the hourly sweep now deletes
them.

**Rejecting now has a limit (5 per lead per kind).** Each rejection is a model call, and a loop
would spend the day's free-tier quota on one lead while every other lead waits behind it. The
refusal says what to do instead rather than silently declining, which would look like the
rejection button being broken — again.

**A dead job can be requeued from the API.** `/admin/dead-jobs` lists them with `last_error`, and
`POST /admin/jobs/{id}/requeue` resets the attempts, because the retries were spent on a
different version of the code. Three dead reply-drafting jobs were recovered by hand with SQL
after a deploy, which is what prompted this.

**`docs/OPERATIONS_QA.md` is new** and answers every question raised, by area, with file
references — crashes and at-most-once, deadlocks and lock ordering, LLM rate limiting and
exhaustion, throttling at four levels, reply threading, bounces and opt-outs, WAL and MVCC and
the connection pool, dead letters, roles, worker liveness, the backup trade, and where the real
throughput ceiling is (human review, as §20 predicted). It marks what is **not** implemented as a
gap rather than describing it as if it worked, which is how most of this project's defects got
written down as features.

**The dashboard has a design system rather than a stylesheet.** It was readable and plain: one
weight of grey, seven identical stat tiles, "Loading…" as a loading state, and an empty state
that was a sentence in a box. Fine to operate, not something to put in front of a founder.

**Tokens first.** `index.css` is now a vocabulary — a neutral ramp, semantic colours with soft
backgrounds and readable text pairs, a type scale named by role, spacing, radii, shadows and
*motion* tokens. Two rules keep it honest: a component never uses a raw colour, and
`prefers-reduced-motion` is honoured in exactly one place rather than in forty. Nothing in this
app is communicated only by movement, so switching it all off loses nothing.

**Light and dark, with a toggle.** The same token names are defined twice, so no component asks
which theme is on. An untouched install gets the system's answer; once somebody chooses, the
choice is stored and wins. A dark-only dashboard demoed in a bright room is a bad first
impression, and this is three blocks of CSS.

**The review queue is now keyboard-driven**, which is the screen that decides whether this tool
is pleasant: `J`/`K` to move, `A` to approve, `R` to reject, `X` to select. Deliberately inert
while typing — an editor fixing a subject must be able to write "approach" without approving
three drafts on the way through. The focused card is marked and scrolled into view, and a
progress bar replaces two stat tiles that between them held two integers: how far through you
are is a shape, not a pair of numbers.

**Seven equal stat tiles became two groups.** Volume and outcomes answer different questions,
and giving them one visual weight made "Meetings booked: 0" look exactly as important as
"Leads: 2" — so the number anybody cares about was the hardest to find. Outcomes take a colour
only when non-zero: a red "Bounced 0" is an alarm about nothing.

**Smaller things that add up.** Icons in the sidebar and an identity block instead of two lines
of text; skeletons shaped like the content instead of the word "Loading"; toasts that carry an
icon so success and failure differ by more than wording; a focus ring on `:focus-visible` only;
a busy button that keeps its width so a row of controls does not reflow mid-click; hover lift on
list rows; and the destructive "Delete campaign" pushed away from "Pause sending", because a
destructive action a thumb-width from a routine one is one somebody will eventually hit by
accident.

**No UI library.** Twelve hand-written SVG paths instead of an icon package: they ship nothing,
cannot break on a major version, and are not a dependency to audit for the life of the project.
The CSS is ~700 lines and the whole bundle grew by 2 KB gzipped.

**Two things found by opening the redesigned dashboard rather than by reading it.**

**It said "No campaign yet" to somebody with two campaigns.** `campaigns` starts as `[]`, which
is indistinguishable from "this org has no campaigns" — so for the half-second before the fetch
returned, the screen stated a falsehood and offered a Create button. **A loading state that
asserts something false is worse than a spinner.** "Loaded" now means *we asked and heard back*,
including hearing an error, so a failed fetch does not leave a skeleton spinning for ever above
the real message. Every network request on that page was a 200 — the bug was entirely in what
the UI chose to say while waiting.

**The filter bar ran into the paragraph above it**, so the field labels read as the last line of
the explanation. It is its own surface now, which is what it always was.

**Four interactions that make the dashboard feel like a tool rather than a form.**

**A command palette (⌘K / Ctrl+K).** Every screen and every campaign, two keystrokes away. Built
from the same `nav` list the sidebar draws, so a screen added later appears in it without
anybody remembering to. Matching is a plain substring: fuzzy matching looks clever and ranks
badly on short lists — with eight commands and a handful of campaigns, typing "cam" should show
the campaigns, not a scored permutation of the whole app.

**Lead detail is a drawer, not a page.** It used to replace the list, so comparing two leads
meant navigating twice and losing the scroll position both times. The list stays where it was,
Escape closes, the body scroll locks behind it, and focus moves into the panel so a keyboard
user is not left on the page underneath.

**A real drop target for the sheet.** Dropping a spreadsheet is how anybody who has used one
expects to start. It is a `<label>` wrapping the real input rather than a `div` with an
`onDrop`, so clicking opens the picker and the keyboard still reaches it — a drop target alone
is invisible to anybody not using a mouse. One path handles the file however it arrives.

**A chart, because every number on the campaign screen was a snapshot.** "1 reply" says nothing
about whether a campaign is working, only that it once did. `GET /campaigns/{id}/activity`
returns sends and replies per day, **bucketed in the campaign's own timezone** — a send at 23:40
IST belongs to that operator's day, and bucketing by UTC would quietly move a sixth of India's
working hours into the previous bar. Empty days are returned as zeros rather than omitted,
because a sparse series is a chart with invisible gaps and a quiet week should look like one.

Drawn by hand in SVG: a charting library is ~50 KB and a styling argument for two series of
thirty points, and this inherits the theme toggle for free. Replies stack *on* sends rather than
sitting beside them — the question is how much of what went out came back, and two adjacent bars
make that a comparison the eye has to perform.

**A dangling section heading, found by looking at it.** The campaign screen rendered "Activity"
above the chart unconditionally, so when the chart declined to draw — no data yet, or a backend
that does not have the endpoint — a title sat over empty space, which reads as something broken
rather than something absent. Heading and chart together now, or neither.

**And the screen that found it confirmed something bigger: the follow-up has been sent.** Three
leads of `test_campaign` are in `FOLLOWUP_SENT` with the message in their timeline — *"quick
follow up"*, sent 17 October. The chain ran end to end for the first time: the lifecycle pass
queued it, the handler drafted it, a human approved it, the send loop sent it, and the lead
moved on. That product rule was written in Phase 5 and had never once executed.

**Follow-ups arrived as new conversations, not under the email they were chasing.** Reported
from a real mailbox, and it was two bugs wearing one coat.

**Every outbound message went out with no `In-Reply-To` and no `References`.** `build_message`
has always accepted both headers and `build_references` has always known how to extend a chain —
`SendLoop` simply never passed anything to `claim_send`. The signature defect of this project,
one more time: a correct producer and a correct consumer with nothing connecting them.

`outbox.thread_headers` now says what each kind threads onto. A **follow-up** continues our own
last message to that lead — the first email, or the previous follow-up. A **reply** answers
*their* last inbound message, not our own. A **first email** threads onto nothing. `References`
extends the parent's chain rather than restarting it, so the third message in a conversation
still names the first, which is how a client groups a thread when an intermediate message is
missing from somebody's mailbox. Nothing to thread onto is not an error: a follow-up with no
visible parent is still a useful email, and refusing to send it would be the worse outcome.

**The subject was the other half.** Clients group on `References` first but fall back to the
subject, and the follow-up drafter was inventing a fresh one — so even with headers, anything
falling back would still file it separately. A follow-up now inherits the thread's subject
prefixed `Re:`, decided at **drafting** time rather than swapped in at send time, because a
reviewer has to see the subject that will actually be sent. Replies have worked this way since
Phase 6; follow-ups did not. The validator was updated to match: a follow-up must now keep the
thread's subject, and is exempt from the six-word limit for the same reason a reply is — the
length was chosen when the thread started and is not ours to shorten. A *first* email still may
not begin `Re:`, because faking a reply to a conversation that never happened is a deception
recipients recognise instantly.

**`docs/DEPLOY_CHEATSHEET.md` is new** — the three commands that deploy the backend, how to get
a shell without SSH, how to tell what is actually running, what to do when it fails, and the
reminder that the dashboard needs no deploy at all because Cloudflare builds it on every merge.

**The chart deployed and was three times too tall.** `preserveAspectRatio="none"` means the
viewBox stretches to whatever box it is given — so `height: auto` derived the height from the
100×34 viewBox and produced a chart about 370px tall on a desktop, for two bars. A fixed 120px
is what that attribute was always implying. Caught by looking at it after the backend deploy
made it draw for the first time.

**Deliverability monitoring, and a smoke check for the live MCP endpoint (ADR-029,
`docs/POSTMASTER.md`).**

**First, the thing that was reported as missing and turns out not to be.** "I can't see any token
or anything in our website related to the Claude connector" — it is there, and it is live. The
panel is **Settings → Connect your own Claude**, between Team and the suppression list. Verified
rather than assumed, from outside: the published dashboard bundle contains the panel and its
`/mcp-tokens` calls, `GET /mcp` on the live API answers **405** and an unauthenticated `POST`
answers **401**, so both halves of PR #116 are deployed. What it needs is an owner or admin role,
because the whole Settings screen is gated — a `member` does not see the nav item.

**Postmaster Tools is set up by hand and deliberately not integrated.** Reputation attaches to
the **sending domain**, and after ADR-023 this deployment has no mailbox of its own — every
workspace sends from an account its owner connected, under its own domain. So there is no
deployment-wide view of deliverability and there cannot be one: reading a tenant's reputation
would need credentials for their domain. The product change is therefore **one sentence and a
link** beside the sending account, naming the workspace's own domains, rather than a panel whose
numbers would require holding an OAuth credential for every tenant's Google account to redraw a
dashboard Google already draws for them.

**Two findings that change what to expect of it, which is more useful than the setup steps.**

1. **It will show nothing at our volume, probably for months.** Google withholds low-volume data
   by design, and counts only mail to `@gmail.com` — a founder on their own Workspace domain does
   not appear at all. Against the locked 100/day/mailbox, warming up from 20, the qualifying
   subset is tens of messages a day against an unofficial threshold in the hundreds. **The reason
   to do it today is that it has no backfill**: verify after the first campaign and the first
   campaign is the one you cannot see. Precisely ADR-026's argument for the outcome dataset.
   Worth knowing for a second reason too — Google says data may also be withheld when reputation
   is *particularly bad*, so "no data" and "bad data" look alike from outside.
2. **The signal that works at any volume is already configured and read by nobody.** DMARC
   aggregate reports arrive daily from every participating receiver and are not volume-gated.
   They would show the `From`-versus-authenticated-account mismatch this repo has warned about
   since Phase 9 — mail sent as `rahul@arkvane.in` while SMTP authenticates as an unrelated
   personal Gmail — immediately and unmistakably, as a source failing both SPF and DKIM for our
   own domain. And `rua` points at `rahul@arkvane.in`, an address this system both **sends from**
   and **polls**, so the reports are ingested as orphaned inbound messages until the hourly sweep
   deletes them at 45 days. Harmless, because nothing matches a lead and no model is ever called
   on them, and still the owner's correspondence sitting in application data for no reason.
   `p=none` stays until the reports are clean: tightening first would have receivers quarantine
   our own real mail.

**`app/scripts/mcp_smoke.py` exercises the deployed endpoint over the public URL.** The 24
integration tests drive a `TestClient` against a test database in the same process, so they
cannot see TLS, Caddy, a forwarded `Origin`, or a route mounted at the root rather than under
`/api/v1`. Six defects surfaced on this project's first real deploy; every one was invisible
locally and in CI and four lived in exactly that gap. The check covers the half a happy path
never looks at — no credential, a garbage one, a **Supabase-shaped** one (ADR-028's two-doors
rule, asserted against the live deployment), a disallowed `Origin`, the legacy `GET` and
`DELETE` — then breaks each 2026-07-28 validation rule on purpose (`-32020` on a header that
disagrees with the body, `-32022` on an old version, `404`/`-32601` on an unknown method), then
`server/discover`, the catalogue and every tool in it. It also asserts on the wire that **no
tool's schema takes an `org_id`**, which is the one tenancy mistake invisible from inside a
single workspace, and that `tools/list` is ordered stably **across two calls** — the property
that replaced the assertion which compared a list with itself.

**It mints a real credential against the real database, which is the one dangerous thing about
it.** So two properties are enforced by `tests/unit/test_mcp_smoke_script.py` rather than left to
care: the secret is **never printed**, because this output is pasted into terminals and chat
windows and hard rule 2 has already been breached once here by a failing assertion; and the token
is **revoked in a `finally` block**, because the run this matters for is the one that fails, and a
check that cleans up only on success leaves a live key behind exactly when nobody is reading the
output. A one-day expiry is the third defence, for a process killed between the two. The guard
was run against the absent script first, to watch all six assertions error.

**And the tool list is read from the catalogue rather than restated**, asserted by that same
guard. A hand-written list inside the checker is a second copy of the thing being checked, which
is this project's signature defect wearing a lab coat.

**And it was run, which is the only reason any of the above is a claim rather than a hope.**
Against a local API on the dev database: **29 checks, all passing, exit 0, token revoked.** Two
things came out of running it that reading it would not have given:

- **The audit assertion was added because of what the run showed.** It is the only check here
  that reads the database rather than the response, and it exists because writing the audit row
  is deliberately wrapped and non-fatal — so a trail that has quietly stopped being written looks
  exactly like one that is working. It reports **7 events for 7 calls**, and three consecutive
  runs each reported 7 against a table holding 21, which is how the time window was shown to
  discriminate rather than to count everything.
- **mypy caught a column that does not exist.** The audit query was written against
  `Event.created_at`; the column is `Event.at`. The signature defect again — a literal that has
  to match the schema — caught by the type checker before commit this time rather than by a
  screen reporting zero for ever. Worth noting only because the previous two instances of it, in
  the platform router eleven lines apart, were found by reading a check constraint, and a tool
  that finds this class automatically is cheaper than the discipline that was needed last time.

The cosmetic finding: the minted token's prefix was printed with a Unicode ellipsis, which a
Windows console renders as a replacement character in the middle of a credential prefix. Plain
ASCII now — this output is read in a container shell and in a terminal, and a credential prefix
that looks corrupted is one somebody will think they mistyped.
