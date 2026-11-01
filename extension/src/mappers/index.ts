/**
 * Which board is this, and where is its form (F8, ADR-039)?
 *
 * **The mappers ship inside the package**, which is a Chrome Web Store requirement rather than a
 * preference: an extension whose logic arrives from a server at run time is a remotely hosted
 * code violation. It is also the better design here -- a selector the server could change is a
 * selector that can start reading the wrong element on a page the user is about to submit.
 *
 * A mapper does two small things: recognise its board, and name the element the form lives in.
 * It never reads a value and never decides an answer; the scan is `scan.ts` and the answers come
 * from the server's answer bank.
 *
 * **An unrecognised page falls back to `generic`, which scopes to the page's largest form.** The
 * alternative -- refusing -- would make the extension useless on the hundreds of boards nobody
 * has written a mapper for, and the cost of a bad reading is a suggestion the person rejects.
 */

export interface Mapper {
  /** Short, stable, and reported to the user: "read as a Lever form". */
  readonly name: string;
  /** Does this mapper claim the page? Hostname first, then a selector that must also be there. */
  matches(doc: Document, url: URL): boolean;
  /** The element holding the application questions, or null to scan the whole document. */
  root(doc: Document): Element | null;
}

function hostEndsWith(url: URL, ...suffixes: string[]): boolean {
  const host = url.hostname.toLowerCase();
  return suffixes.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
}

/**
 * Greenhouse. Two generations are live at once: the embedded `#application_form` on a company's
 * own careers page, and the hosted `job-boards.greenhouse.io` application, which wraps each
 * question in `div.field`. Both are matched, because a company that embeds the board is the
 * commonest case and it is served from the company's own hostname -- so the selector, not the
 * host, is what recognises it there.
 */
const greenhouse: Mapper = {
  name: "greenhouse",
  matches(doc, url) {
    if (hostEndsWith(url, "greenhouse.io")) return true;
    return doc.querySelector("#application_form, form[action*='greenhouse']") !== null;
  },
  root(doc) {
    return doc.querySelector("#application_form, #application-form, main form");
  },
};

/** Lever. Hosted at `jobs.lever.co`; each question is an `li.application-question`. */
const lever: Mapper = {
  name: "lever",
  matches(doc, url) {
    if (hostEndsWith(url, "lever.co")) return true;
    return doc.querySelector("form.application-form, li.application-question") !== null;
  },
  root(doc) {
    return doc.querySelector("form.application-form, .application-form, main form");
  },
};

/**
 * Ashby. Hosted at `jobs.ashbyhq.com`, a React application whose class names are hashed
 * (`_fieldEntry_hash`), so a prefix match is the only stable hook. The hashes change between
 * releases -- which is exactly why the fallback below is not optional.
 */
const ashby: Mapper = {
  name: "ashby",
  matches(doc, url) {
    if (hostEndsWith(url, "ashbyhq.com")) return true;
    return doc.querySelector("[class^='_fieldEntry'], [class*='_ashby']") !== null;
  },
  root(doc) {
    return doc.querySelector("form, [class^='_container']");
  },
};

/** Anything else: the page's own form, or the whole document if it has none. */
const generic: Mapper = {
  name: "generic",
  matches() {
    return true;
  },
  root(doc) {
    const forms = Array.from(doc.querySelectorAll("form"));
    if (forms.length === 0) return null;
    // The largest form by control count, because a page's header search box is also a form and
    // taking the first one is how a scan comes back with "Search" as its only question.
    return forms.reduce((biggest, form) =>
      form.querySelectorAll("input,textarea,select").length >
      biggest.querySelectorAll("input,textarea,select").length
        ? form
        : biggest,
    );
  },
};

/** Order matters: the first claim wins, and `generic` claims everything. */
export const MAPPERS: readonly Mapper[] = [greenhouse, lever, ashby, generic];

export function mapperFor(doc: Document, href: string): Mapper {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return generic;
  }
  for (const mapper of MAPPERS) {
    if (mapper.matches(doc, url)) return mapper;
  }
  return generic;
}
