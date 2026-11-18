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
| body text | `content`, **only with `?content=true`** | `descriptionPlain` | `descriptionPlain` |

Two things worth knowing before building on this:

- **Greenhouse's list response carries no description unless it is asked for.** `?content=true`
  on the list returns it; the alternative is a second call per job
  (`/jobs/{id}?questions=false`). **`feed_url` passes `content=true`** -- for a year it did not,
  so every posting polled from a Greenhouse board was stored with no body and could never be
  scored. The field arrives as **HTML escaped inside a JSON string**, so `&lt;p&gt;` where the
  other two send plain text; `feeds._plain_from_escaped_html` unescapes, turns block tags into
  newlines and unescapes again. Body text stays optional in the model, because a response
  captured without the parameter still has to parse.
- **Lever's `createdAt` is epoch milliseconds**, where the other two are ISO 8601 strings.

## Sizes

A single Ashby board returned **2.8 MB** for 171 postings. Greenhouse's was 64 KB. So a feed read
needs a response size cap, not just a timeout: a board with thousands of postings would otherwise
be read into memory whole.

**With `content=true`, measured 2026-11-18:** a 13-posting Greenhouse board came to **166 KB**,
about 7.7 KB of escaped markup per posting. Extrapolated, `MAX_FEED_BYTES` (8 MB) holds roughly a
thousand postings with full descriptions, so the parameter did not need the cap raised. A board
larger than that fails the size check with a sentence rather than being half-read — and would
then need the per-job call instead.

## A wrong slug looks like a broken endpoint

`api.lever.co/v0/postings/netflix` returns `404 {"ok":false,"error":"Document not found"}` because
Netflix is not on Lever — not because the endpoint moved. Boards confirmed working while testing:
`leverdemo` and `ro` on Lever, `vercel` on Greenhouse, `ramp` on Ashby.
