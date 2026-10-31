# Public ATS job feeds, as they actually respond

Tested 2026-10-31 against live boards, because the v3 doc's endpoints came from one third-party
article and a parser written against an assumed shape is a parser that breaks on first contact.

| Source | URL | HTTP | Shape |
|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` | 200 | `{"jobs": [...], "meta": {...}}` |
| Lever | `api.lever.co/v0/postings/{site}` | 200 | **a bare JSON array**, not wrapped |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{name}` | 200 | `{"jobs": [...], "apiVersion": "..."}` |

**Lever is the odd one out**: it returns a top-level list where the other two wrap theirs in
`jobs`. A parser that assumed one shape for all three would have failed on exactly one source,
which is the kind of thing that looks like a bad slug rather than a bad assumption.

## Field names, per source

| Field we need | Greenhouse | Lever | Ashby |
|---|---|---|---|
| id | `id` | `id` | `id` |
| title | `title` | `text` | `title` |
| apply url | `absolute_url` | `hostedUrl` (or `applyUrl`) | `jobUrl` (or `applyUrl`) |
| location | `location.name` | `categories.location` | `location` |
| posted at | `updated_at`, `first_published` | `createdAt` (epoch ms) | `publishedAt` |
| body text | *absent from the list response* | `descriptionPlain` | `descriptionPlain` |

Two things worth knowing before building on this:

- **Greenhouse's list response carries no description.** The body needs a second call per job
  (`/jobs/{id}?questions=false`) or `?content=true` on the list. So body text is optional in our
  model, and a Greenhouse post starts without one.
- **Lever's `createdAt` is epoch milliseconds**, where the other two are ISO 8601 strings.

## Sizes

A single Ashby board returned **2.8 MB** for 171 postings. Greenhouse's was 64 KB. So a feed read
needs a response size cap, not just a timeout: a board with thousands of postings would otherwise
be read into memory whole.

## A wrong slug looks like a broken endpoint

`api.lever.co/v0/postings/netflix` returns `404 {"ok":false,"error":"Document not found"}` because
Netflix is not on Lever — not because the endpoint moved. Boards confirmed working while testing:
`leverdemo` and `ro` on Lever, `vercel` on Greenhouse, `ramp` on Ashby.
