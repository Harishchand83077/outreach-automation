# Open questions

Each question states a **recommendation** and the **default I will proceed with** if unanswered, so
nothing blocks. Answered items move to `DECISIONS.md`. Never paste a secret here or in chat — I will
name the environment variable and where to set it.

## Needs your hand (I am not permitted to do these)

**Q-0 — Two fixes to `.claude/settings.json`.** I created the file, but editing it afterwards is
blocked as self-modification (correctly — it is my own permissions file). Two lines need your
attention:

1. **`"Bash(curl:*|*sh*)"` in `deny` is not valid pattern syntax**, so it matches nothing and the
   intended "never pipe a download into a shell" rule is **not actually enforced**. Replace that one
   entry with these four — nothing in this project needs an HTTP client at the shell, since every
   test fakes the network:
   ```json
   "Bash(curl:*)",
   "Bash(wget:*)",
   "Bash(iwr:*)",
   "Bash(Invoke-WebRequest:*)",
   ```
2. **`"Read(./.env.*)"` also blocks `.env.example`** — and this is not merely cosmetic, as I first
   assumed. It **blocked part of the Phase 0 safety patch**: `.env.example` still documents the
   deleted `DRY_RUN_EMAILS` flag, including the sentence that caused issue B1 ("Leave unset or 0
   to send real emails"), and it does not mention `SEND_MODE` at all. Startup now refuses to run
   if `DRY_RUN_EMAILS` is set, so a stale copy cannot silently mislead, but the template itself is
   wrong until it is edited.

   Recommended fix — narrow the rule so the committed template stays maintainable while every
   real secret file stays blocked. Replace `"Read(./.env.*)"` and `"Read(./**/.env.*)"` with:
   ```json
   "Read(./.env.local)",
   "Read(./.env.*.local)",
   "Read(./.env.production)",
   "Read(./.env.development)",
   "Read(./**/.env.local)",
   ```
   Keep `"Read(./.env)"` and `"Read(./**/.env)"` exactly as they are. Then tell me and I will
   finish the `.env.example` rewrite. Alternatively, paste the replacement block into
   `.env.example` yourself — the exact text is in the Phase 0 PR description.

Nothing else in the file needs changing — the `allow`/`ask`/`deny` lists otherwise match §00.3 plus
the two extra denies (`Read(./local/**)`, `Bash(gh repo edit --visibility public:*)`).

## Needed soon

**Q-A — Branch protection on `main`.** Is it enabled (require PR, require the CI checks once they
exist)? I will not push to `main` either way, but `gh pr merge --auto` behaves differently.
*Recommendation:* enable it once Phase 0 CI is green, requiring the `lint`, `types`, `tests` and
`frontend` checks. *Default:* assume off; merge with `--squash --delete-branch` after checks pass.

**Q-B — Mailbox provider.** Which SMTP/IMAP host will the business-domain mailbox use (Google
Workspace, Zoho, Fastmail, something else)? Needed at Phase 5 to set the right ports/quirks, and it
decides whether ADR-002's Sent-folder recovery works at all. *Recommendation:* Google Workspace or
Zoho on 587/993. *Default:* assume SMTP 587 STARTTLS + IMAP 993 TLS and verify at Phase 5.

**Q-C — `SEND_ALLOWLIST` addresses.** Which two of your own addresses should the rehearsal use?
Set them in your local `.env` as `SEND_ALLOWLIST=a@…,b@…`; I never read the file. Needed at the
Phase 5 gate. *Default:* nothing is sent at all (`SEND_MODE=disabled`) until this is set.

## Q1–Q7 from `ARCHITECTURE_TARGET.md` §24

Each is deferred to the phase that actually needs it, so the answer arrives with context rather than
in the abstract.

**Q1 — Does the 100/day cap cover *all* outbound, or 100 *new* leads/day with follow-ups on top?**
*Asked at:* Phase 5. *Recommendation:* all outbound. 100 new leads/day plus follow-ups is 150–200
sends/day, which is reckless for a young mailbox. *Default:* all outbound, i.e. about 50 new
leads/day plus 50 follow-ups at steady state.

**Q2 — Postal address in the footer?** *Asked at:* before go-live (compliance copy needs a lawyer's
eye regardless). *Recommendation:* include it — it costs nothing and materially helps both
credibility and CAN-SPAM posture. *Default:* omit, and flag it in `COMPLIANCE.md` as a gap.

**Q3 — How should a human be told a reply arrived?** *Asked at:* Phase 6. *Recommendation:* start
with the dashboard badge plus a once-daily email digest; add Telegram only if replies are being
missed. *Default:* dashboard badge + email digest.

**Q4 — Separate sending domain before go-live? ⚠ long lead time.** *Asked at:* start of Phase 5,
because DNS plus a warm-up takes weeks and it gates the go-live date. *Recommendation:* **yes** — a
look-alike domain, so cold-outreach reputation can never damage the domain you use with paying
clients. *Default:* assume a separate domain and write `GO_LIVE.md` around it; say so now if you
intend to use the primary domain, as it changes the DNS and warm-up plan.

**Q5 — Indian public holidays to skip?** *Asked at:* Phase 5. *Recommendation:* add the handful that
actually matter for B2B email (Diwali, Holi, Independence Day, Republic Day, Gandhi Jayanti) to
campaign `settings.holidays`. *Default:* empty list; weekends are already excluded.

**Q6 — Who reviews besides you, and with which role?** *Asked at:* Phase 7. *Recommendation:*
`member` for freelancers (review and reply only), `admin` for anyone who should change campaign
settings or the suppression list. *Default:* single `owner` (you), invite-only, no other members.

**Q7 — How long do we keep leads who never replied?** *Asked at:* Phase 9. *Recommendation:* 12
months, then purge the lead and its messages while keeping a hash of the address on the suppression
list, so a purged person is never re-contacted. *Default:* 12 months.

## Answered

| Date | Question | Answer |
|---|---|---|
| 2026-10-09 | Repo public with real addresses in `sample_leads.xlsx` — remediation? | Repo made private (verified); both addresses are the owner's own, so no third-party exposure. Delete the file, ship synthetic fixtures, skip the history rewrite. |
| 2026-10-09 | Approve the §00 permissions model and the five standing approvals? | Approved as written, plus two extra `deny` rules (see `.claude/settings.json`). |
| 2026-10-09 | P0–P10 breadth-first, or a vertical slice? | Vertical slice — ADR-003. |
| 2026-10-09 | Supabase Postgres as specified, or simplify? | Postgres in Compose, Supabase for Auth only — ADR-001. |
