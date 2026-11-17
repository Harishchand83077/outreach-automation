# Outreach Platform v4 — AI Job-Switch Copilot: Product, UI, Architecture and Roadmap

Nov 8, 2026 · @Harish Chand

## Executive summary

**Recommendation: make the *opportunity* (one job, with everything about it) the centre of the product, reuse the outreach engine as the tool that talks to recruiters and referrers, and build the navigation and the missing screens first, because the job-hunt screens that exist have no router, no Today view and no opportunity page.** v4 turns the repo from an email-outreach platform into a job-switch copilot for software engineers in India. The engine stays; the front door changes.

**What the repo looks like today.** I re-cloned the live repo at commit `e7ec2d7` (8 November 2026), six days after the first audit, and updated this document to match. v3 is merged through P16 except two small slices. The repo now has resume tailoring with PDF rendering and an applications tracker, a referral finder fed by the user's own LinkedIn export, contact lookups with the user's own provider keys, a mailbox pool, a booking webhook, a watchlist with signals, a relationship graph, experiments, four connector scopes, and four screens beyond the campaign ones (Job hunt, Sending, Signals, Operations). Still not built: a resume upload and parser, structured facts and preferences, cover letters, an opportunity entity, a job-verification history, a recruiter inbox view, and any router.

**Three findings drive this document.**

1. **The job-hunt screens exist but have no navigation model.** `JobHunt.jsx`, `Sending.jsx`, `Signals.jsx` and `Operations.jsx` now show profile facts, job posts, fit, tailored resumes and applications, and a backend guard (ADR-048) fails the build if a feature has no screen. But screens are still picked by a state variable inside a 4,983-line `App.jsx`: no router, no deep links, no Today view, no opportunity page. Routing and a shell are still the first gate.
2. **The fit score is stored now and still too thin for a recommendation.** `fit_scores` (ADR-055) keeps the result of an explicit *Score the fit* press, with matched and missing lists. It still matches a requirement to a fact when they share two meaningful words. That is fine for proposing matches a person can dismiss, and not enough to say APPLY or SKIP. v4 starts with interpretable criteria and measures them against outcomes before any learned ranking.
3. **Career memory is still a list of statements, and nothing reads a resume.** A fact has a kind, a statement, an optional evidence URL, a source (user, github or resume) and a confirmation time. There is no PDF or DOCX reader anywhere in the code, no structured experience, no preferences, no confidence and no last-used date. Tailored resumes are built from confirmed facts, so the missing parser is the real start of P18.

**What to build first (the MVP).** Career Memory (A) with resume upload and parsing, preferences and structured facts; job verification and the Next Best Move (B, C) on top of the stored fit score; the Opportunity Workspace (D), which wraps the existing applications; the Application Copilot (E), which adds cover letters, character-limited answers and an opportunity link to the tailoring and PDF already built; a recruiter inbox for one Gmail account (G, light); and basic outcomes (I). The Warm Path Navigator (F) is mostly built (LinkedIn-export import, referral finder) and needs relationship notes and links to opportunities. The Interview War Room (H) follows the MVP. The three extra modules stay designs only.

**How v4 relates to v3.** v3 is merged through P16 apart from S16.3 and S16.4, so v4 builds on top of it and nothing from v3 is parked any more. The mailbox pool, booking webhook, signals, graph, experiments and four connector scopes exist and are reused: the agency screens keep working, and the job-switch product reads from the same stores. Phases continue the repo's numbering: v4 is P17 to P24, and the next free ADR number is 057.

**Five decisions are yours** (last section, each with a default): whether v4 replaces or sits beside agency outreach; whether you are the only user at first; how much of the inbox the agent may read; whether the extension gets a public listing; and whether Outlook is needed early.

## Audit: what the repo does today

**The platform and most of the job-hunt backend are built; the product still lacks a navigation model, a resume reader and an opportunity entity.** Status below is from reading the code at commit `e7ec2d7` (8 November 2026); I did not run the test suite or the live server, so *works* means the code and its tests exist, not that I saw it working. The repo has 50 tables, 43 migrations, 138 test files, ADRs up to 056 and about 120 routes. The title line of `docs/PROGRESS.md` still says *Updated 2026-10-11 (Phase 9 slice 1)*, although its body is current.

| Capability | Status | Where | What is missing for v4 |
| --- | --- | --- | --- |
| Sign-in, workspaces, roles, invitations | Implemented | `members`, `invitations`, `OrgContext`, Supabase JWKS | A single-person workspace as the default for an individual job seeker |
| Background jobs | Implemented | `jobs` queue with `dedupe_key`, lease, dead state; five worker tasks | Per-workspace queue cap and version stamps for new job kinds (v3 F21 remainder) |
| Model access | Implemented | `LLMRouter`, free-first chain, `allow_pii` | A resume-sized document path and a cost log per feature |
| Company research and grounding | Implemented | `domain_research`, shared `company_profiles` (ADR-038), claim check on `pp\d+` ids, search-key chain | Research per opportunity, not only per lead |
| Sending, outbox, inbox reading | Implemented in code | SMTP/IMAP, outbox, UID cursors, `SEND_MODE` gate, mailbox pool with sticky sender (ADR-044), booking webhook | Real sending was not switched on in the notes I read; a candidate sending from their own Gmail does not need the pool |
| Reply classification | Implemented | 12 labels, keyword rules plus a model; each label maps to an action | Recruiter-specific labels: interview invite, rejection, screening request, offer, scheduling |
| Gmail sign-in per user | Implemented in code | `google_oauth.py`, `oauth_states`, ADR-023, ADR-050 | Outlook not built; check ADR-050 and `PROGRESS.md` for the Google client's verification state before relying on refresh tokens |
| Candidate facts and answer bank | Partial | `profile_facts` (kind, statement, evidence URL, source user, github or resume, confirmed time), `answer_bank`, a facts panel in Job hunt | No resume upload or parsing (no PDF or DOCX reader anywhere), no structured roles or dates, no preferences, no confidence, no last-used date |
| Job intake | Partial | `job_posts` (status open or archived), `watched_boards`, Greenhouse/Lever/Ashby feeds, pasted URLs, daily poll (ADR-036), archive button | No `first_seen` or `last_verified`, no cross-source dedupe, no staleness evidence, no company career-page discovery |
| Fit score | Partial | `fit_scores` (ADR-055): computed when the user presses *Score the fit*, read free afterwards, with matched and missing lists; two shared words count as a match | No experience, location or salary logic; no next-move recommendation |
| Resume tailoring and PDF | Implemented | `services/resume` (ops, validate, tailor, store, pdf; ADR-043), `resume_versions` with `edit_ops`, `rendered_text`, `fact_ids`; tailor, render and PDF routes; a versions list with a reader | Not tied to an opportunity; no uploaded base resume; no side-by-side diff; no cover letters or character-limit fitting |
| Applications | Partial | `applications` (job post, resume version, status planned, applied, interviewing or closed, `applied_at`, `follow_up_due`, notes) and an Applications panel | No timeline, tasks, strategy note, closed reason or link to mail threads |
| Referrals and contacts | Implemented | `connections` imported from the user's own LinkedIn export (ADR-038, ADR-042), referral finder with a drafted note, `contact_candidates` lookups with the user's own provider keys; a test asserts nothing fetches LinkedIn | No relationship notes or strength, no request states, no link to an opportunity |
| Signals, graph, brief, experiments | Implemented | watchlist and `signal_events`, `entities` and `edges` (an edge without a source is refused, ADR-051), brief, `experiments` | Becomes the base for a job radar later |
| Chrome extension | Partial | `extension/` MV3 side panel, `/ext/v1` (`whoami`, `suggest`, `outcome`), `captures`, mappers for three ATS (ADR-039, 040); unchanged since the first audit | Not yet shown to run against a live form; no job-save flow; no match panel |
| Claude connector (MCP) | Implemented | `/mcp`, read tools, `propose_draft`, `propose_reply`, four scopes (ADR-056) | Tools for opportunities and facts |
| Outcome records | Partial | `lead_outcomes`, `experiments`, export script (ADR-026) | Outcomes for applications, interviews and offers; nothing links a resume version to a result |
| Web app | Partial | React/Vite; `App.jsx` (4,983 lines) plus `JobHunt.jsx`, `Sending.jsx`, `Signals.jsx`, `Operations.jsx`, `ui.jsx`; dark theme tokens; backend guards that every screen calls real routes, reads real fields and declares its state (ADR-048) | No router or deep links, no Today, Opportunity, Pipeline or Inbox screens, JavaScript not TypeScript, no component tests beyond `api.test.js` |
| Opportunities, tasks, preferences, interviews | Missing | no tables | All of it |
| Vector search | Missing | no pgvector in code or dependencies | Only needed for fact retrieval at scale; see the data model section |

**Two things to verify before building on this.** First, run the suite and the app locally; I could not. Second, rewrite the handoff note at the top of `PROGRESS.md`, because Claude Code reads it first and a header that says Phase 9 misleads it.

## Competitive landscape

**Existing tools do one slice well: track, autofill, match, or bulk-apply. None of them, as far as the sources I opened say, decides *which move to make* for each job or manages the recruiter conversation afterwards.** The only comparison I could open is [Scoutify's 2026 roundup](https://scoutify.com/blog/best-job-search-tools-2026), written by a vendor that ranks its own product first; I use it for what each tool says it does, not for rankings. Prices are as that article reports them (September 2026).

| Tool | What it is | What it does not do (per the article) |
| --- | --- | --- |
| Teal | Resume builder, keyword matching, job tracker; free tier, about $29/month paid | Does not find jobs or send alerts |
| Huntr | Kanban tracker with browser extension; about $40/month paid | Does not find jobs or send alerts |
| Simplify | Free autofill extension | You still press submit; portal coverage is the vendor's own claim |
| Jobright | AI matching, resume tailoring, cover letters, applies on paid plans | Limited free tier, no public pricing page |
| LazyApply | Bulk auto-apply through LinkedIn, Indeed and others; $99 to $999 a year | Runs through your own board accounts; the article flags account-restriction risk on LinkedIn |
| Scoutify, JobCopilot, AIApply | Auto-apply from career pages or boards | Supported career sites only; form accuracy not measured |
| Wellfound | Startup board with salary and equity; direct messages from founders | Reviews cite unanswered applications and stale listings; thin outside startups |
| LinkedIn Jobs | Largest board; Easy Apply; recruiter contact | Premium about $40/month |

The same article does not discuss referral features for any tool. That is a gap in what I read, not proof that no tool offers them.

**India specifics.** [Cutshort](https://cutshort.io/how-to-source-talent-using-claude) publishes an MCP server and API so an employer's agent can search Indian tech talent; keys go to approved employers, so it is a recruiter-side tool and a sign that recruiters will have agents on their side. I did not open Naukri, Instahyre or LinkedIn's own pages in this pass, so I make no claims about their features or terms.

**Where v4 should differ, and why that is defensible:**

- **A recommendation, not a list.** Four actions per job (apply, referral first, build evidence, skip), each with its reasons and the facts it rests on.
- **Claims the candidate agreed to.** Resumes and answers are built only from confirmed facts, and every sentence points at one. Bulk tools cannot offer this without a fact store.
- **The conversation after the application.** Recruiter replies, interview invites and follow-ups in one timeline per job, with the approval gate the repo already has.
- **The user's own outcomes.** Which resume version and approach led to replies, shown with its sample size and without hiring-probability claims.

**What v4 will not compete on:** application volume, auto-submit, scraping job boards or LinkedIn, or a contact database.

## Feature gap analysis

**Of the nine features, four have a real base (E, F, part of B and part of C); the rest need new tables and screens.** Size is my estimate of work for one developer working with Claude Code: S is a few slices, M is a phase, L is more than a phase. It is a guess, not a measurement. Sizes for E and F shrank since the first audit because the repo built their cores.

| Feature | Today | Gap | Size |
| --- | --- | --- | --- |
| A. Career Memory | Facts with source and confirmation; answer bank; a facts panel | Resume upload and parsing; structured roles, projects, education; preferences and salary range; confidence and last-used dates; per-fact evidence list | M |
| B. Job discovery and truth engine | ATS feeds for Greenhouse, Lever, Ashby; pasted URL; daily poll; archive | Career-page discovery; cross-source dedupe; verification history with `first_seen` and `last_verified`; stale and suspicious signals shown with evidence; extension capture of a job | M |
| C. Fit and Next Best Move | Stored word-overlap fit score with matched and missing lists | Structured requirement extraction; checks for experience years, location, salary, eligibility; the four-move rule; referral awareness (referrals now exist to read) | M |
| D. Opportunity Workspace | An applications list with status and follow-up date | The `opportunities` entity wrapping applications, a timeline of events, tasks and reminders, and the screen that holds them | M |
| E. AI Application Copilot | Tailored resumes by validated edit operations with PDF and fact ids; answer bank; extension suggests answers for three ATS | Cover letters; character-limit fitting; an uploaded base resume; a diff view; evidence per claim in letters; approval bar; opportunity link | M |
| F. Warm Path Navigator | LinkedIn-export import, referral finder with a drafted note, contact lookups | Relationship notes and strength, referral request states, link to an opportunity, a Network screen | S |
| G. Recruiter Inbox Agent | Reply classifier, follow-up scheduler, opt-out stops, Gmail sign-in, IMAP reader | Recruiter detection among all mail, linking a thread to an opportunity, interview and deadline detection, reply drafts, Outlook | M |
| H. Interview War Room | Nothing | Interview detection, prep packs, study plans, mock interviews, rubrics, progress | L |
| I. Career Intelligence | `lead_outcomes`, experiments and export for outreach | Outcome events for applications and interviews; per-version and per-approach views; sample sizes shown; no probability claims | M |
| Module: Skill Proof Studio | Nothing | Design only | L |
| Module: Opportunity Radar | Watchlist and signal events built for companies | Design only; reuses the feed poller and the watchlist | S |
| Module: Career Path Simulator | Nothing | Design only | L |
| Chrome extension | Side panel suggests answers on three ATS | Save a job, show match, draft messages, resume choice per application | M |
| Web UI | Job hunt, Sending, Signals and Operations screens, no router | Router, shell, Today, Opportunity, Pipeline, Memory, Inbox and Insights screens | L |

**Cross-cutting gaps** that every feature above depends on:

1. **An `opportunity` as the spine.** Today a `lead` is a person to email. A job-seeker's unit of work is a job, with people, documents and events hanging off it. The database section proposes the entity.
2. **An event log for the whole journey**, not only for mail. The existing `events` table is append-only and tied to leads; opportunities need the same discipline.
3. **A router and a component structure in the web app**, or every new screen makes `App.jsx` longer.

## MVP scope and what waits

**The MVP is done when one person can upload a resume, see jobs ranked with a recommended move, work one opportunity from one screen, send an approved application and recruiter email, and see what happened.** Everything else waits until that loop has been used on real jobs for a few weeks.

### In the MVP (phases P17 to P21)

- **A. Career Memory**, with resume upload, structured facts, preferences and confidence.
- **B. Job discovery**, using the feeds already built plus pasted URLs and extension capture; verification history; no career-page crawling yet.
- **C. Next Best Move**, with four actions and plain reasons; interpretable rules only.
- **D. Opportunity Workspace**, with timeline, tasks and status.
- **E. Application Copilot**, with tailored resumes by edit operations, cover letters and screening answers; extension assists on the three ATS already mapped.
- **G. Recruiter inbox**, light: one connected Gmail account, recruiter threads linked to opportunities, drafts for approval.
- **I. Outcomes**, basic: events and a funnel with sample sizes.
- **UI**: a job-hunt shell with real routing, replacing outreach as the default screen.

### After the MVP (P22 to P24)

- **F. Warm Path Navigator**, finishing what exists: the LinkedIn-export import and referral finder get relationship notes, strength, request states and a link to each opportunity.
- **H. Interview War Room**, in two steps: prep packs first, then mock interviews with rubrics.
- **Outlook** for the inbox agent.

### Designs only

Skill Proof Studio, Opportunity Radar and Career Path Simulator. The feature specifications list what each needs from the MVP so nothing is built that blocks them.

### What happens to v3's unfinished items

| v3 item | State at `e7ec2d7` and decision | Why |
| --- | --- | --- |
| S13.5 F9 provider lookups | Built (`contact_candidates`, the user's own provider keys). Keep | Reuse to find recruiters and referrers on a button press |
| S13.6 F10 referral finder | Built (LinkedIn-export import, finder, drafted note). Absorb into F | Add relationship notes and opportunity links |
| S13.7 F20 resume tailoring | Built with PDF and an applications tracker. Absorb into E | Add an uploaded base resume, cover letters, limits and the diff view |
| P14 F7 mailbox pools, F15 booking webhook, F17 provider steps | Built. Keep, unused by the job-switch flow | Built for agency volume; a candidate sending from their own Gmail does not need the pool |
| P15 F11/F22 signals, F12 graph, F13 variants | Built (watchlist, `signal_events`, `entities` and `edges`, `experiments`). Keep | Signals become Opportunity Radar later; the graph stays internal and Postgres-only |
| P16 F24 scopes, new MCP read tools | Built (four scopes, ADR-056) except S16.3 and S16.4. Add opportunity tools after E | Lets the user query their own job search from Claude |
| F1, F3, F4 deliverability | Merged earlier. Keep for the shared sending domain | If candidates send from their own Gmail, Gmail signs the mail; the domain checks matter only for any mail sent from `arkvane.in` |

Agency outreach (`kind = outreach`) stays in the code and tests. It leaves the main navigation and moves under a workspace setting, so nothing is deleted and nothing needs migrating.

## User journeys and screen map

**Six journeys cover the product; each one starts at a single screen and ends with the user having approved something or learned something.**

| Journey | Steps | Ends when |
| --- | --- | --- |
| 1. Set up | Sign in, upload resume, review the facts extracted from it (confirm, edit, delete), set role targets, location, salary range and notice period, optionally connect Gmail and install the extension | At least one confirmed fact per section and a saved preference set |
| 2. Daily triage | Open Today: new jobs with a recommended move, replies waiting, tasks due; act on each in one or two clicks | Today shows nothing unreviewed |
| 3. Work one opportunity | Open the job; read the fit reasons; see the strategy; pick resume version; draft cover letter and answers; add a referral or recruiter contact | Status moves to Ready to apply |
| 4. Apply | In the ATS page, the side panel proposes answers from Career Memory; the user edits, accepts and submits themselves; marks the application as applied | The `applications` row says applied, with the resume version used |
| 5. Handle a reply | A recruiter email arrives; it is linked to an opportunity and classified; a draft reply waits for approval; an interview invite creates a task and a calendar-ready entry | The reply is sent or dismissed by the user |
| 6. Review the week | Funnel by stage, by resume version and by approach, each number with its sample size | The user changes a preference or a plan |

### Screen map

| Screen | Route | Job | Builds on |
| --- | --- | --- | --- |
| Today | `/today` | One list of what needs a decision today: jobs to triage, replies, tasks | New |
| Jobs | `/jobs` | Discovered and saved jobs, filter by move, source, freshness; save a pasted URL | `job_posts`, `watched_boards`, the jobs section of `JobHunt.jsx` |
| Opportunity | `/jobs/:id` | The workspace for one job (wireframe below) | New, wrapping `applications` |
| Pipeline | `/pipeline` | Board of opportunities by status; drag to change status | New |
| Career Memory | `/memory` | Tabs: Facts, Experience, Preferences, Answers, Documents | `profile_facts`, `answer_bank`, the facts panel in `JobHunt.jsx` |
| Resumes | `/resumes` | Base resume, tailored versions, which application used which | `resume_versions` list and reader in `JobHunt.jsx` |
| Inbox | `/inbox` | Recruiter threads grouped by opportunity, drafts to approve | Review queue, reply drafter |
| Network | `/network` | Imported contacts, paths to a company, referral requests | `connections`, referral finder, `contact_candidates` (screen after MVP) |
| Prep | `/prep` | Interview prep packs, study plan, mock interviews | New (after MVP) |
| Insights | `/insights` | Funnel and comparisons with sample sizes | Outcome export |
| Settings | `/settings` | Accounts, extension token, Claude connector, privacy and data export, workspace type | Existing settings, MCP tokens, team |
| Outreach (legacy) | `/outreach/*` | Campaigns, leads, review queue, Sending, Signals and Operations as they are today | Existing screens, moved |

A global command palette (the repo already has a keyboard-action hook in `App.jsx`) jumps to any job, contact or screen. Every screen has a URL, so a link to a job can be pasted into a note or opened from the extension.

## UI design

**Keep the design system the repo already has, add routing and a component structure, and design every screen around one rule: a recommendation is always shown with its reason, and nothing leaves the app without a visible approval.** The repo's tokens (dark and light themes, DM Sans and JetBrains Mono, a 4-to-44 px spacing scale, a blue accent with success and warning tones) and its shared pieces in `ui.jsx` (`Skeleton`, `Empty`, `Drawer`, `CommandPalette`, `ThemeToggle`) are a sound base. The problem is structure, not style.

### Structural changes

| Change | Why | Cost |
| --- | --- | --- |
| Add a router (React Router) and give every screen a URL | Today a state variable picks the screen, so there are no links to a job, no back button, no way for the extension to open an opportunity | One dependency, free |
| Split `App.jsx` into `routes/` and `components/` before adding screens | 4,983 lines in one file; each new screen would add 300 to 700 more. Four screens already sit in their own files | A refactor slice with no behaviour change, guarded by the existing tests |
| Fetch with TanStack Query | Job scoring and resume rendering are background jobs; screens must poll, cache and show "working" honestly | One dependency, free |
| New files in TypeScript, old files stay JavaScript | The extension is already strict TypeScript; shared API types stop drift. Converting everything first would stall the product | Config only |
| Accessible primitives (Radix UI) for dialogs, menus and tabs | Focus trapping and keyboard handling are easy to get subtly wrong by hand | One dependency family, free |
| Component tests (Vitest with Testing Library) and a Playwright smoke test per route | Only `api.test.js` exists today | Dev dependencies, free |

**The split has a pattern to copy.** `JobHunt.jsx`, `Sending.jsx`, `Signals.jsx` and `Operations.jsx` are already separate files, so the refactor moves `App.jsx` (now 4,983 lines) toward that shape rather than inventing one. The repo also enforces one UI rule in its backend suite (ADR-048): a feature with no screen is not shipped, and three tests check that each screen calls real routes, reads real fields and declares its state. Every v4 screen must pass that guard and register itself in it.

### Product-specific building blocks

- **Move badge.** APPLY, REFERRAL FIRST, BUILD EVIDENCE, SKIP. Each has an icon and a word, never colour alone. Clicking it opens the reasons.
- **Evidence chip.** A fact id such as `pp12` rendered as a small chip; click opens the fact, its source and when the user confirmed it. This is the visible form of the claim check.
- **Verification label.** *Open, verified 2 days ago*, *Not re-checked for 21 days*, or *Possibly stale: page unchanged for 45 days, posted date missing*. Never "fake" or "ghost": the labels state what was observed.
- **Approval bar.** A fixed bar at the bottom of any screen that holds a draft, resume or message: *Review*, *Edit*, *Approve and send*. Anything that would leave the app says so on the button.
- **Sample-size caption.** Every chart and every comparison shows its count: *14 applications, 3 replies. Too few to compare versions.*
- **Status stepper.** Saved, Preparing, Ready, Applied, Interviewing, Offer, Closed, with the last change and who made it (you, the extension, the inbox agent).

### Rules for states and wording

1. **Empty is not loading.** Keep the repo's own lesson (the comment above `campaignsLoaded` in `App.jsx`): a list that has not loaded must never read as "no jobs".
2. **Say what you did not do.** *No salary listed*, *Location not parsed*, *Resume not uploaded*; a missing input is shown, not skipped.
3. **Plain verdict words.** *Not a fit: needs 8 years, you have 4 confirmed*. Not scores out of 100 with decimals.
4. **Undo and review before anything external.** Applying, emailing, saving to the extension's form: a preview first.
5. **Mobile is for triage.** Today, Jobs, Inbox and approvals work on a phone; the workspace, memory editor and insights are desktop-first.

### Opportunity workspace

> *Diagram — wireframe · Opportunity workspace, desktop*

The header carries the move and the verification state. The stepper shows where the job is. The left panel explains the move requirement by requirement, with fact ids; the middle is the timeline; the right holds documents and people. The approval bar appears whenever something waits for you, and it is the only place a document is approved. On a phone the three panels stack and the bar stays fixed at the bottom.

### Accessibility and performance

Visible focus on every control (the repo defines `--border-focus`), full keyboard operation and the command palette, text contrast checked in both themes, reduced-motion respected, and a loading skeleton shaped like the final content. A route should show something within a second on the free hosting tier or say why not.

## Feature specifications

**Each feature is specified the same way: the problem, who benefits, how it works, what data it needs, what changes in the backend and the screens, which outside services it uses, how we know it is done, and how we know it works.** Acceptance criteria are things a test or a reviewer can check. Metrics are measured on the user's own usage, so early numbers are small and must be shown with their sample size.

### A. Career Memory

| Item | Specification |
| --- | --- |
| Problem | Facts about the candidate sit in a resume, a profile and their head; they retype them for every application, and a model asked to help will fill gaps with inventions |
| Benefit | Enter once, review once; every resume line, answer and message can point at a fact the user agreed to |
| How it works | Upload a PDF or DOCX. A job extracts the text and a model proposes structured facts (roles with dates, projects, skills with where they were used, education, certifications). Each proposal shows the resume lines it came from. Only facts the user confirms are usable, as ADR-037 already requires. A GitHub repository URL can add proposals from its README and languages; these start unconfirmed. Confidence is a label (*stated*, *one project*, *several projects*), not a percentage |
| Data | Extend `profile_facts`: `structured` (JSON), `source` (resume, github, user, answer), `confidence`, `last_used_on`, `supersedes_id`. New `candidate_preferences` (target roles, locations, remote, salary range in INR, notice period, negotiable flag, work-authorisation notes) and `documents` (uploaded originals, sealed or in object storage) |
| Backend | Job kind `parse_resume`, idempotent on the file hash; `/api/v1/memory/*` routes; document text is untrusted input to the model; PII-allowed model entries only (`allow_pii`) |
| Frontend | Career Memory screen with five tabs; a review queue of proposed facts beside the source text; bulk confirm for untouched proposals |
| Outside services | The model router; optional GitHub REST for public repositories |
| Acceptance | A sample resume yields structured proposals with source lines; nothing unconfirmed reaches a prompt (existing test extended); editing a confirmed fact keeps the old version; deleting a fact removes it from answers that cite it, or flags them |
| Success measure | Minutes from upload to 20 confirmed facts; share of proposals confirmed without edit; share rejected as "not in my resume" (hallucination rate, target near zero) |
| Already in the repo | At commit e7ec2d7: profile\_facts with user, github and resume sources, confirm and edit routes, answer\_bank with approval, and a facts panel and answers screen in Job hunt. Not present: any PDF or DOCX reader, structured fields or preferences. The parser is a real build, not wiring. |

### B. Job discovery and truth engine

| Item | Specification |
| --- | --- |
| Problem | Postings are duplicated across sources, and some stay up after the role is filled; time is lost on both |
| Benefit | One entry per job, with an honest statement of how recently it was seen to be open |
| How it works | Sources: public ATS feeds (built), a pasted URL, extension capture and, later, career pages that publish structured `JobPosting` data. Jobs are de-duplicated on a canonical key (company domain, normalised title, location, canonical apply URL), keeping every source. A scheduled check marks a job *open* (present at last poll), *closed* (absent from two consecutive polls, or HTTP 404/410) or *unknown*. A *possibly stale* label appears only with listed observations: missing posted date, posted over a set age, page unchanged for a set period, the same role re-listed under a new id. Ages are configurable defaults, not facts about the market |
| Data | On `job_posts`: `first_seen_at`, `last_seen_at`, `last_verified_at`, `closed_at`, `canonical_key`. New `job_sources` and `job_checks` (checked time, method, result, status code, evidence JSON) |
| Backend | Handlers `verify_job` (scheduled, idempotent per job per day) and a dedupe step in intake; reuses the existing fetch guard in `research/net.py` and respects each site's robots rules |
| Frontend | Jobs list with verification labels; a verification history panel on the job |
| Outside services | Public ATS feeds and company pages only; no job-board scraping |
| Acceptance | One job from a feed and a pasted URL appears once with two sources; a job removed from a feed shows *closed* after two polls; every stale label lists its observations; no string in the product says "fake" or "ghost" |
| Success measure | Share of jobs shown as open that are still open seven days later; duplicate rate in a manual sample; jobs the user marks "this was open" after a stale label (false-stale rate) |

### C. Job fit and Next Best Move

| Item | Specification |
| --- | --- |
| Problem | Candidates apply to jobs they will not get and skip jobs they would, with no stated reason either way |
| Benefit | A recommended move per job, with the requirement-by-requirement reasons, so the user can agree or override in seconds |
| How it works | (1) A model extracts structured requirements from the posting as untrusted text: must-have and nice-to-have skills, minimum years, location and remote policy, stated salary, eligibility, seniority. (2) Deterministic rules compare each requirement with confirmed facts and preferences and mark it met (with fact ids), partly met, unmet or unknown. (3) A named rule table picks the move: **SKIP** on a hard blocker (closed, location incompatible, eligibility unmet, stated salary below the user's floor, years gap above a tolerance); **APPLY** when every must-have is met or at most one is partly met; **REFERRAL FIRST** when the fit reaches APPLY or is one step short and the user has a recorded contact at the company; **BUILD EVIDENCE** when the gaps are skills the user claims but cannot show, or one or two demonstrable skills. There are no numeric weights: thresholds are named constants the user can change, and are reviewed against outcomes before anything is learned |
| Data | New `fit_evaluations` (job, profile version, requirements, per-requirement results, move, reasons, evaluator version, created time). Recomputed when the posting text or the profile changes |
| Backend | Job kind `evaluate_fit`; routes to read an evaluation, re-run it, and record an override with a reason |
| Frontend | Requirement table with evidence chips, Move badge, "why" drawer, override control |
| Outside services | The model router, for extraction only |
| Acceptance | On a set of about 30 postings the user labels with their own move, the agreement rate is reported (the target is for the user to set after seeing the first report); every SKIP names its blocker; every met must-have lists fact ids; changing a salary floor re-evaluates open jobs |
| Success measure | Share of recommendations accepted; overrides by type; later, interview rate by move, shown with counts |
| Already in the repo | fit\_scores (ADR-055): computed when the user presses Score the fit, read free afterwards, with stored matched and missing lists and a two-shared-word threshold. The first P19 slice decides whether fit\_evaluations extends fit\_scores or supersedes it; the repo must not keep both. |

### D. Opportunity Workspace

| Item | Specification |
| --- | --- |
| Problem | Everything about one job lives in a different place: the posting in a tab, notes in a document, the recruiter in an inbox, the resume in a folder |
| Benefit | One screen and one timeline per job, so the next step is obvious and nothing is lost between tools |
| How it works | A job becomes an *opportunity* when the user saves or approves it, not when it is discovered. From then on, every action adds an event to an append-only timeline: fit evaluated, resume created, application marked, email received or sent, status changed, task added, note added. Each event records who or what did it (the user, the extension, the inbox agent). Tasks have due dates and fire in-app reminders once |
| Data | New `opportunities` (job post or manual entry, company domain, title, status, current move, strategy note, chosen resume version, closed reason), `opportunity_events` (append-only), `tasks`, `notes` |
| Backend | CRUD routes under `/api/v1/opportunities`; a status transition function in the domain layer, like `domain/state.transition()` for leads, so no route writes a status directly; reminder job idempotent per task and due date |
| Frontend | The workspace screen (wireframe below), the Pipeline board, and the Today list |
| Outside services | None |
| Acceptance | Every status change writes an event with an actor; the timeline cannot be edited through the API; closing requires a reason; a reminder fires once even if the job runs twice |
| Success measure | Share of open opportunities whose status is current (changed or confirmed within 14 days, an assumed window); tasks completed by their due date |
| Already in the repo | applications (job post, resume version, status planned, applied, interviewing or closed, applied\_at, follow\_up\_due, notes) and an Applications panel in Job hunt. The opportunity should wrap it: add the timeline, tasks, strategy note and closed reason, and migrate or reference applications instead of creating a parallel table. |

### E. AI Application Copilot

| Item | Specification |
| --- | --- |
| Problem | Tailoring a resume and rewriting the same screening answers for each application takes an hour, and generic AI output reads as generic |
| Benefit | A tailored resume, cover letter and answer set in minutes, in which every claim traces to a fact the user confirmed |
| How it works | *Resume.* The model proposes **edit operations** on the base resume (include or drop a bullet, reorder, rephrase within one fact, adjust the summary). A validator then checks every sentence of the result against confirmed facts and rejects any number, skill, employer or title not found there. The accepted result is stored as an immutable `resume_versions` row with its operations and fact ids, then rendered to PDF by a separate, memory-limited job. *Cover letter.* Written from facts plus cited company research, to a word limit. *Screening answers.* The approved answer bank first; for a new question, a draft from facts, fitted to the form's character limit and re-validated after shortening. *Forms.* The extension proposes answers from this material in its side panel; the user edits, accepts and submits. *Applied.* Only the user marks an application as applied, and the row records the exact resume version |
| Data | New `resume_versions`, `application_documents` (type, content, fact ids, opportunity), `applications` (opportunity, resume version, status, applied time, channel) |
| Backend | Job kinds `tailor_resume`, `render_resume`, `draft_cover_letter`, `draft_answer`; validator in the domain layer, shared with the draft claim check; routes under `/api/v1/opportunities/:id/documents` |
| Frontend | Resume diff view (base against tailored, each changed line with evidence chips), cover letter editor with word count, answers list with character counters, approval bar |
| Outside services | The model router; a PDF renderer chosen in the first slice by measuring memory on the production box (candidates: WeasyPrint, headless Chromium) |
| Acceptance | A tailored resume containing a skill, number or employer absent from confirmed facts is rejected, with the sentence named; every line of every version lists fact ids; a version cannot be updated, only superseded; a generated answer never exceeds its stated limit; the extension cannot set an application to applied |
| Success measure | Share of generated text the user keeps unchanged; minutes from opening a job to ready-to-apply; replies per resume version, shown with counts |
| Already in the repo | services/resume (ops, validate for numbers and named tokens, tailor, store, pdf; ADR-043); resume\_versions with edit\_ops, rendered\_text, fact\_ids; tailor, render and PDF routes; a versions list with a reader and a Render PDF button; applications. This row supersedes the Data cell above where it calls resume\_versions and applications new. Not present: cover letters, character-limit fitting, an uploaded base resume, a diff against it, an approval bar. |

### G. Recruiter Inbox Agent

| Item | Specification |
| --- | --- |
| Problem | Recruiter mail is mixed into a personal inbox; replies are late, invites are missed, follow-ups are forgotten |
| Benefit | Every job thread in one place, linked to its opportunity, with a drafted reply and the dates that matter pulled out |
| How it works | The user connects Gmail by OAuth and chooses the scope of reading: **only threads we sent or that carry a label the user picks** (default), or the whole inbox (opt-in). The existing inbox loop reads with `BODY.PEEK` and a UID cursor. A thread is linked to an opportunity by sender domain, the company name in the text, or because we sent the first message. A model classifies it with the existing approach (rules first, model second) using added labels: interview invite, screening request, scheduling, information request, rejection, offer, recruiter outreach, not job-related. Dates in invites are proposed as tasks and become real only when the user confirms. A reply draft is prepared with the candidate persona and waits for approval. Follow-up schedules and the stop-on-reply and stop-on-opt-out rules reuse the existing scheduler |
| Data | Link table `thread_opportunities`; new labels in the reply enum; `tasks` for proposed dates. Mail outside the chosen scope is neither stored nor sent to a model |
| Backend | Extend the IMAP loop with the scope filter; new classifier labels and their policy table (what each label may cause); Outlook through Microsoft Graph in a later phase |
| Frontend | Inbox screen grouped by opportunity; a draft with the approval bar; a card for each detected invite with *Add task* and *Dismiss* |
| Outside services | Gmail (IMAP with OAuth now). Reading the whole inbox through the Gmail API needs a restricted scope, which Google reviews before a public app can use it (I have not re-read the current policy); IMAP with OAuth or an app password is the path the repo already uses |
| Acceptance | A reply to a sent message is linked to its opportunity; an invite produces a proposed task and no calendar entry; no message is sent without approval; with the label scope, unrelated mail is never stored (tested with a fixture inbox); an opt-out or reply stops the follow-up sequence |
| Success measure | Classification agreement with the user's corrections (counts per label); time from invite to reply; invites the user reports as missed |

### I. Career Intelligence and Outcome Learning

| Item | Specification |
| --- | --- |
| Problem | Candidates cannot tell which resume, source or approach produces replies, and rely on folklore |
| Benefit | Their own funnel and comparisons, with the uncertainty visible |
| How it works | Stages: saved, applied, replied, screened, interviewed, offer, rejected. Silence is shown as *no response yet*, never as rejection (ADR-026, extended to applications). Views by resume version, by move, by source, and by referral against cold. Any group under 10 items (an assumed floor) shows *too few to compare*. Insights are worded as observations, with the count and a note that no cause is shown. There are no hiring-probability claims |
| Data | Application and interview events from the opportunity timeline; an export to CSV. Optional manual time entries per opportunity |
| Backend | Query layer over events; extend `export_dataset.py`; no model needed for the MVP |
| Frontend | Insights screen: funnel, comparison tables with counts, a weekly summary on Today |
| Outside services | None |
| Acceptance | No number appears without its count; silence is never counted as a rejection; a group below the floor is not ranked; the CSV export matches the screen |
| Success measure | The user reports, in a one-tap weekly prompt, whether the summary changed what they did; clicks are not the measure |

### F. Warm Path Navigator (after the MVP)

| Item | Specification |
| --- | --- |
| Problem | A referral is the strongest way in, and most candidates never check who they know at a company |
| Benefit | For each opportunity, the people the user already knows there, a drafted note that fits the relationship, and a record of the request |
| How it works | The user uploads the connections CSV they export from LinkedIn themselves (column names to be checked against a real export) or adds contacts by hand, with a relationship note, how well they know them, and tags such as alumni or former colleague. Paths are found by company match and tags; a person the system cannot place is not guessed. The note is drafted from the relationship note, the job and the user's confirmed facts, and the user sends it themselves. If the contact has an email address, an email can go through the normal approval gate. Requests are tracked: asked, replied, referred, declined |
| Data | New `contacts`, `contact_notes`, `referral_requests`. Contacts are other people's personal data: stored per workspace only, deletable, never shared or pooled |
| Backend | Idempotent CSV import (1,000 rows); company normalisation shared with job intake; a structural test that no LinkedIn host is fetched anywhere |
| Frontend | Network screen; a *People you know here* panel on the opportunity; the referral request tracker |
| Outside services | None; no LinkedIn access of any kind |
| Acceptance | Importing the same file twice creates no duplicates; a path states which contact fields produced it; a draft cites the relationship note; no request is sent without approval |
| Success measure | Referral requests per application; replies and referrals with counts |
| Already in the repo | connections imported from the user's own LinkedIn export (ADR-038, ADR-042), a referral finder with a drafted note, contact\_candidates lookups with the user's own provider keys, and a test that nothing fetches LinkedIn. This row supersedes the How it works and Data cells above for import and matching. Not present: relationship notes and strength, referral request states, a link to an opportunity, a Network screen. |

### H. Interview War Room (after the MVP)

| Item | Specification |
| --- | --- |
| Problem | Prep is scattered and generic, and candidates do not know which of their weak spots matters for this interview |
| Benefit | A prep pack specific to the company, role and the user's gaps, a plan counted back from the interview date, and practice with feedback |
| How it works | An invite detected by the inbox agent (or added by hand) creates an *interview* with round, type and date. The prep pack has: cited company facts from the shared cache, likely topics from the posting ranked by the user's gaps in the fit evaluation, and behavioural story outlines built from confirmed facts. The study plan fills the days before the interview. In a mock session the model asks questions, adapts to answers, and grades each answer against a written rubric (named dimensions with descriptions of each level), quoting the part of the answer that justifies each grade. Weak topics produce practice items. LLM grading is an unproven judge here; scores are feedback, calibrated by letting the user mark grades as fair or unfair |
| Data | New `interviews`, `prep_packs`, `mock_sessions`, `mock_turns`, `rubric_scores`, `practice_items` |
| Backend | Job kinds `build_prep_pack`, `mock_turn`; a daily cap on mock-interview model calls, because multi-turn sessions are the costliest feature; rubrics versioned in code |
| Frontend | Prep screen: pack, plan, mock session, progress by topic |
| Outside services | The model router; speech input is out of scope for the first version |
| Acceptance | Every company fact in a pack cites a source; stories use only confirmed facts; every grade quotes the answer; a session stops at its cap with a clear message |
| Success measure | User ratings of pack usefulness and grade fairness; repeat practice on weak topics. No claim about offers |

### Future modules (designs only)

| Module | What it would do | What the MVP must leave room for |
| --- | --- | --- |
| Skill Proof Studio | Generate a role-specific assignment, evaluate the result against a rubric, publish a shareable evidence page | Facts with an evidence URL (exists); the rubric engine from H |
| Opportunity Radar | Watch companies for growth and hiring signals, propose opportunities before a posting exists | The feed poller and `watched_boards` (exist); signals propose, never create |
| Career Path Simulator | Compare alternative roles against Career Memory and list the skills and experience to bridge the gap | Structured facts with dates and the requirement extractor from C |

## Database schema

**Stay on Postgres with ordinary tables; add 19 tables and extend 8, with `opportunities` at the centre.** No graph database and no vector extension in the MVP (reasons below). Every table carries `org_id` and goes through `OrgContext`, as in ADR-004; the one exception, `company_profiles`, already exists under ADR-038.

### New tables

| Table | Key columns | Notes |
| --- | --- | --- |
| `candidate_preferences` | `org_id`, target roles\[\], locations\[\], remote ok, `salary_min_inr`, `salary_max_inr`, `notice_days`, `notice_negotiable`, authorisation notes | One row per workspace, versioned; a change re-evaluates open jobs |
| `documents` | `org_id`, kind (resume, offer, other), filename, sha256, storage key, text, `uploaded_at` | Original files; dedupe on hash |
| `job_sources` | `job_post_id`, source, external id, URL, `first_seen_at`, `last_seen_at` | Many sources per job |
| `job_checks` | `job_post_id`, `checked_at`, method (feed, http, page hash), result, status code, evidence JSON | The verification history |
| `fit_evaluations` | `job_post_id`, `profile_version`, `posting_hash`, requirements JSON, results JSON, move, reasons\[\], `evaluator_version` | Unique on the four version fields so a re-run is idempotent |
| `opportunities` | `org_id`, `job_post_id` (nullable for manual entries), company domain, title, status, current move, strategy note, `resume_version_id`, closed reason | Unique on `(org_id, job_post_id)` when present |
| `opportunity_events` | `opportunity_id`, type, actor (user, extension, inbox, system, mcp), payload JSON, `occurred_at`, `idempotency_key` | Append-only, like `events` |
| `tasks` | `opportunity_id`, title, due, status, source (user, invite, system) | Proposed tasks need confirmation |
| `notes` | `opportunity_id`, body, author |  |
| `application_documents` | `opportunity_id`, kind (cover letter, answer set), content, `fact_ids[]`, `approved_at` |  |
| `contact_notes` | `contact_id`, how we know each other, last contact |  |
| `referral_requests` | `opportunity_id`, `contact_id`, state, `asked_at`, `answered_at` |  |
| `thread_opportunities` | `thread_key`, `opportunity_id`, linked by (rule, user) | Evidence for each link |
| `interviews` | `opportunity_id`, round, type, `scheduled_at`, source, status |  |
| `prep_packs` | `interview_id`, sections JSON, source URLs\[\], `generated_at` |  |
| `mock_sessions`, `mock_turns` | session, topic, rubric version; turn question, answer, grade |  |
| `rubric_scores` | `mock_turn_id`, dimension, level, quoted evidence |  |
| `practice_items` | `org_id`, topic, prompt, `next_due` |  |

### Tables that change

| Table | Change |
| --- | --- |
| `profile_facts` | Add `structured`, `confidence`, `last_used_on`, `supersedes_id`; extend `source` values |
| `job_posts` | Add `first_seen_at`, `last_seen_at`, `last_verified_at`, `closed_at`, `canonical_key` |
| `classifications` | New recruiter labels in the enum and its policy table |
| `jobs` | New job kinds (`parse_resume`, `evaluate_fit`, `verify_job`, `tailor_resume`, `render_resume`, `build_prep_pack`, `mock_turn`) |
| resume\_versions (exists) | Add a nullable opportunity\_id and base\_document\_id. It stays immutable; PDFs already live in resume\_renders. Reuse it; do not create a second table. |
| applications (exists) | Add a nullable opportunity\_id and channel, backfilled when an opportunity is created from one. applied\_at stays set only by the user. Touches existing rows, so the pull request carries needs-human. |
| connections (exists) | Stands in for the contacts table in this design. Add strength, tags and a relationship note. Wherever contact\_id appears in the new tables, it means connections.id. |
| fit\_scores (exists, ADR-055) | Either extend it with requirements, results and move, or supersede it with fit\_evaluations and migrate; the first P19 slice decides, and only one of the two may remain. |

### Relationships

- A `job_post` has many `job_sources` and `job_checks` and many `fit_evaluations` (one per profile and posting version).
- An `opportunity` belongs to at most one `job_post`; it has many events, tasks, notes, documents, referral requests, interviews and thread links, and at most one current `resume_version`.
- An `application` joins one opportunity to one immutable `resume_version`.
- A `contact` has many notes and many referral requests; a referral request belongs to one opportunity.
- A `mock_session` belongs to an `interview` or stands alone for general practice.
- Every generated document and answer lists `fact_ids[]` that must exist, be confirmed, and belong to the same workspace.

### Decisions on technology

- **No graph database.** The questions asked (contacts at a company, jobs at a company, threads for an opportunity) are one or two joins. A graph store would add a second system to run on a 2 GB host for no query that Postgres cannot answer.
- **No pgvector in the MVP.** Matching a requirement to a fact and finding an earlier answer to a similar question are done with Postgres full-text search plus a model-assisted check, over at most a few hundred facts per user. Adding vectors means replacing the database image, choosing an embedding model and re-embedding on every edit. Revisit in P23 if the evaluation shows lexical matching missing real matches.
- **Object storage for files.** Resumes and rendered PDFs are stored under keys in the S3 bucket already used for backups, with sealed or private access; the database holds keys and hashes.
- **Retention.** Captures stay 7 days (ADR-039); uploaded documents and rendered PDFs stay until the user deletes them; the erasure path covers every new table, with a test that proves it.

## Backend API

**About 60 routes in six groups, all under the existing three doors; anything that calls a model or the web returns `202` with a job id and the screen polls.** The doors stay as ADR-039 defined them: `/api/v1` for the signed-in user, `/ext/v1` for the extension token, `/mcp` for the connector. Paths below are proposals for the plan, not final names.

**Conventions.** Cursor pagination as in `pagination.py`. A role check on every write (viewer, member, admin as today). Idempotency key header on every write that creates a record. Errors carry a code the screen can map to words. No route returns a stored credential or the full text of a mail that is outside the user's chosen scope.

**Routes that already exist are extended, not replaced.** At `e7ec2d7` the signed-in door has the candidate facts and answers routes, `job-posts` with `fit` (GET to read, POST to score), `watched-boards`, `resume-versions` with `tailor`, `render` and `pdf`, `applications`, `connections` import, `referrals`, `contact-candidates`, `watchlist`, `signals`, `graph`, `booking` and `searchkeys`. The `/memory/*` and `/opportunities/*` paths below are additions; the old paths keep working until their screens move, then are deprecated in one slice with a test that no screen still calls them (the ADR-048 guard).

| Group | Routes | Notes |
| --- | --- | --- |
| Career Memory | `GET/POST /memory/facts`, `PATCH/DELETE /memory/facts/:id`, `POST /memory/facts/:id/confirm`, `POST /memory/facts/bulk-confirm`, `GET/PUT /memory/preferences`, `GET/POST /memory/answers`, `POST /memory/answers/:id/approve`, `POST /memory/documents` (upload), `GET /memory/documents/:id`, `POST /memory/documents/:id/parse` (202), `POST /memory/import/github` (202) | Replaces `/profile/facts` and `/profile/answers`; keep the old paths as aliases for one release |
| Jobs | `GET /jobs` (filters: move, status, source, freshness), `POST /jobs` (URL or text), `GET /jobs/:id`, `GET /jobs/:id/checks`, `POST /jobs/:id/verify` (202), `GET /jobs/:id/fit`, `POST /jobs/:id/fit` (202, re-run), `POST /jobs/:id/fit/override`, `GET/POST/DELETE /watched-boards` | Existing `job-posts` and `watched-boards` routes are extended, not replaced |
| Opportunities | `GET/POST /opportunities`, `GET/PATCH /opportunities/:id`, `POST /opportunities/:id/status`, `GET /opportunities/:id/events`, `POST /opportunities/:id/notes`, `GET/POST /opportunities/:id/tasks`, `PATCH /tasks/:id`, `GET /today` | Status changes only through the transition endpoint |
| Documents and applications | `POST /opportunities/:id/resume` (202, tailor), `GET /resumes`, `GET /resumes/:id`, `GET /resumes/:id/pdf`, `POST /opportunities/:id/cover-letter` (202), `POST /opportunities/:id/answers` (202), `PATCH /documents/:id`, `POST /opportunities/:id/applications`, `POST /applications/:id/mark-applied` | A resume is never PATCHed; a new version is created |
| Network | `POST /network/import` (CSV), `GET/POST /contacts`, `PATCH/DELETE /contacts/:id`, `GET /opportunities/:id/paths`, `POST /opportunities/:id/referrals`, `PATCH /referrals/:id`, `POST /referrals/:id/draft` (202) | After the MVP |
| Inbox and interviews | `GET /inbox/threads`, `GET /inbox/threads/:id`, `POST /inbox/threads/:id/link`, `POST /inbox/threads/:id/draft` (202), `POST /drafts/:id/approve`, `GET/POST /interviews`, `POST /interviews/:id/prep` (202), `POST /mock-sessions`, `POST /mock-sessions/:id/turn` (202), `GET /insights/funnel`, `GET /insights/compare`, `GET /insights/export.csv` | The draft approval route is the existing review endpoint, generalised |

**Extension routes (`/ext/v1`).** Existing: `whoami`, `suggest`, `suggestions/:id/outcome`. Added: `POST /jobs/capture` (page text or URL, user pressed Capture), `GET /jobs/:id/match` (move and top reasons, read only), `GET /resumes/active` (the version chosen for this opportunity), `POST /referrals/draft` (202). No route lets the extension send, approve, mark applied or read mail.

**Connector tools (`/mcp`).** Read tools added after E: `list_opportunities`, `get_opportunity`, `get_fit`, `list_facts` (confirmed only, with exclusions the user set). Write tools stay limited to proposals: `propose_draft` and `propose_reply` exist; `propose_note` is the only addition. No send, approve, apply or `SEND_MODE`.

## Workflow architecture

**One modular monolith with a queue, as today. The intelligence layer proposes; a separate, small action layer executes, and only after a visible approval.** There is no autonomous multi-agent loop: each "agent" is a fixed pipeline of queued steps, with a model used as a function that returns structured output. That keeps every step retryable, testable and explainable, and it fits a 2 GB host.

> *Diagram — architecture · two layers, one approval gate, shared stores*

Clients reach the system through three doors. The intelligence layer writes rows marked proposed; the action layer runs only on rows you approved or on changes you made yourself.

### Two layers, one rule

- **Intelligence layer** (pure or read-only): resume parsing, requirement extraction, fit evaluation, move choice, document drafting, mail classification, task proposals. Its output is always a row marked *proposed*, with the evidence it used.
- **Action layer** (the only code with side effects): sending mail through the outbox, rendering and storing files, fetching pages, writing a status. Each action reads an *approved* row, or is a status change the user made. A structural test asserts the intelligence modules import nothing that can send, as the extension gateway test already does.
- **Events drive follow-on work.** Writing an `opportunity_events` row, in the same transaction, enqueues the jobs that react to it through a fixed table in code (for example *job saved* queues `evaluate_fit`; *invite detected* queues `propose_task`). No message broker is added; the Postgres queue with `dedupe_key` is enough and already crash-safe.

### External actions and their gates

| Action | Gate | Executed by |
| --- | --- | --- |
| Send an email (recruiter reply, referral note, follow-up) | Draft in `pending_review`, user presses Approve; `SEND_MODE` and the allowlist still apply | Existing outbox and `SendLoop` |
| Submit a job application | Never by the product. The user submits in the employer's page; the extension may fill fields the user accepted | The user |
| Mark an application as applied | User action only | The user, in the web app |
| Create a calendar entry from an invite | User presses *Add* on the proposed task | The user, later an export link |
| Read mail | Per-user scope choice; label scope by default | Inbox loop, read only |
| Fetch a company page or feed | Allowed hosts and the SSRF guard; per-workspace daily slots | Fetcher |
| Store or delete a document | User upload or delete | API |
| Export data | User request; contains only their workspace | API |

### Status machine for an opportunity

Eight states: *saved*, *preparing*, *ready*, *applied*, *in conversation*, *interviewing*, *offer*, *closed*. *Closed* carries a reason: rejected, withdrawn, job closed, skipped, no response, offer accepted, offer declined. One function in the domain layer owns all transitions, and a database constraint plus a test reject any other path.

> *Diagram — opportunity status machine · 8 states, one user-only step*

The top row is the preparation path; the second row runs right to left from the first reply to an offer. The table below says who may cause each move.

| From | To | Who may cause it |
| --- | --- | --- |
| saved | preparing | User (opens the workspace) or the system when a resume is requested |
| preparing | ready | User, after approving documents |
| ready | applied | User only |
| applied | in conversation | Inbox agent proposes on a recruiter reply; the user confirms |
| in conversation | interviewing | Inbox agent proposes on an invite; user confirms |
| interviewing | offer | User |
| any open state | closed | User; the system may propose *job closed* from the verification check and the inbox agent may propose *rejected*, both needing confirmation |
| closed | saved | User (reopen) |

*No response* is a proposal after a set number of days with no reply (default 21, an assumption), and the stage is shown as *no response yet* until the user closes it, which keeps silence from being counted as rejection.

### Pipelines

1. **Intake.** Discover or paste, then dedupe, then fetch body, then verify, then extract requirements, then evaluate fit, then appear in Jobs with a move. Each step is its own idempotent job; a failure leaves the job visible with the step that failed.
2. **Tailoring.** Propose edit operations, validate against facts, on rejection repair up to twice with the validator's messages, then stop and show the user what could not be supported. Render only a validated version.
3. **Inbox.** Read within scope, link to an opportunity, classify, apply the label's policy (stop follow-ups, propose a task, draft a reply), wait for the user.
4. **Interview prep.** Triggered by an interview row; builds the pack from cached company facts and the fit gaps; mock turns are separate requests so a session survives a restart.

### Failure behaviour

A killed worker leaves a lease that expires and the job returns to the queue (exists, F21). A model outage falls to the next entry in the router chain, or marks the step *waiting for capacity* with the reason shown, never silently skipped. A step that depends on a missing input (no resume, no preferences) stops with a message naming the missing input.

## External integrations and their limits

**Job coverage will be partial by design: only sources that publish data for this purpose are used, so the product must say what it covers rather than imply it sees the whole market.** Limits marked *unverified* come from my background knowledge or from this document's earlier research and should be checked while building the slice that depends on them.

| Integration | Used for | Limits and how v4 copes |
| --- | --- | --- |
| Greenhouse, Lever, Ashby public job feeds | Job discovery and verification | Cover only employers using those systems; no Workday or custom portals. The Jobs screen shows which sources a company is seen on, and the extension covers the rest by user capture |
| Company career pages | Discovery where a page publishes structured job data | Per-site terms and robots rules apply; one request per company per day; skipped where unclear |
| Gmail (IMAP with OAuth, per user) | Recruiter inbox | Google client is in Testing mode, so refresh tokens last 7 days until the app is verified (ADR-023). Whole-inbox access through the Gmail API needs a restricted scope with Google's review (*unverified*); the default label scope avoids it |
| Outlook / Microsoft 365 | Recruiter inbox, later | Needs a Microsoft app registration and consent; tenant admin policies can block it (*unverified*). Not in the MVP |
| Model providers via the router | Parsing, extraction, drafting, classification, mock interviews | Free tiers have rate limits and may change; resumes are personal data, so only entries marked `allow_pii` are used for them, and the Settings screen lists which providers see what |
| Tavily search | Company research when evidence is thin | Free credits are finite; the shared cache and per-workspace fair share already limit use |
| GitHub REST | Optional proposals from public repositories | Rate-limited when unauthenticated (*unverified*); only public data; proposals start unconfirmed |
| Object storage (the existing S3 bucket) | Resumes and rendered PDFs | Cost grows with files; per-user size cap and a delete path |
| PDF and DOCX parsing | Resume upload | Scanned or image-only PDFs yield no text; the screen says so and asks for another file |
| LinkedIn | Nothing automated | No scraping, no automated messages or connection requests, no reading profile pages. The user may upload their own data export. LinkedIn's terms forbid automation (not rechecked in this pass) |
| Naukri, Instahyre, Cutshort | Not integrated | I found no candidate-side API for them in this pass (not exhaustively searched). Cutshort's MCP server is for employers. Users paste URLs or capture jobs in the extension |
| Calendar | Interview dates | MVP: tasks plus a downloadable calendar file the user opens. A calendar API is a later step and would need its own consent |

## Chrome extension

**Grow the existing side panel in three small steps (save a job, show the match, fill accepted fields) without giving it any standing access to pages or any way to submit.** Today it is a Manifest V3 side panel, strict TypeScript, no content scripts, no packaged host permissions, mappers shipped inside the package, and it explicitly suggests only: its README says it does not fill anything in. Every step below widens that on purpose, with a decision recorded in an ADR and the README and `PRIVACY.md` updated in the same pull request.

| Capability | How it works | What it adds beyond today | Guard |
| --- | --- | --- | --- |
| Save a job | User presses *Save job*. A script injected under `activeTab` reads the page's structured job data if present (the `JobPosting` markup many career pages publish), else the visible posting text, and sends it to `POST /ext/v1/jobs/capture` | Page text leaves the browser, but only on that press (the `captures` table and its 7-day retention already exist) | Never on a page the user did not press the button on; the panel shows what will be sent before it is sent |
| Show the match | The panel calls `GET /ext/v1/jobs/:id/match` for the saved or recognised job and shows the move and the top reasons | Read only; no model call in the request | Result is the stored evaluation, so the panel and the web app can never disagree |
| Fill accepted fields | Per field, *Use this answer* writes the text into the input and fires the page's input events; the user reviews and submits | The first time the extension writes to a page | Text and select fields only; **no file inputs** (browsers do not allow it, and the user attaches the resume themselves); no click on any submit or apply control, with the existing no-submit test extended |
| Answers from Career Memory | Unchanged: approved answers first, then drafts from confirmed facts flagged as drafts | Character-limit fitting from the form's declared limit | Anything not in the bank is marked *needs you* |
| Draft a message | A button asks the server for a referral or recruiter note for this opportunity and copies it to the clipboard | New route, no page access | The note is shown for editing; it is never sent from the extension |
| Pick the resume | Shows which resume version the opportunity uses and offers its PDF for download | Reads `GET /ext/v1/resumes/active` | Download only |

**Where it will not work.** Pages that render forms in a closed shadow root or inside a cross-origin frame cannot be read; the panel says the scan is incomplete, as it does now. Multi-page applications keep one session per tab in memory, so answers accepted on page one are available on page two and vanish when the tab closes.

**LinkedIn, by design.** The package contains a list of hosts it will not run on, starting with `linkedin.com`, and a test that asserts the capture and fill scripts refuse them. On such a page the only action is *Save link*, which sends the address and tab title the user already sees; the user pastes the description if they want a fit evaluation. I chose this reading of the requirement (no DOM extraction, no automated messages or connection requests); whether even saving a link is acceptable under LinkedIn's terms is your call to check.

**Structure.** Keep the three-part layout (service worker, side panel, injected collectors), generate API types for the extension from the server's OpenAPI document so the two cannot drift, and keep mappers as fixture-tested modules per ATS: Greenhouse, Lever and Ashby first, then one more only when the user's own applications call for it. Tests: Playwright against saved form pages including a React-controlled input, a test that no file input is ever written, and the host-denylist test.

**Distribution.** Load unpacked while you are the user. A public listing needs a privacy policy, a single-purpose description and a review that can take days; decide that after the MVP has been used on real applications (see the open questions).

## Security, privacy and platform compliance

**v4 stores a person's career history, their mail and other people's contact details, which is a heavier duty than the outreach product carried; the controls below are requirements for the slices, not later hardening.** Legal points are flagged for professional advice; I am not a lawyer and did not re-read the statutes.

### What is sensitive

| Data | Why it is sensitive | Handling |
| --- | --- | --- |
| Resume, salary range, notice period, interview notes | Identifies the person, can harm them with an employer | Private bucket with encryption at rest (confirm default encryption is on), access only through the API, deleted on request |
| Recruiter mail | Third parties' words and the user's job search | Read only within the user's chosen scope; unrelated mail never stored or sent to a model |
| Contacts | Other people's personal data, gathered without their knowledge | Per workspace, never shared or pooled, deletable, minimal fields |
| Model prompts | Copy personal data to a provider | Only `allow_pii` entries; Settings lists which providers receive which data |

### Controls

1. **Untrusted text everywhere.** Postings, recruiter mail, resumes and web pages are data. Models return structured output only, have no tools with side effects, and their output passes validators (the fact check, the status machine) before it changes anything. The MCP untrusted-text wrapper already exists; the same wrapper marks these inputs in prompts.
2. **No external action without approval.** The table in the workflow section is enforced by structural tests, not by convention.
3. **Three doors, three token types.** A token for one door fails on the others (tested). New routes need their own structural test, as the repo already requires.
4. **Tenant isolation test over every new table.** The existing isolation test is extended; `company_profiles` keeps its opposite test.
5. **Export and erase.** One export (JSON plus files) and one erase path covering every new table and stored file, each with a test.
6. **Logs.** The existing scrubber removes secrets; add resume text, email addresses and phone numbers to what is scrubbed, with tests like the ones that pass a secret through every path.
7. **Backups hold personal data.** Backups are encrypted and expire; erase requests are honoured in live data immediately and in backups when they expire. State the period in `PRIVACY.md`.
8. **Words that cannot defame.** Verification labels state observations (*page unchanged 45 days*), never an accusation. No employer is called fake.
9. **Consent where it is real.** A first-use notice for resume upload, mail access and contact import says what is stored, who sees it (including model providers) and how to delete it.

### Rules the outside world sets

- **Gmail data.** Google's API Services User Data Policy has *limited use* requirements, which I recall include a ban on using the data to train general AI models (verify the current text). Consequence: the outcome dataset must hold the user's own labels and counts, not mail text, and any future model training on mail content is off the table without a separate review.
- **LinkedIn.** Its terms prohibit automated access and automated messaging (not rechecked here). v4 contains no code that contacts LinkedIn and a test that proves it.
- **Chrome Web Store.** No remotely hosted code, a single purpose, a privacy policy and honest data disclosure (the existing design already follows the first).
- **India's DPDP Rules** (notified November 2025, phased in; not re-read). A user's own data and a third party's contact details are different cases. The hosting region is Sydney; confirm what the Rules say about cross-border processing before onboarding anyone but yourself.
- **Employer sites.** Public feeds and structured data only, one request per company per day, robots rules honoured.

## Evaluation and success metrics

**Judge every component that uses a model against a small set of real, hand-labelled cases before it ships, and judge the product on interviews and effort saved, not on applications sent.** The sets are small because one person's job search is small; numbers are reported as counts, and nothing is called significant on a single user's data.

### Component evaluations (gates for each phase)

| Component | Test set (assumed sizes) | Measure | Gate |
| --- | --- | --- | --- |
| Resume parsing | 5 to 10 real resumes, facts labelled by hand | Facts found against missed; invented facts | Zero invented facts on the set |
| Requirement extraction | 30 postings with must-haves labelled by hand | Share of must-haves found | Reported; user decides the bar after seeing it |
| Fit and move | The same 30 postings, move chosen by the user | Agreement and a confusion table of the four moves | Every SKIP has a named blocker; agreement reported |
| Job verification | 50 jobs shown open and 50 shown closed, re-checked by hand | Precision of *open* and of *closed* | Reported; stale labels always list observations |
| Resume tailoring | A set of base resumes with fake skills and numbers injected into the proposed edits | Share of injected claims rejected | 100% rejected |
| Answers and letters | Every generated answer | Fact ids present; limit respected | 100%, checked in tests, not sampled |
| Mail classification | 100 recruiter emails from the user, labelled | Precision and recall per label | Recall on interview invites and rejections reported separately |
| Mock grading | The same answer graded three times; user fairness ratings | Spread between runs; share rated fair | Reported; scores shown as feedback only |

### Product metrics

| Metric | Definition | Why it matters |
| --- | --- | --- |
| Time to first value | Minutes from sign-up to 20 confirmed facts and one evaluated job | Tests whether set-up is bearable |
| Time to ready-to-apply | Median minutes from opening a job to *ready* | The effort the product is meant to cut |
| Recommendation acceptance | Moves the user keeps against overrides, with override reasons | Whether the reasons persuade |
| Replies, screens and interviews per application | By resume version, move and source, each with its count | The outcome that matters; descriptive only |
| Effort per interview | Hours logged (optional) divided by interviews | The stated goal: less effort for qualified interviews |
| Weekly usefulness | One-tap rating on the weekly summary | Direct signal that a person can give in a second |

**Guardrails** (watch, do not optimise): applications per week should not become the target; user-reported wrong facts in output must be zero; rejected unsupported claims should be caught by the validator, not by the user; model cost per active user per month stays within the budget set in the cost section.

### Learning from outcomes

1. **Log the recommendation when it is shown.** Store the move, its reasons and the evaluator version at that moment; later changes never rewrite it.
2. **Compare, then decide.** After enough outcomes, compare move against result and check whether any rule threshold looks wrong. Change a threshold by an ADR, not silently.
3. **A learned ranking waits.** Not before there are outcomes from many users who agreed to share them; one user's history cannot train one.
4. **Know the blind spot.** Users act on APPLY and skip SKIP, so skipped jobs have no outcomes; the data cannot tell whether a SKIP was a mistake. Say so on the Insights screen.

## Phased roadmap

**Eight phases (17 to 24) in three bands, with two gates before anyone but you uses it.** Phase numbers continue v3's. Order follows dependency: the screen shell first, then the facts everything cites, then jobs, then the workspace that holds them, then documents, then mail. Sizes are guesses (S a few slices, M a phase of work, L more); no dates are given because the work is solo and each slice ends in a review by you.

> *Diagram — roadmap · 8 phases, 3 bands, 3 gates*

Read top to bottom. A gate is a condition the earlier work must meet, not a date. The opening gate can be passed before P23 if you want other users sooner.

| Phase | Slices | Done when | Needs | Size |
| --- | --- | --- | --- | --- |
| **P17 Foundations** | Rewrite the `PROGRESS.md` handoff header; run the suite and record the real baseline; add a router and split `App.jsx` (4,983 lines) following the pattern of `JobHunt.jsx`, with no behaviour change; add the component kit (move badge, evidence chip, approval bar, loading and empty states); add a workspace-type setting and a job-hunt navigation shell with campaigns, Sending, Signals and Operations under *Legacy*; generate API types for web and extension; register new routes in the ADR-048 guard | Every existing screen has a URL and still passes its tests; a link to `/leads` opens that screen on reload; the shell shows the new navigation for a job-switch workspace and the old one for an outreach workspace | v3 merged through P16 (S16.3 and S16.4 may stay open) | M |
| **P18 Career Memory** | Spike on real resume PDFs; preferences and documents tables; resume upload to the bucket; a PDF and DOCX reader; `parse_resume` job; extended `profile_facts`; Memory screen with the review queue, built from the facts panel in Job hunt | Component evaluation passes with zero invented facts; nothing unconfirmed reaches a prompt; erase removes documents and facts | P17 | M |
| **P19 Jobs and moves** | Verification and dedupe; `job_checks`; requirement extraction; extend or supersede `fit_scores` with the four-move rules; Jobs screen from the jobs section of Job hunt; extension *Save job* and *Show match* | Feature B and C acceptance; the fit agreement report on the user's 30 labelled postings is produced | P18 | L |
| **P20 Opportunities** | `opportunities`, events, status machine, tasks, wrapping `applications` with a backfill (`needs-human`); Opportunity screen; Pipeline board; Today | Feature D acceptance; a status can change only through the transition function (test) | P19 | M |
| **P21 Application Copilot** | Attach the existing tailoring and PDF to an opportunity; use the uploaded base resume; cover letters and answers with limit fitting; resume diff screen; *mark applied* through the opportunity; extension field fill and resume download | Feature E acceptance; the injected-claims set is still rejected 100% by the existing validator, extended to letters | P18, P20 | M |
| **P22 Inbox and outcomes** | Label-scoped reading and thread linking; recruiter labels and their evaluation; Inbox screen with approval; invite detection to proposed tasks; Insights screen and export | Feature G and I acceptance; unrelated mail is never stored (fixture test) | P20 | L |
| **P23 Network and connector** | Relationship notes, strength and the referral request tracker on `connections`; Network screen; extension referral draft; connector opportunity tools and `propose_note` on the four existing scopes; decide on pgvector from the evaluation | Feature F acceptance; no LinkedIn host is ever contacted (existing test still passes) | MVP gate | S |
| **P24 Interview War Room** | Interviews; prep packs; study plan; mock sessions with rubrics and a daily cap; practice items | Feature H acceptance; every pack fact cites a source | P22, P19 | L |

### Gates

1. **After P18, the memory gate.** Parsing has produced zero invented facts on real resumes, and you have confirmed your own facts in production. Without this, every later output is built on sand.
2. **After P22, the MVP gate.** You have used the product on real applications for about four weeks, the metrics above are being recorded with counts, and you have decided what to cut or change. P23 and P24 start from that review, not from this plan.
3. **Before the first other user, the opening gate.** Legal advice on the DPDP Rules and on hosting in Sydney; the Google client verified or an alternative chosen; a privacy policy; the erase and export paths tested; a decision on the Chrome store listing.

### What can run alongside

Styling and component work in P17 can overlap with P18's backend. The evaluation sets (your resume, 30 labelled postings, 100 labelled emails) can be assembled by you at any time and should be, because P18, P19 and P22 each wait on one.

## Risks, costs and unknowns

**The largest risk is not technical: a job-switch tool earns trust only if no generated line is ever false, and the second is that a single user's data is too thin to prove the recommendations help.** The register below ranks what could hurt most and what limits it.

### Risk register

| Risk | Effect | Mitigation |
| --- | --- | --- |
| A resume line contains something the user never did | Lost trust; harm in an interview | Edit operations only; validator against confirmed facts; injected-claims test; diff view with evidence chips |
| Recommendations feel arbitrary or wrong | Users ignore the Move badge | Named rules, visible reasons, override with reason, agreement report before launch |
| Job coverage is partial | A user trusts the list as complete | Show sources per job and an explicit coverage note; extension capture for the rest |
| A stale-job label is wrong or reads as an accusation | Missed job; reputational risk | Observation wording, listed evidence, one-click "this was open" feedback |
| Reading mail goes beyond what the user expects | Privacy breach; Google policy issue | Label scope by default, opt-in for more, nothing stored outside scope, fixture test |
| Mail-derived data used to train models | Violates Google's limited-use rules (verify) | Dataset holds counts and user labels only |
| Prompt injection through a posting or recruiter email | Model told to reveal or do something | Structured output, no side-effect tools, validators, approval gate |
| Extension fill misbehaves on a real form | Wrong data typed into an application | Text and select fields only, user reviews, fixtures per ATS, a visible "I filled these fields" list |
| Free model tiers change or throttle | Features stall | Router chain, cool-downs, visible *waiting for capacity* state, per-feature daily caps |
| Sydney hosting and Indian users' data | Possible legal exposure | Legal advice before the opening gate |
| Scope creep from nine features | Nothing finishes | MVP cut, gates, one phase at a time with your review |
| One 2 GB host runs more work | Memory pressure, slow pages | Separate memory-limited render job, per-workspace queue caps, measure before adding load |

### Costs and where they come from

The model token figures are my estimates for planning, not measurements: parsing a resume about 5,000 tokens once; extracting and evaluating one posting about 3,000; tailoring one application 6,000 to 10,000 including a repair attempt; one ten-turn mock interview 30,000 or more. A user triaging 30 jobs a day is about 90,000 tokens a day for fit alone. Whether the free tiers cover that is **unknown** and depends on their current limits, which I did not check; the first evaluation run should log real usage per feature through the existing `llm_usage` table.

| Cost | Expectation | Control |
| --- | --- | --- |
| Models | The main variable cost; mock interviews and tailoring are the heaviest | Free-first router, caps per feature per day, extraction only when a job is opened or in the user's top results |
| Storage | Files of roughly 100 KB each (estimate); trivial for one user | Per-user size cap; delete path |
| Hosting | No new service in the MVP; PDF rendering is the memory risk | Measure in the P21 spike before choosing the renderer |
| Chrome Web Store | A one-time developer fee, if listed (*unverified*) | Load unpacked until needed |
| Google verification and legal advice | Time and possibly money before other users (*unverified*) | Only at the opening gate |

No paid service is added without your approval, as in the hard rules.

### Technical unknowns to resolve early

1. **PDF rendering memory** on the production box, and which renderer fits. A spike in P21 before any design depends on it.
2. **Resume parsing quality** on real, messy PDFs, including two-column layouts. Measured in P18 on real files.
3. **Whether label-scoped reading is enough** to catch recruiter mail that arrives outside any label. Measured in P22 with the user's own inbox.
4. **How well ATS feeds cover the user's target companies.** Measured in P19 by listing the user's top 30 companies and checking which publish feeds.
5. **Whether lexical matching of requirements to facts is good enough** without vectors. Decided in P23 from the evaluation.

## Open questions and sources

**Five decisions are yours; each has a default I will use if you say 'default'.**

### Decisions needed

- [ ] Does v4 replace agency outreach or sit beside it? *Default: beside it. New workspaces are job-switch by default; outreach, Sending, Signals and Operations move under Legacy and nothing is deleted.*
- [ ] Are you the only user until the MVP gate (about four weeks of real use)? *Default: yes.*
- [ ] How much of the inbox may the agent read? *Default: threads we sent plus a Gmail label you choose; whole inbox only if you opt in.*
- [ ] Does the Chrome extension get a public store listing? *Default: no, load unpacked.*
- [ ] Is Outlook needed in the first release? *Default: no, after P24 if asked.*

### Things only you can supply

- Your current resume, and permission to use it as the parsing test.
- About 30 job postings (links are enough) with the move you would choose for each.
- About 100 recruiter emails you have received, labelled by type, or the label scheme in your Gmail that picks them out.
- The 30 companies you most want to work for, so job coverage can be measured.

### What I did not verify

- I did not run the test suite or the live server; 'implemented' means the code and tests are present.
- I did not read every file. At `e7ec2d7` I read the models, the routers and services for candidate, resumes, job posts, referrals, contacts, signals and the extension, the fit module, the frontend structure (`App.jsx`, `JobHunt.jsx`, `ui.jsx`), the ADRs from 040 to 056, and the v3 plan and progress notes.
- I took S16.3 and S16.4 as the only open v3 slices from the plan and progress notes; confirm that against `PROGRESS.md` before P17.
- I did not check whether the Google client has left Testing mode after ADR-050.
- Google, Microsoft, LinkedIn, GitHub and Chrome Web Store policies and limits are from background knowledge or earlier research and are marked *unverified* where they matter.
- I did not open Naukri, Instahyre or Wellfound pages, and the only market comparison is one vendor-written article.
- Model token figures and effort sizes are my estimates.

### Sources opened (2 and 8 November 2026)

| Source | Used for |
| --- | --- |
| [Scoutify: best job search tools 2026](https://scoutify.com/blog/best-job-search-tools-2026) | What competing tools say they do and their stated limits. Vendor-written; prices as that article reports them |
| [Cutshort: sourcing talent with Claude](https://cutshort.io/how-to-source-talent-using-claude) | An employer-side MCP server and API for Indian tech talent |
| Repository `Harishchand83077/outreach-automation` at commit `e7ec2d7` (first audit at `915296d`) | The audit: models, routers, services, extension, frontend, `IMPLEMENTATION_PLAN_V3.md`, `PROGRESS.md` |
| `OUTREACH_PLATFORM_V3.md` (this project's earlier document) | Phases P10 to P16, feature ids F1 to F24, hard rules |
