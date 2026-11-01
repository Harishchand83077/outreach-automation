# The Chrome side panel

Reads the questions on a job-application page and shows your own approved answer beside each
one. **It suggests. It does not fill anything in, and it does not submit anything.**

## Install it (load unpacked)

```bash
cd extension
npm ci
npm run build          # type-checks, then writes dist/
```

Then in Chrome: `chrome://extensions` → **Developer mode** on → **Load unpacked** → pick
`extension/dist`. Pin the toolbar icon; clicking it opens the side panel.

In the panel, paste **your workspace's API address** and an **extension token** (dashboard →
Settings → the extension panel). Chrome will ask once for permission to reach that one address.

## What it sends, and what it does not

| Sent to your workspace | Never sent, never stored, never read |
|---|---|
| The page URL | Anything you have typed into the page |
| The **labels** of the form's questions | The page's text, its HTML, or a screenshot |
| What you did with a suggestion: accepted, edited, rejected | Any other tab, your history, your cookies |

`PRIVACY.md` in the repository root is the authority on this and says the same thing.

## Why it is built the way it is

**No content script in the manifest.** `collect.js` is injected by the panel on a button press,
under `activeTab` — a permission Chrome grants for one tab because you clicked this extension's
own button. So the extension has no standing ability to read any page: not a policy, an absence.

**No host permissions in the package.** The API origin is requested when you save its address.
A packaged wildcard would be an install-time prompt covering the whole web.

**The mappers ship inside the package.** The Web Store forbids remotely hosted code, and it is
the better design anyway: a selector a server could change is a selector that can start reading
the wrong element on a page you are about to submit.

**A question with no approved answer comes back blank**, marked as needing you. No model is
reachable from this path at all. Guessing screening answers is what the tools this is not
copying do, and a guess dressed as an answer is worse than a gap.

**A page it cannot fully read says so.** Some boards render the form inside a closed shadow root
where a label is simply not reachable. The panel reports the scan as incomplete rather than
presenting a short list as a complete one.

**The token is in `chrome.storage.local`, never `sync`.** Sync would push a bearer credential
through your Google account to every browser you sign into.

## Working on it

```bash
npm run typecheck      # tsc --noEmit, strict
npm test               # vitest; run `npm run build` first for the build assertions
npm run build          # dist/, loadable unpacked
```

Four things are asserted over the source rather than trusted: no form submission, no field
writing, no file upload, and no reading of a field's contents. They live in
`tests/safety.test.ts` **and** in `tests/unit/test_extension_package.py` in the Python suite —
deliberately twice, because the Python one runs on every change to the repository while this
package's own suite is a job that can be skipped.

`tests/fixtures/README.md` says exactly what the four saved forms are and what that makes the
scanner tests prove. They are hand-written to each board's published markup conventions, not
captured from a live page, and the file says so rather than implying more.
