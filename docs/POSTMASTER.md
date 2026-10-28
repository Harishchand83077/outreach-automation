# Seeing whether our mail is being filtered

Until now there has been no way to answer that. The one time it came up, we found out by
accident: the first reply this system ever received was sitting in Spam, and we learned it
because a person looked in a mailbox. That is not monitoring.

This document sets up the two free things that can tell us, and is honest about what each one
will and will not show at our volume — because the more useful of the two is the one nobody
thinks of, and the famous one will probably show nothing for months.

---

## 1. The short version

| Signal | Shows | Works at our volume? | Who sets it up |
|---|---|---|---|
| **Google Postmaster Tools** | spam rate, domain & IP reputation, SPF/DKIM/DMARC pass rates, delivery errors — **for mail to `@gmail.com` only** | **Probably not yet.** See §3. | owner, 10 minutes |
| **DMARC aggregate reports** | every source sending as `arkvane.in`, with SPF and DKIM pass/fail counts, from every receiver that honours DMARC | **Yes, at any volume** | already on — and already being ignored. See §5. |

Set up Postmaster anyway, now, for the reason in §3: it has no backfill.

---

## 2. Setting up Postmaster Tools

Two steps, both the owner's, because both need a Google sign-in.

1. **Add the domain.** [postmaster.google.com](https://postmaster.google.com) →
   **Add new domain** → `arkvane.in` → **Create**.

   Add the domain you *authenticate* with — the DKIM `d=` domain or the SPF return-path domain.
   For us both are `arkvane.in` (`DEPLOY.md` §6), so there is one domain to add, not two.

2. **Verify it.** **More options** → **Verify domain**, copy the TXT value Google shows, add it
   at GoDaddy, come back and press **Verify**. Up to 10 minutes.

### The three things that go wrong here

- **Add a record; never replace one.** The apex of `arkvane.in` already carries an SPF record, a
  Google site-verification token and (at `google._domainkey`) a 2048-bit DKIM key. A verification
  step that overwrites the SPF record trades visibility for deliverability, which is the opposite
  of the point. In GoDaddy's panel the apex host is `@`.
- **One SPF record, still.** Unchanged from `DEPLOY.md` §6 and worth repeating because it is the
  single most common way a domain's mail breaks: two SPF records is a permanent error, not a
  merge.
- **The apex already has a `google-site-verification=` TXT.** It may be enough — Google's
  verification is shared across its properties, so if this account already verified `arkvane.in`
  (for Search Console or for Workspace), Postmaster may show it verified with no new record.
  **Check before adding anything.** Google's own setup page documents only the TXT/CNAME route
  and does not mention reusing an existing verification, so do not assume it either way: open the
  Verify window and see what it says.

---

## 3. It will probably show nothing, and that is why to do it now

Two independent reasons, and they compound.

**Google hides low-volume data on purpose.** Its own words on the setup page: data might be
missing if the total number of messages for a given day is too low, to protect users' privacy.
It does not publish the threshold. Deliverability practitioners put it at somewhere between
roughly 100 and a few hundred messages a day for anything at all to appear, and in the low
thousands for the full set of dashboards — spam rate in particular is reported as the hungriest.
Those are unofficial numbers and should be treated as such.

**It only counts mail to personal Gmail.** `@gmail.com` and `@googlemail.com` addresses only.
A founder on their own Workspace domain does not appear here at all, however Google-hosted their
mailbox is.

Our locked product rule is **100 emails a day per mailbox**, and warm-up starts at 20. Some
fraction of those recipients are on `@gmail.com`. So realistically we are looking at tens of
qualifying messages a day against a threshold of hundreds, and the dashboards will be empty or
sparse for a long time.

**So why do it today?** Because **Postmaster has no backfill.** It shows data from after
verification and nothing from before. Verifying the day before the first real campaign means the
first campaign is the one we cannot see. The ten minutes buys nothing this week and buys the
whole history later, which is the same argument ADR-026 made for the outcome dataset: it is
cheapest now and impossible retroactively.

An empty dashboard is also not *only* a volume signal. Google says data may be withheld when a
domain's reputation is particularly bad, so "no data" and "bad data" look alike from outside.
That is one more reason the thing in §5 matters.

---

## 4. What to read, once there is anything to read

- **Spam rate** is the number that decides everything else. Google's bulk-sender guidance is to
  stay under **0.3%** and aim under **0.1%**. At 100/day, *one* complaint in a day is 1% — so at
  our volume this metric is extremely noisy and a single bad day means little. Watch the trend.
- **Domain reputation** — High / Medium / Low / Bad. Low or Bad means stop and fix, not send
  slower.
- **Authentication** — SPF, DKIM and DMARC pass percentages. **This is the panel to check
  first**, because it is the one we already know has a problem (§5).
- **Delivery errors** — rejections and why.

If spam rate climbs or reputation drops, the lever is this system's own: `SEND_MODE`, the daily
cap and the send window are all editable from the dashboard, and restricting takes effect on the
send loop's next cycle.

---

## 5. The signal that already works, and that nothing reads

`arkvane.in` already publishes DMARC:

```
_dmarc.arkvane.in  TXT  "v=DMARC1; p=none; rua=mailto:rahul@arkvane.in"
```

`rua` means every participating receiver — Google, Microsoft, Yahoo and most of the rest — sends
us a **daily aggregate report**: each IP that sent mail claiming to be `arkvane.in`, how many
messages, and whether SPF and DKIM aligned. It is **not** volume-gated. It arrives whether we
send 20 messages or 20,000.

**This is the one that would have caught the defect we already know about.** `DEPLOY.md` and
`PROGRESS.md` both record it: mail has gone out as `From: rahul@arkvane.in` while SMTP
authenticated as an unrelated personal Gmail, so Google will not sign it for `arkvane.in` and the
SPF and DKIM passes verified for that domain **do not apply to it**. In a DMARC aggregate report
that appears immediately and unmistakably, as a sending source with `dkim=fail` and `spf=fail`
against our own domain.

Three things are wrong with the current setup, in order of how much they matter:

1. **Nobody reads them.** They are gzipped XML, one file per receiver per day. Unread reports are
   the same as no reports.
2. **`rua` points at a sending mailbox.** `rahul@arkvane.in` is an address this system sends
   *from*, and whose INBOX and Spam folder the IMAP loop polls. So the reports are ingested into
   the database as inbound messages, match no lead, and sit as orphans until the hourly sweep
   deletes them after 45 days. Harmless — no model is ever called on them, which is the property
   the threading-first reply matcher exists for — but it is the owner's correspondence mixed into
   application data for no reason. A dedicated address such as `dmarc@arkvane.in`, not attached
   to any campaign, fixes both halves.
3. **`p=none` asks receivers to do nothing** about mail that fails. That is correct for now and
   should stay: tightening to `quarantine` before the alignment problem in §5 is fixed would have
   receivers quarantine our own real mail. The order is: read the reports, fix alignment, *then*
   tighten.

Reading them is either a person opening the XML — fine at one or two reports a day, which is what
we get — or a report analyser. Several have free tiers. The trade is explicit: a DMARC aggregate
report carries no message content, but it does carry our sending sources and per-receiver
volumes, so an analyser is a third party learning the shape of our mail programme. That is a
decision for the owner rather than a default, and nothing in this repo sends them anywhere.

---

## 6. This monitors **our** mail, not the product's

Postmaster Tools is keyed on the **sending domain**. After ADR-023 the deployment has no mailbox
of its own: every workspace sends from an account its own owner connected, under its own domain.

So verifying `arkvane.in` tells us about *our* outreach and nothing about anybody else's. There
is no deployment-wide view of deliverability and there cannot be one — a tenant's reputation
belongs to their domain, and we could not read it without credentials for it, which we neither
have nor want.

Each workspace therefore has to do this for itself. The dashboard says so, next to the sending
account, with a link — one sentence in the place where somebody is already looking at the address
that has the reputation. That is the whole of the product-side change: there is no API call, no
stored token and no new dependency, because adding a page that showed Postmaster numbers would
mean holding OAuth credentials for every tenant's Google account in order to display a dashboard
Google already draws for them.

---

## 7. The checklist

Owner, once:

- [ ] Add and verify `arkvane.in` at [postmaster.google.com](https://postmaster.google.com) — and
      **check the Verify window first** in case the existing site-verification token already
      covers it (§2).
- [ ] Expect empty dashboards (§3). Do not read that as a fault.
- [ ] Point `rua=` at an address that is **not** a sending mailbox, and read a week of reports.
- [ ] Fix the `From`-versus-authenticated-account mismatch before the first real campaign. The
      DMARC reports are how you will know it is fixed.
- [ ] Leave `p=none` until those reports are clean.

Then, before go-live and weekly after it: the Authentication panel, then the spam rate trend.
