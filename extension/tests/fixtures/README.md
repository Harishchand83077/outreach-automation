# Form fixtures

Four saved pages the scanner is tested against.

**What these are, stated plainly because it changes what the tests prove.** `docs/ATS_FEEDS.md`
records the three ATS *feed* shapes as **live-tested** — those endpoints were called and the
responses recorded. These four files are not that. They are hand-written, trimmed pages built to
each board's published markup conventions: Greenhouse's `#application_form` with `label[for]`,
Lever's `li.application-question` with `.application-label`, and Ashby's hashed
`_fieldEntry_*` class names around a plain `<label>`.

So these tests prove the scanner reads **those structures** correctly, and that the mappers
scope to the right element. They do not prove any live board's current markup matches them. The
three things that make that acceptable:

1. The `generic` mapper takes the page's largest form, so a board whose markup has moved on
   still yields its questions — the mappers are an optimisation over a fallback that works.
2. A field with no reachable label is **counted and reported** rather than guessed at, so a
   markup change degrades into "some fields could not be read" rather than into wrong answers.
3. Nothing the extension reads is acted on without a person reading it.

`shadow.html` is the fourth and is the one that is not about any board: a form rendered inside a
shadow root, which is where the capture-only fallback has to hold.
