/**
 * Reading one page's application questions: the mapper picks the form, `scan.ts` reads it.
 *
 * Separate from `collect.ts` -- the injected entry point -- for a mechanical reason worth
 * writing down. `chrome.scripting.executeScript({ files })` injects a **classic** script, so a
 * bundle containing an `export` statement is a syntax error at the moment of injection. Keeping
 * every export here and leaving `collect.ts` with none means the built content script has no
 * module syntax in it at all, and `tests/build.test.ts` asserts that against the real build.
 */

import { mapperFor } from "./mappers/index";
import { scanDocument, type ScanResult } from "./scan";

export interface Collected extends ScanResult {
  via: string;
}

export function collectForm(doc: Document, href: string): Collected {
  const mapper = mapperFor(doc, href);
  const result = scanDocument(doc, { url: href, via: mapper.name, root: mapper.root(doc) });
  return { ...result, via: mapper.name };
}
