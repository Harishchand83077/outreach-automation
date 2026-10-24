# The four things only you can do

Each needs you signed in to a Google account. I never enter credentials, so these are yours to
click. Everything else is built and deployed.

Do them in this order. (1) and (2) together make "Connect Gmail" work; (3) takes five minutes and
tells you whether your mail is actually arriving; (4) is one command.

---

## 1. A Google Cloud project with an OAuth client

This is what turns **Connect Gmail** from a button into a working connection.

1. <https://console.cloud.google.com/projectcreate> → name it `outreach-automation` → **Create**.
2. **APIs & Services → OAuth consent screen**:
   - User type **External** → Create.
   - App name `Outreach`, your email as support and developer contact.
   - **Scopes → Add or remove scopes → paste** `https://mail.google.com/`, tick it, Update.
     (Also tick `.../auth/userinfo.email`.)
   - **Test users → Add users**: every email address that will use this, *including your own*.
     Up to 100.
   - **Leave the publishing status as `Testing`.** Read the box below before changing it.
3. **APIs & Services → Library** → search **Gmail API** → **Enable**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Type **Web application**, name `Outreach API`.
   - **Authorised redirect URI** — exactly this, no trailing slash:
     ```
     https://api.arkvane.in/api/v1/connect/google/callback
     ```
   - Create, then copy the **Client ID** and **Client secret**.

> ### Why it stays in `Testing`, and what it costs
>
> Google calls reading a mailbox a **restricted** scope. Publishing with one requires an annual
> third-party security assessment that costs real money, and hard rule 5 says not to spend it.
> An **unpublished** client needs none of that: up to 100 test users, any scope, free.
>
> **The price is that Google expires the connection every 7 days.** Each person clicks
> *Reconnect* about once a week. The system handles it properly — the mailbox pauses with a
> reason and the dashboard shows the button — rather than failing silently.
>
> `docs/MULTI_TENANT_PLAN.md` §4 has the two ways out when this outgrows ten people. Neither is
> a rewrite.

## 2. Put the values on the server

Session Manager → `i-02ca5f650887fc66c` → Start session, then:

```bash
cd /opt/outreach

# A key that encrypts stored mailbox credentials. Generate it, keep a copy somewhere safe.
sudo docker compose --env-file .env -f deploy/compose.prod.yml run --rm --no-deps api \
  python -m app.scripts.make_key

sudo nano .env
```

Add these five lines (paste your own values):

```bash
CREDENTIAL_KEY=<the key you just generated>
GOOGLE_CLIENT_ID=<from step 1>
GOOGLE_CLIENT_SECRET=<from step 1>
GOOGLE_REDIRECT_URI=https://api.arkvane.in/api/v1/connect/google/callback
DASHBOARD_URL=https://outreach-automation.sanketyadavdohar123.workers.dev

# Who may create an account. Leave this out entirely and nothing changes — sign-up stays closed.
SIGNUP_MODE=open
```

Then `sudo ./deploy/backup.sh && sudo ./deploy/deploy.sh` and check
`curl -s https://api.arkvane.in/ready`.

**If you lose `CREDENTIAL_KEY`,** every connected mailbox must be reconnected. Nothing else
breaks, and no stored token can be read by anybody — which is the point of it.

**`SIGNUP_MODE=open` means anybody who can sign in gets a workspace.** They send as themselves,
with their own Gmail quota and their own reputation — this is not an open relay and nobody can
send as anybody else. But if you want it narrower, use
`SIGNUP_MODE=allowlist` with `SIGNUP_ALLOWED_DOMAINS=arkvane.in,yourclient.com`. There is also a
`MAX_ORGS` ceiling, 25 by default.

---

## 3. Google Postmaster Tools

**Do this one even if you do nothing else.** Right now you have no idea whether your mail is
being filtered — you found out about the Spam problem because a reply happened to land there.
Postmaster Tools shows your spam-complaint rate, domain reputation and whether SPF, DKIM and
DMARC are passing *in the wild* rather than just being published. Free, and it is Google telling
you what Google thinks of you.

1. <https://postmaster.google.com/managedomains> → **Add domain** (the `+` button).
2. Enter **`arkvane.in`** — the domain in your `From:` header, not the sending account.
3. Google shows a **TXT record**. Copy the value.
4. At GoDaddy → `arkvane.in` → **DNS → Add record**:
   - Type **TXT**, Name **`@`**, Value **the string Google gave you**, TTL 600.
   - Do **not** touch the existing SPF, DKIM or DMARC records.
5. Back in Postmaster Tools → **Verify**. If it fails, wait ten minutes and try again.

**Data appears after you have sent a meaningful volume** — it needs hundreds of messages a day
before most charts populate, so expect it to look empty at first. The *authentication* panel is
useful immediately.

While you are there, two existing items worth closing:

- **Confirm DKIM is signing.** Send one message from the arkvane.in mailbox to a Gmail address,
  open it, **⋮ → Show original**, and check `DKIM: PASS`. If it says anything else, Workspace
  admin → Apps → Gmail → Authenticate email → **Start authentication**.
- **`SMTP_EMAIL` is still a personal Gmail.** Until it is an `@arkvane.in` Workspace address,
  your SPF and DKIM records protect a domain your mail is not actually sent from. This is the
  single biggest thing limiting deliverability, and it was the cause of the reply that went to
  Spam.

---

## 4. A real booking link

Your campaign persona's booking URL is `https://calendar.google.com/calendar/u/0/r?pli=1`. That
opens **the reader's own calendar**. It books nothing. Every email you send asks for a meeting
and then links to a dead end.

<https://cal.com> free tier, 20 minutes. Then edit the campaign persona and paste the real link.

No code I write is worth as much as this.
