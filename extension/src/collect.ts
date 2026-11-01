/**
 * The content script, injected on a button press and never before (F8, ADR-039).
 *
 * There is **no `content_scripts` block in the manifest**, so this file does not run on any page
 * until the side panel asks for it, under `activeTab` -- a permission Chrome grants for one tab,
 * for as long as that tab stays on that page, and only because the user clicked the extension's
 * own button. The extension therefore has no standing ability to read anything, which is a
 * stronger statement than a privacy policy: there is no code path that could.
 *
 * It reads the questions and the URL. It does not read what the user typed, it does not write to
 * the page, and it exports nothing -- see `form.ts` for why that matters.
 */

import { collectForm } from "./form";

// `executeScript` resolves to the value of the last statement evaluated, which is how the panel
// receives this without a message channel: one call, one reply, nothing left listening after.
collectForm(document, location.href);
