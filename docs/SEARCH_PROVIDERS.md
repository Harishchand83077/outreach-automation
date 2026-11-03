# Search providers — what is verified and what is assumed

F17, ADR-046. The same table `docs/CONTACT_PROVIDERS.md` carries, and for the same reason: three
of these endpoints could not be called without an account, and hard rule 5 forbids buying one.
Saying which shapes were tested and which were read off a page is the difference between a
document and a guess.

**None of the three has been called from this repository.** Brave requires a registered
subscription token, Serper a registered account, and Tavily a key the deployment holds but which
belongs to one account whose free tier is shared. So every shape below is each provider's
documented response, encoded as a fixture, and read in exactly one function
(`_read_tavily`, `_read_brave`, `_read_serper` in `app/services/research/search.py`). A wrong
assumption is therefore one function and one test, which is the whole reason the readers are
separate.

| Provider | Endpoint | Method | Key travels in | Results under | Snippet field | Confidence |
|---|---|---|---|---|---|---|
| `tavily` | `https://api.tavily.com/search` | POST | request body (`api_key`) | `results[]` | `content` | **Exercised** — this is the shape the pre-F17 code already read, against a real key |
| `brave` | `https://api.search.brave.com/res/v1/web/search` | GET | `X-Subscription-Token` header | `web.results[]` | `description` | Documented, **not called** |
| `serper` | `https://google.serper.dev/search` | POST | `X-API-KEY` header | `organic[]` | `snippet` | Documented, **not called** |

## What the shapes have in common, and where they differ

Every one of the three returns a list of objects with a title, a URL and a snippet, and the only
differences are where the list lives and what the three fields are called. That is why `_hits`
is one shared function taking three field names: the per-provider reader is three lines, and the
clipping, the dropping of a row with no URL and the length caps cannot drift between providers.

**The differences are exactly the kind that fail for one source only**, which is the ATS feeds'
lesson repeated — Lever returning a bare array where Greenhouse and Ashby wrap theirs in `jobs`
produced a parser that worked for two sources and read as a bad board slug for the third. Brave
nesting under `web.results` and Serper calling the URL `link` are the same trap, so each has its
own test asserting its own documented shape rather than one test over a shape they share.

## Failure, and why it is not an empty list

Before F17 a provider failure returned `[]`: search was a best-effort supplement, so an outage
degraded the research result rather than failing the lead, and with one provider that was the
whole truth.

With a chain, "nobody writes about this company" and "this provider is down" are opposite
answers:

- A **confirmed empty** stops nothing and **is cached** — the next lead at that company should not
  pay to be told the same silence.
- An **outage** moves to the next member and is **never cached** — storing it would stop the
  workspace searching for a week over one bad afternoon.

So `search()` raises:

| Status | Raised | What the chain does |
|---|---|---|
| 401, 403 | `SearchKeyRefused` | records `invalid_reason` on the credential, leaves this member out of future chains, tries the next |
| 429 | `SearchUnavailable` | tries the next; the key is untouched, because being out of credit is not being wrong |
| other non-200 | `SearchUnavailable` | tries the next |
| network error | `SearchUnavailable` | tries the next |
| 200 with unreadable JSON | `SearchUnavailable` | tries the next |
| 200 with a shape we cannot read | *no exception*, empty hits | treated as a provider that answered and found nothing |

The last row is the one honest compromise. A 200 whose body parses but whose fields we do not
recognise could mean a provider renamed a field, which is an outage in effect — but it is
indistinguishable from a genuinely empty result set, and guessing wrong in the other direction
would have a renamed field refuse every search for ever. We did reach them and they did reply, so
it is recorded as an answer.

## Budget

`DEFAULT_MONTHLY_BUDGET` is 1,000 credits per provider per workspace per calendar month,
overridable with `TAVILY_MONTHLY_CREDIT_BUDGET`. The number comes from Tavily's published free
plan; applied to the other two it is **a ceiling of ours, not a claim about their plans**, which
we cannot read. Its job is the same as `DAILY_LOOKUP_BUDGET`'s: stop a loop spending somebody's
month in an afternoon.

Per provider rather than pooled, because each member of the chain is a separate account with its
own allowance — a shared total would refuse a workspace's second key for credits its first key
spent.

## The deployment entry

Where `TAVILY_API_KEY` is set, the deployment's own Tavily account is the **last** member of every
workspace's chain, recorded in `search_usage` as `tavily:deployment` rather than `tavily`.

It is the only member that is not somebody's own key, and the only one with a caveat worth
printing on the screen: **its free tier is shared by every workspace that uses it**, so one large
import elsewhere can exhaust it, and there is no row a route could mark invalid when the key goes
wrong. Adding your own key puts it ahead of this one. ADR-023's argument applies — as long as one
identity lives in the environment, every question about who may use it is a rule somebody has to
maintain — and this entry survives only because removing it would stop the live deployment
searching at all.
