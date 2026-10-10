# Setup Checklist — what to create/provide before Claude Code starts

**You do NOT need everything on day one.** Claude Code only needs the "Phase 0–3" items to start. Provide the rest when it reaches that phase (it will ask).

| Needed by | Items |
|---|---|
| **Start (Phase 0–3)** | GitHub (§1), a dedicated Supabase project (§2), your test sheet (see "Your test sheet" below) |
| Phase 4 (LLM router + research) | Groq key and any extra provider keys (§3), Tavily key (§4, optional) |
| Phase 6–7 (send/replies) | Business mailbox SMTP/IMAP (§5), booking link (§8), test mailboxes (§9) |
| Phase 9 (deploy) | VM + domain/subdomain (§6), Cloudflare Pages (§7) |

---

## 1. GitHub (so Claude Code can branch → PR → CI → merge)

1. Make sure the repo is yours and Actions is enabled (Settings → Actions → General).
2. Install GitHub CLI and log in **as you**: `gh auth login` (browser flow). Claude Code uses `gh`; **do not paste tokens into chat**.
3. **Protect `main`** (Settings → Branches → Add rule for `main`): require a pull request, require status checks to pass (add the CI job names after the first CI run), disallow force-push. Claude Code can't (and shouldn't) set this itself.
4. Settings → Code security: enable Dependabot alerts + secret scanning (+ push protection).
5. Repo visibility: keep it **private** (the code and any future samples contain business logic/PII). Private repos get a limited free Actions-minutes quota, which is plenty for CI.
6. In Claude Code, block it from reading secrets. Suggested `.claude/settings.json` (verify the syntax with `/permissions` in your Claude Code version):
   ```json
   { "permissions": { "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"] } }
   ```
   and allow the routine commands it needs (`git`, `gh`, `pytest`, `ruff`, `mypy`, `npm`, `docker`) rather than running it with `--dangerously-skip-permissions`.

## 2. Supabase (Postgres database + Auth) — FREE

Why: one free service gives you the database and login (email/password, Google) for a team.
Verified free-plan facts: 500 MB database, 50,000 monthly active users for Auth, 2 free projects per organization, and **projects pause after one week of inactivity** (our always-on worker keeps it active, but pick a nightly export anyway because free plans don't give you point-in-time recovery).

**You already have a Supabase account, so skip sign-up.** Your Render memory (the 420 MB) is irrelevant here because this app doesn't use Render. What you need is a **new, separate Supabase project** inside your existing account, so this app's data never mixes with your other project. The 500 MB database limit applies per project, and the free plan allows 2 free projects per organization. Open your dashboard and count: if you already have 2, either pause/delete one you no longer need or tell me and we'll pick another option (a second organization, or another free Postgres). The app is designed to stay small (compact JSON, raw page text purged after 30 days, a storage meter in the admin screen).

1. supabase.com → open your dashboard → **New project** (under your existing organization). Name it e.g. `outreach`. Choose the closest region (**Mumbai / South Asia** if listed; otherwise Singapore). Set a **new, unique database password** (don't reuse your other project's) and save it in a password manager.
2. Project Settings → **API**: copy **Project URL**, **anon (public) key**, and **service_role key** (secret: server only, never in the frontend).
3. Project Settings → **Database** → "Connection string": copy the **Session pooler** URI (port 5432) and put your DB password in it. Use this one string for everything (app, worker, migrations). Do **not** use the "Direct connection": on the free plan it is IPv6-only and many VMs can't reach it. Don't use the transaction pooler (6543) for this app.
4. Authentication → Providers: enable **Email**. (Optional: Google.) Authentication → URL configuration: add your future frontend URL later.
5. Authentication → Settings: turn **off** public sign-ups (you invite team members manually) — Claude Code will tell you when.
6. Project Settings → API (Data API): **turn off "Enable Data API"**. The app talks to the database directly and uses Supabase only for login, so nothing should be reachable through Supabase's auto-generated REST API. (If the dashboard wording differs, Claude Code will tell you the exact toggle.)

Env vars: `SUPABASE_URL`, `SUPABASE_ANON_KEY` (frontend only), `DATABASE_URL` (session pooler string; the app converts it to the async driver form), `SUPABASE_JWKS_URL` (`https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json`; in Project Settings → JWT Keys, make sure the project uses **asymmetric signing keys** rather than the legacy shared secret). Claude Code will tell you if it also needs `SUPABASE_SERVICE_ROLE_KEY` (invites only; keep it server-side).

## 3. LLM providers — free-first, with automatic fallback

**About rotating several free accounts:** please don't. Groq's Acceptable Use Policy prohibits getting around its limits by "registering multiple accounts or orchestrating usage between multiple organizations", even if each account stays under its own limit. Breaking that risks losing the key the whole system depends on. (I couldn't find Tavily's equivalent clause, so I can't say it's allowed; I'm treating it the same way and not building it.)

**What works legitimately, and gives you more headroom:** one account per provider, plus an automatic fallback chain across **models and providers**. The app's LLM router reads an ordered list; when one entry hits its quota it cools down and the next one takes over. Limits are published per model, so several models on Groq already give separate headroom.

| Provider | Free-tier shape (as published Apr–Sep 2026; limits change, check each console) | Caveat |
|---|---|---|
| **Groq** | Per-model limits. Sample figures for some models: 30 req/min, 1,000 req/day, ~8,000 tokens/min, ~200,000 tokens/day. Your console's Limits page is authoritative | Tokens-per-minute is tight; spread the sends through the day |
| **OpenRouter** | Many `:free` models, 20 req/min and 50 req/day; 1,000/day after a one-time $10 top-up | Quality and uptime vary by model. The $10 top-up is the cheapest meaningful upgrade |
| **Cerebras** | ~1M tokens/day on listed models | One source says API access needs a verified payment method and the $5 credit expires after 30 days. Verify before relying on it |
| **Google AI Studio (Gemini)** | Limits per project, vary by model | **Free-tier prompts are used to improve Google's products (outside EEA/UK).** The app will send it only public website text, never lead names/emails/replies |
| **Mistral (Experiment tier)** | ~1B tokens/month | Data training is opt-in; same restriction as Gemini |
| Cohere trial | — | **Non-commercial only: excluded** |

"No training on free tier" statements come from a comparison page dated April 2026. Check each provider's current policy.

**Capacity, honestly:** ~8–10k tokens per lead (research + draft + validation) means 100 leads/day is roughly 0.8–1M tokens/day. A chain of 2–3 Groq models + Cerebras + OpenRouter free may cover that, but I don't know until it's measured. The app records real tokens per lead and shows them. When every free entry is exhausted the queue pauses with a clear message; it never quietly switches to a bad model.

**How to get keys (one account each, yours or the company's):** Groq → console.groq.com → API Keys. OpenRouter → openrouter.ai → Keys. Cerebras → cloud.cerebras.ai → API Keys. Gemini → aistudio.google.com → Get API key. Start with just Groq; add others when Claude Code reaches Phase 4 and asks.

Env vars: `GROQ_API_KEY`, `CEREBRAS_API_KEY` (optional), `OPENROUTER_API_KEY` (optional), `GEMINI_API_KEY` (optional), `LLM_CHAIN` (Claude Code generates the default), `PAID_LLM_ENABLED=false`, `PAID_LLM_DAILY_USD_CAP=0`.

## 4. Tavily (web search for research) — optional, free

1. tavily.com → sign up → copy the API key from the dashboard. Free "Researcher" plan: **1,000 credits/month, no card**. A basic search costs 1 credit, an advanced one 2.
2. At 1 search per lead that covers ~1,000 leads/month; at 100/day for 22 working days you'd need ~2,200. Claude Code caps usage with a monthly budget, caches per domain, and falls back to reading only the company's own website. Use **one Tavily key per account**; don't rotate across several accounts to stretch the free credits. Paid plan only if the news/person hooks prove worth it.

Env vars: `TAVILY_API_KEY` (optional), `TAVILY_MONTHLY_CREDIT_BUDGET=900`.

## 5. Your business mailbox (SMTP + IMAP)

**Tell me which provider hosts the mailbox** (Google Workspace, Zoho, Microsoft 365, GoDaddy, Hostinger, …). Settings and limits differ. Typical values:

| Provider | SMTP | IMAP | Notes |
|---|---|---|---|
| Google Workspace | smtp.gmail.com : 587 (STARTTLS) | imap.gmail.com : 993 | Needs 2-Step Verification, then Account → Security → **App passwords**. IMAP must be enabled. |
| Zoho Mail | smtp.zoho.in (or .com) : 587 | imap.zoho.in : 993 | App-specific password under Security. Match the data-centre domain (.in vs .com). |
| Microsoft 365 | smtp.office365.com : 587 | outlook.office365.com : 993 | Basic auth/SMTP AUTH may be disabled by your admin. |

Env vars: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `IMAP_HOST`, `IMAP_PORT`, `IMAP_USERNAME`, `IMAP_PASSWORD`, `FROM_NAME`, `FROM_EMAIL`, `REPLY_TO` (optional).
**Put these in the VM's `.env` only (production). Never in CI, never in the repo.**

**Do these DNS/reputation steps before sending to real founders (not needed for Claude Code to start):**
1. Your domain must have **SPF, DKIM and DMARC** records. Check at mail-tester.com (free): send it a test email and aim for 9–10/10.
2. **Big risk, your decision:** cold outreach from your *main* business domain can damage the reputation of the address you use with paying clients. Common practice is a **separate look-alike sending domain** (a few hundred rupees/year) with its own mailbox and redirect to your main site. The code supports adding a second mailbox later; I recommend you decide before go-live.
3. **Warm up:** don't blast 100/day from a mailbox that has never sent cold mail. The ramp (20/day, +10 every 2 business days) is built into the plan.

## 6. Where the backend + worker run (needs an always-on machine)

**Verified: Render's free tier blocks outbound SMTP ports (25, 465, 587) and sleeps after 15 minutes**, so it cannot send mail or watch replies. Railway keeps services always on but isn't free. Options: Your Render free capacity is also already used by your other project, so Render is out regardless.

- **Option A (free): Oracle Cloud "Always Free" VM** (their free ARM VM guides advertise up to 4 cores / 24 GB). Signup needs a card for verification and capacity in some regions is limited. I could not confirm Oracle's outbound-SMTP policy, so test port 587 from the VM before committing.
- **Option A2 (free, card needed): Google Cloud "Always Free" e2-micro VM** (US regions only). One source confirms GCP keeps port 587 open and blocks port 25; we only use 587. I did not re-verify the free-tier specifics this session, so check cloud.google.com/free first.
- **Option B (~US$5/month): a small VPS** (Hetzner, DigitalOcean, etc.). This is the "necessary spend" I'd make if A fails: reliable always-on for an email product.

You need: a VM with SSH access (Ubuntu), Docker installed (Claude Code will provide the script), and a **subdomain** such as `api.yourdomain.com` pointing (A record) to the VM's IP for HTTPS via Caddy. Ask Claude Code to produce `docs/DEPLOY.md` and follow it; it will not deploy for you.

Env vars (production only): `APP_ENV=production`, `API_PUBLIC_URL`, `ALLOWED_ORIGINS`, `CRON_SECRET` (generate: `openssl rand -hex 32`), `SEND_MODE` (starts as `disabled`; you flip to `allowlist` for rehearsal, then `live`).

## 7. Cloudflare (frontend hosting) — free — **done 2026-10-10**

Vercel's free Hobby plan prohibits commercial use (an agency counts), so the dashboard is hosted on Cloudflare, whose free tier has no such restriction. The owner disconnected and deleted the Vercel project on 2026-10-10 and connected this repo to Cloudflare instead.

Dashboard settings: Workers & Pages → connect GitHub repo, **root directory `frontend`**, build `npm run build`, output `dist`. Env (build-time): `VITE_API_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` — these are public by design and belong in the Cloudflare dashboard, not in this repo.

The build is configured by **`frontend/wrangler.jsonc`**, committed. It declares an assets-only
Worker — no server code, just the Vite output — and sets `not_found_handling` to
`single-page-application`, which is what makes a browser refresh on a client-side route such as
`/leads/<id>` work. Without it Cloudflare looks for a file at that path, does not find one, and
returns 404 before the router's JavaScript is ever served. This replaces the `_redirects` file the
older Pages flow needed; do not add both.

**If the project was created as Pages rather than as a Worker**, the same file needs the Pages key
instead — delete the `assets` block and use `"pages_build_output_dir": "./dist"`. Tell Claude Code
which one the dashboard shows and it will adjust. Either way the repo carries no Vercel config,
because Vercel was wired up entirely through its dashboard; nothing needed deleting here.

## 8. Booking link
Cal.com (free) or Google Calendar appointment scheduling or Calendly → create a "20-min intro call" → copy the public link. It goes into the **campaign persona** in the UI (not an env var).

## 9. Test mailboxes (for rehearsal, `SEND_MODE=allowlist`)
Two addresses you control (e.g., a personal Gmail and a second one). Env: `SEND_ALLOWLIST=you@gmail.com,other@gmail.com`. These receive rehearsal emails and let you reply to test the inbox side.

## 10. Optional later
- **Sentry** (free tier) for error alerts: `SENTRY_DSN`.
- **UptimeRobot / Better Stack** (free) to ping `/health`.
- Email verification API (ZeroBounce/Hunter free credits) if MX checks aren't enough.

---

## `.env` template (what Claude Code will turn into `.env.example` v2)

```
APP_ENV=development            # development | production
SEND_MODE=disabled             # disabled | allowlist | live   (only YOU set live, only in production)
SEND_ALLOWLIST=                # comma-separated test addresses
LOG_LEVEL=INFO

SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_JWKS_URL=
DATABASE_URL=

GROQ_API_KEY=
CEREBRAS_API_KEY=              # optional
OPENROUTER_API_KEY=            # optional
GEMINI_API_KEY=                # optional (only ever gets public website text)
LLM_CHAIN=                     # ordered provider/model list; Claude Code generates the default
LLM_DAILY_TOKEN_BUDGET=
PAID_LLM_ENABLED=false
PAID_LLM_DAILY_USD_CAP=0
TAVILY_API_KEY=                # optional
TAVILY_MONTHLY_CREDIT_BUDGET=900

SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
IMAP_HOST=
IMAP_PORT=993
IMAP_USERNAME=
IMAP_PASSWORD=
FROM_NAME=
FROM_EMAIL=
REPLY_TO=

CRON_SECRET=
API_PUBLIC_URL=
ALLOWED_ORIGINS=
SENTRY_DSN=                    # optional
```

---

## Your sheet (what each lead row needs)

| Column | Required? | Why it matters |
|---|---|---|
| `full_name` | **Required** | Greeting, person lookup |
| `email` | **Required** | Delivery; validated (syntax + MX), deduped |
| `company_name` | **Required** | Research + personalization |
| `company_url` | **Required** | Primary free research source (we read the site itself) |
| `job_title` | Recommended | "Founder"/"CTO" changes the pitch; used in the research query |
| `linkedin_url` | Recommended | Stored for *your* reference and the review screen. **Not scraped** (LinkedIn bans/legal risk) |
| `notes` | Optional | Anything you already know ("raised pre-seed in March", "building a fintech app"). Fed to research + draft |
| any other columns | Optional | Kept as extra data and available to the drafter |

Header names are flexible (`Name`, `E-mail`, `Website`, `Company`… will be mapped, with a preview before import). Example:

```
full_name,email,company_name,company_url,job_title,linkedin_url,notes
Asha Rao,asha@examplelabs.in,Example Labs,https://examplelabs.in,Co-founder & CEO,https://linkedin.com/in/asharao,Launching a mobile app next quarter
```

## Your test sheet (`local/test_leads.xlsx`)

Yes, do exactly that. Rules:
- **It's fine if columns are missing.** The importer shows a mapping preview and tells you what's missing; it will ask rather than guess.
- Put **your own email addresses** in `email`. Use at least 2–3. Gmail plus-addresses (`you+t1@gmail.com`, `you+t2@gmail.com`) all land in one inbox, which is handy for testing replies. List each exact address in `SEND_ALLOWLIST`.
- Put **real company websites** in `company_url` (and the company name). The app will read those public pages for research, politely and at low volume.
- **Mail goes only to allow-listed addresses.** Any row whose email isn't allow-listed is imported but never sent. Still, don't put real founders' emails into this sheet until go-live.
- Keep it in the `local/` folder (gitignored) in a **private** repo. Claude Code will commit only fake `@example.com` fixtures, and will remove the old `sample_leads.xlsx`, which has real personal Gmail addresses.
- Extend it as we go; at each phase Claude Code will tell you which columns or rows it wants added.

## Persona (filled once per campaign in the UI)

| Field | Example |
|---|---|
| Your name / role | "Harish Chand, Co-founder" |
| Agency name + website | "…" |
| What you do | "We build MVPs and AI features for startups, fixed scope and deadline, paid on completion" |
| Ideal customer | "Seed to Series A Indian startups without an in-house AI/mobile team" |
| Offer / value | "A 2–4 week MVP sprint with a fixed price" |
| **Proof points** (the *only* claims the AI may state as yours) | "Built X for Y in 3 weeks", "12 shipped projects" (only true ones!) |
| Goal | "Book a 20-minute call" |
| CTA + booking link | link from §8 |
| Tone / language | "Warm, direct, no hype; English" |
| Avoid | "revolutionary", exclamation marks, fake urgency |
| Signature | your real signature |
| Follow-up angle | "Short nudge + one useful idea" |

Honest proof points matter most: if you leave this thin, the system is *forbidden* to make claims up, and the emails get more generic.

---

## Does this fix issue #1 (no research / hallucination)?
**Mostly, but not alone.** Your columns + persona give the system real input (the company URL, the person's title, and your true claims). The fix is complete when Claude Code also builds: (1) the website reader, (2) the capped Tavily search, (3) evidence-cited research, (4) the "claim must trace to evidence/persona" validator. All four are in the master prompt (Phases 4–5). LinkedIn profile *content* will not be read, by design.

## Costs I expect (honest)
- **Free at launch:** GitHub, Supabase, Cloudflare Pages, Groq free (low volume), Tavily free, Oracle VM (if you get one).
- **Likely first paid items, in this order:** a one-time $10 OpenRouter top-up, or Groq/Cerebras pay-as-you-go, only if the free chain can't cover your *measured* tokens per lead; a ~US$5/month VPS if Oracle/GCP aren't available; a second sending domain (~₹ few hundred–1,000/year, strongly advised); Tavily paid only if hooks from news/search prove valuable.
- Free-tier terms change often. These were checked on 7 Oct 2026; re-check pricing pages before relying on them.

## Compliance note (not legal advice)
Target is B2B contacts in India. The system will always show a real sender identity, include an opt-out, stop immediately on opt-out/bounce, keep a suppression list, store minimum data, and support deleting a lead on request. Have a lawyer review `docs/COMPLIANCE.md` (which Claude Code will write) before scaling, especially regarding India's data-protection law.
