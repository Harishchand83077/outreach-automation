# Should this app use RAG or agents?

Written 2026-11-27 (ADR-086). The short answer: **not the usual way.** The system already has a
design rule that most RAG and agent stacks lack, and the right additions respect it.

## The rule this codebase already follows

> A model proposes. Deterministic code verifies. A person approves.

Evidence in the code: the drafter's claim check (every assertion resolves to a persona proof point
or a cited research hook), research hooks verified against fetched text by code, fit scores that
are rules over a model's extraction, reviewer-edit lessons that reach a prompt only after a person
approves them, and an MCP catalogue with no tool that can send or approve. Anything we add that
lets a model act on its own, or puts an unverified model output in front of a recipient, breaks
hard rules 6 and 7.

## RAG: where would retrieval actually help?

RAG pays when (a) the corpus is too big to put in a prompt, (b) the question is semantic, and
(c) a wrong retrieval is cheap. Checked against each place text is used:

| Place | Corpus size | Verdict |
|---|---|---|
| Drafting: which proof points to use | under 10 per persona | **No.** Put them all in the prompt. Retrieval only adds a way to miss one. |
| Drafting: company evidence | a few cited hooks per lead | **No.** Already small and already verified against source text. |
| Answer bank matching (extension) | tens of entries | **No, deliberately.** Matching is word overlap on purpose: a wrong answer on a job application is worse than a blank, so a miss shows "needs you". Embeddings would raise recall and also raise confident wrong answers. Users add a second pattern instead. |
| Fit score: requirement to fact | tens of facts | **Not now.** "Postgres" vs "PostgreSQL" misses are real but the score must stay reproducible and auditable. Revisit as "suggested match, confirm" only if measured misses justify it. |
| Finding things you already know (leads, notes, replies, research, postings) | thousands of rows | **Yes, but keyword search is enough.** Postgres full-text search is exact, free, explainable and needs no extension. |

**Vector database and embeddings: not justified.** The production image is plain `postgres:17`
(no pgvector, so adding it is a deploy-config change); the free-first LLM chain has no guaranteed
embedding endpoint; sending lead names and replies to an embedding provider is exactly what the
`allow_pii=false` routing exists to prevent; and at this corpus size the benefit is negligible.
Revisit when a workspace passes roughly 5,000 documents *and* a measured query set shows keyword
search missing things users wanted.

## Agents: where would autonomy help?

An agent is a model choosing its own next tool call in a loop. Its costs are non-determinism,
spend you cannot cap in advance, weaker behaviour on free-tier models, and an injection surface:
web pages and lead replies are untrusted text (rule 7), and an agent that reads them while holding
tools is the exact setup that attack needs.

| Candidate | Verdict |
|---|---|
| In-app research agent that browses and decides what to fetch | **No.** Research is a fixed pipeline (cache, fetch, extract, synthesise, verify, search once) and a fixed pipeline is cheaper, testable and safe. An agent would add cost and variance to a step that already works. |
| In-app agent that drafts or replies by itself | **No.** Violates `review_all` and rule 7. |
| **The user's own Claude as the agent, over MCP** | **Yes, and it already exists.** It runs under the user's own supervision, with a catalogue that cannot send or approve, and writes only proposals into the review queue. The leverage is giving it better tools, not building a second agent inside the app. |

## What to build, in order

1. **Workspace search** (built, ADR-086): Postgres full-text over leads, notes, replies, research,
   job postings and saved people. One service, two doors: the dashboard's Cmd/Ctrl+K palette and an
   MCP tool `search_workspace`, so the connected Claude can retrieve what is already known before
   it researches or drafts. This is the honest "RAG" here: retrieval for an external generator,
   with Claude writing and the human approving. Replies, postings and research crossing MCP are
   wrapped as `<untrusted>`.
2. **Next, measured, not assumed:** record which searches return nothing, and which answer-bank
   labels fall to "needs you", for two weeks. That is the data that decides whether fuzzy
   matching, a GIN index, or embeddings are ever worth their cost.
3. **Later, only if the data asks:** a "suggested match, confirm" step for fit requirements;
   an index on the search documents.

## Explicitly not building

An in-app autonomous agent, a vector store, auto-send of any kind, LinkedIn automation (ADR-084).
