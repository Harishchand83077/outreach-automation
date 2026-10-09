# Progress

Updated 2026-10-09. Read this with `CLAUDE.md` at the start of every session.

## Handoff note (rewrite at the end of every session)

**Where we are.** Plan approved. §00 bootstrap and §0 repo analysis are **complete**: every tracked
file read, all 43 original issue IDs verified (all confirmed — none "Partly", none "Not
reproducible"), 8 new issues added, baseline measured and tagged `baseline-pre-rewrite`. The §0
deliverables (`REPO_ANALYSIS.md`, the verdict-annotated `ISSUES.md`, `CLAUDE.md`, this file,
`DECISIONS.md`, `QUESTIONS.md`, `SETUP.md`) are written on `main` but **not yet committed**.

**Decisions already taken** (see `DECISIONS.md`): Postgres in Compose with Supabase for Auth only
(ADR-001); no `BCC_SELF` dependency for send recovery (ADR-002); vertical-slice sequencing rather
than the spec's P0→P10 breadth-first order (ADR-003); `org_id` in the first migration but the
members/roles UI deferred (ADR-004).

**Next up.** Commit the docs, then the Phase 0 safety patch (`fix/P0-safety-patch`) — the single
most important change in the project, because it is what stops the prototype from being able to
cause harm. Then CI.

**Live environment notes.** Python 3.12.1 venv already exists at `.venv` with the prototype's deps
installed (left as found; 3.12.12 is also installed via uv if a rebuild is wanted). `gh` is
authenticated as `Harishchand83077`. The repo is private. Branch protection on `main` is **not yet
confirmed** — see `QUESTIONS.md` Q-A.

## Phase status

| Phase | Scope | Status |
|---|---|---|
| §00 | Bootstrap, tooling, permissions | **Done** |
| §0 | Repo analysis, issue verification, baseline | **Done** |
| 0 | Safety patch + CI + docs | In progress — docs written, patch next |
| 1 | Data layer, state machine, job queue, outbox | Not started |
| 2 | Import, persona, campaigns | Not started |
| 3 | LLM router, research engine | Not started |
| 4 | Drafting, grounding, review queue | Not started |
| 5 | Send engine — **first `allowlist` send gate** | Not started |
| 6 | Reply engine, follow-ups | Not started |
| 7 | Auth, multi-tenancy | Not started |
| 8 | UI completion | Not started |
| 9 | Ops: Docker, deploy, logs, alerts, backups | Not started |
| 10 | Rehearsal, go-live gate | Not started |

## Issue register

51 tracked issues. Every one must end `Done`, `Superseded-by-rewrite` (with a test proving the
behaviour) or `Won't fix` (with a reason). Full text and evidence in `ISSUES.md`.

| Status | Count | IDs |
|---|---|---|
| Verified, not yet fixed | 51 | all |
| Done | 0 | — |

**Phase 0 targets** (the safety patch, in one PR labelled `needs-human`):
B1, B2, B3, B4, B6, C2, C8, NEW-1, NEW-2 — plus `.gitignore` for `uploads/`/`local/` (part of F3).

**Phase 0 CI targets:** E6, and the F1–F3 documentation rewrite.

**Deliberately deferred past Phase 0** (they need the rewritten foundation, not a patch):
B5, B7, B8, B9, B10, B11, B12, B13, B14, B15, B16, B17, B18, C1, C3, C4, C5, C6, C7, all of A, all
of D, all of E except E6, and NEW-3 to NEW-8. Rationale: patching the legacy modules for these
would be throwaway work, and `ISSUES.md` records the evidence so nothing is lost.

## PR log

| PR | Branch | Issues | Status |
|---|---|---|---|
| — | — | — | none opened yet |
