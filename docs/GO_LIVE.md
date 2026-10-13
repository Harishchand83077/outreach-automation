# Go-live

The gate between "it works" and "it is mailing strangers". Work down it in order; every step is
either a check you can run or a decision you make once and record here.

The automated half of this gate is `tests/e2e/test_go_live.py`, which walks a lead's whole life
against a real database, a real SMTP server and a fake model. It proves the refusals as much as
the sends. Everything below is the half a test cannot do.

---

## 0. Where things stand today

| | |
|---|---|
| API | `https://api.arkvane.in`, Caddy TLS, `/ready` green |
| Dashboard | Cloudflare Workers |
| Database | Postgres in Compose on the EC2 instance, no published port |
| Backups | nightly 02:17, 14 days local, copied to `s3://outreach-backups-918464541565/`, 30-day lifecycle |
| Restore | **drilled** 2026-10-13: restored into a scratch database, row counts identical |
| Send mode | `allowlist` from the environment, changeable by the owner from the dashboard (ADR-010) |
| Sent so far | 3 emails, to the owner's own addresses, delivered and recorded |

---

## 1. The sending identity — do this first

**This is the one item that is currently wrong, and it is deliberate.** The mailbox row says
`rahul@arkvane.in` while SMTP authenticates as a personal Gmail account. Gmail accepts the
mismatched envelope, so nothing bounces — but **Gmail will not sign `arkvane.in` mail**, so the
SPF and DKIM records verified for that domain do not cover anything sent this way.

For testing against your own inboxes that is harmless. Before mailing anybody else:

- [ ] `SMTP_EMAIL` is an address on the sending domain, and the mailbox row's `from_email` is
      **the same address**. A mismatch means unauthenticated mail against a domain that has
      records, which scores worse than a domain with none.
- [ ] Send one message to a Gmail address and open **Show original**. Require
      `SPF: PASS`, `DKIM: PASS`, `DMARC: PASS`. DNS cannot tell you this; only a received
      message can.
- [ ] If DKIM fails, press **Start authentication** in the Workspace admin console. Publishing
      the record is not the same as switching it on.

**Do not run cold outreach from a personal Gmail account.** Google's bulk-sender rules apply to
the account, not the domain, and the usual outcome is a suspended mailbox and a lost address
rather than a warning.

### DNS, checked from outside

```bash
dig +short TXT arkvane.in                      # exactly ONE spf1 record
dig +short TXT google._domainkey.arkvane.in    # the DKIM public key
dig +short TXT _dmarc.arkvane.in               # p=none with rua= to start
dig +short MX  arkvane.in
```

- [ ] Exactly one SPF record. Two is not stricter, it is a permanent error.
- [ ] DMARC starts at `p=none` with reporting, and moves to `quarantine` only after the reports
      are clean for a fortnight.

### Worth deciding now, not later

A **separate sending domain** (`arkvane-mail.in`, say) keeps cold-mail reputation away from the
domain your clients see. It needs its own warm-up, so the time to start is before volume, not
after a problem. See QUESTIONS.md Q4.

---

## 2. Rehearsal, in `allowlist`

Already done once: three leads went out, were delivered, and were recorded `SENT`. Repeat it
after any change to the sending identity.

- [ ] `SEND_ALLOWLIST` contains only addresses you own.
- [ ] Send mode is `allowlist`. Check it on the Settings screen, not from memory.
- [ ] Import a small sheet, let research and drafting run, and **read the drafts**. The point of
      the rehearsal is the content, not the plumbing.
- [ ] Approve one. Confirm it arrives, is plain text, carries `List-Unsubscribe`, and shows the
      right From name.
- [ ] Reply to it from the recipient account. Confirm the reply appears against the lead and
      that `next_action_at` is cleared — a follow-up sent to somebody who already replied is the
      most visible failure this system has.
- [ ] Reply `unsubscribe` from another. Confirm the address lands on the suppression list.
- [ ] Confirm a lead's timeline reads correctly end to end on the Leads screen.

---

## 3. Warm-up

A new mailbox sending 100 a day looks exactly like a compromised one.

- 20/day to start, +10 every two business days, to the cap. Roughly three and a half weeks to
  100. This is automatic: set `warmup_started_on` on the mailbox and the send loop does the rest.
- [ ] `warmup_started_on` is set. **If it is NULL the full cap applies immediately** — which is
      correct for an old mailbox and wrong for a new one.
- [ ] The first week's recipients skew towards addresses likely to reply. Early engagement is
      what builds reputation; early silence is what costs it.

---

## 4. The flip to live

Only the owner, only in production, and the API enforces both (ADR-010).

- [ ] Section 1 is complete and DKIM passes **as received**.
- [ ] A human has read at least twenty drafts and would have sent all of them.
- [ ] Suppression list imported, if you hold an existing do-not-contact list.
- [ ] The footer is right. Note the postal address is deliberately absent (QUESTIONS.md Q2) —
      acceptable for an India-only audience, not for US recipients.
- [ ] A backup ran today and the restore drill has been done at least once.

Then: **Settings → Send mode → live → type `LIVE`**. The typed confirmation exists so this
cannot be a mis-click.

---

## 5. Rollback

**Stop sending, immediately:** Settings → Send mode → `disabled`. It takes effect on the send
loop's next cycle, within seconds, and needs no shell. That is why it is a database value and
not an environment variable any more.

Softer options, in increasing order of bluntness:

| Problem | Action |
|---|---|
| One lead should not be chased | Pause that lead — a flag, so it resumes where it was |
| One campaign is wrong | Set the campaign to `paused` |
| Something is wrong and you do not yet know what | Send mode → `disabled` |
| The mailbox is in trouble with the provider | Pause the mailbox; the loop stops and records why |

**A bad deploy:** `deploy.sh` keeps the previous image and puts it back if the API does not
answer. To go back by hand, check out the previous tag and re-run it.

**A bad migration:** restore the newest dump into a scratch database first and look at it. Never
restore over the live database as the first move — `docs/DEPLOY.md` under *Restoring* has the
drill, and it has been rehearsed.

---

## 6. The first week

- [ ] Read `/admin/needs-attention` daily: unknown sends, dead jobs, paused mailboxes.
- [ ] Watch the reply rate. Under ~2% means the drafts are not landing; stop and fix the
      research or the offer rather than sending more.
- [ ] Watch bounces. Over ~3% and the list quality is the problem, not the sending.
- [ ] Check the DMARC reports before raising the policy.
- [ ] Keep one weekly copy of a dump somewhere that is not AWS. The S3 copy protects against
      losing the instance; it does not protect against losing the account.

---

## What is deliberately not done

Stated here so nobody has to rediscover it:

- **No postal address in the footer** (Q2). Fine for India-only; revisit before any US recipient.
- **No Indian public-holiday calendar** (Q5). Weekends are handled; holidays are not.
- **`auto_send_if_confidence` exists in the type system and is refused by validation.** Enabling
  it is a code change with its own review, not a settings edit.
- **Erasure does not reach backups already taken.** A dump from before a deletion still contains
  that lead until it ages out, 30 days in S3 and 14 locally. Anyone claiming otherwise is
  describing a system that takes no backups.
