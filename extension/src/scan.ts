/**
 * Reading a form's *questions* off a page, and nothing else (F8, ADR-039).
 *
 * This is the only module in the package that touches a page's DOM, and it is a pure function of
 * a `Document` so it can be tested against saved copies of real application forms without a
 * browser.
 *
 * **Three rules, and they are what the feature promises rather than implementation details:**
 *
 * 1. **A field's value is never read.** Not `input.value`, not `textarea.value`, not a checked
 *    state. What the user has typed on an application page is theirs; the server needs the
 *    question to match an answer, and the answer is ours already.
 * 2. **Nothing is written.** No assignment to a value, no `click()`, no `submit()`, no
 *    `dispatchEvent`. The person fills their own form.
 * 3. **A page we cannot read is reported as unread, never guessed at.** Some boards render the
 *    form inside a closed shadow root, where a label is simply not reachable; a wrong label
 *    produces a confidently wrong suggestion, which is worse than none.
 */

/** A question found on the page. `key` is what the server matches on. */
export interface Field {
  key: string;
  /** Which mapper claimed it, for the panel to show and for a bug report to name. */
  via: string;
}

export interface ScanResult {
  url: string;
  fields: Field[];
  /**
   * True when the page held form controls we could not put a question to -- a closed shadow
   * root, or an input with no label of any kind. The panel says so, because a partial scan with
   * no warning reads as a complete one.
   */
  partial: boolean;
  /** How many controls were seen in total, labelled or not. Shown beside `partial`. */
  controls: number;
}

/** Controls that carry a question. A button or a hidden field is not one. */
const CONTROL_SELECTOR = [
  "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset])",
  "textarea",
  "select",
].join(",");

/** Longer than this is prose somebody pasted into a legend, not a question. */
const MAX_KEY_CHARS = 300;
const MAX_FIELDS = 60;

export function normalise(text: string | null | undefined): string {
  return (text ?? "").replace(/\s+/g, " ").trim().slice(0, MAX_KEY_CHARS);
}

/**
 * The question for one control, from the page's own structure.
 *
 * Tried in the order that is most likely to be the *visible* question, because that is what the
 * user is reading and therefore what their answer bank was written against. An `aria-label` or a
 * `placeholder` comes last: both are often a format hint ("DD/MM/YYYY") rather than a question,
 * and `name` is never used at all -- `job_application[answers_attributes][3][text_value]` is not
 * a question anybody wrote an answer to.
 */
export function questionFor(control: Element): string {
  const doc = control.ownerDocument;
  const id = control.getAttribute("id");
  if (id) {
    // Not `CSS.escape`: an id containing a quote or a backslash would otherwise build a
    // selector that throws, and a scan must not fail over one odd field on the page.
    const escaped = id.replace(/["\\]/g, "\\$&");
    const explicit = doc.querySelector(`label[for="${escaped}"]`);
    const text = normalise(explicit?.textContent);
    if (text) return text;
  }

  // A radio or a checkbox is normally wrapped in a label holding *one option*, and the question
  // is the group's legend -- so for those two the legend is tried first. Taking the wrapping
  // label would ask the answer bank about "A referral" instead of "How did you hear about this
  // role?", once per option.
  const type = (control.getAttribute("type") ?? "").toLowerCase();
  const grouped = type === "radio" || type === "checkbox";
  if (grouped) {
    const legend = normalise(control.closest("fieldset")?.querySelector("legend")?.textContent);
    if (legend) return legend;
  }

  const wrapping = control.closest("label");
  if (wrapping) {
    const text = normalise(wrapping.textContent);
    if (text) return text;
  }

  const described = control.getAttribute("aria-labelledby");
  if (described) {
    const parts = described
      .split(/\s+/)
      .map((ref) => normalise(doc.getElementById(ref)?.textContent))
      .filter(Boolean);
    if (parts.length) return normalise(parts.join(" "));
  }

  const group = control.closest("fieldset");
  const legend = normalise(group?.querySelector("legend")?.textContent);
  if (legend) return legend;

  return normalise(control.getAttribute("aria-label") ?? control.getAttribute("placeholder"));
}

/**
 * Every question on the page, deduplicated, with whether anything was unreadable.
 *
 * `via` is supplied by the caller (the mapper that recognised the board), so the panel can say
 * *"read as a Greenhouse form"* and a wrong reading is attributable to one mapper rather than to
 * "the extension".
 */
export function scanDocument(
  doc: Document,
  options: { url: string; via: string; root?: ParentNode | null },
): ScanResult {
  // Scoped to the form the mapper recognised, so a board's own site search and newsletter box
  // are not read as application questions.
  const scope: ParentNode = options.root ?? doc;
  const controls = Array.from(scope.querySelectorAll(CONTROL_SELECTOR));
  const seen = new Set<string>();
  const fields: Field[] = [];
  let unreadable = 0;

  for (const control of controls) {
    const key = questionFor(control);
    if (!key) {
      unreadable += 1;
      continue;
    }
    if (seen.has(key)) continue;
    seen.add(key);
    if (fields.length < MAX_FIELDS) fields.push({ key, via: options.via });
  }

  // **A board built out of web components.** A control inside a shadow root is not in the
  // document's `querySelectorAll` at all, so the loop above sees a form with no questions in it
  // -- which is indistinguishable from a page that genuinely has none, and is how a scan comes
  // back empty with nothing admitting why.
  //
  // An **open** root can be walked, and is. A **closed** one cannot: `host.shadowRoot` is null
  // and there is no way round that by design. So a custom element with no open root is counted
  // as a field we could not read, which over-reports -- not every `<x-y>` is a field -- and
  // over-reporting leaves the panel saying the list may be incomplete, which is the harmless
  // direction. Claiming a complete reading of a page we cannot see into is the other one.
  const custom = Array.from(scope.querySelectorAll("*")).filter((node) =>
    node.tagName.includes("-"),
  );
  for (const host of custom) {
    const root = host.shadowRoot;
    if (!root) {
      unreadable += 1;
      continue;
    }
    for (const control of Array.from(root.querySelectorAll(CONTROL_SELECTOR))) {
      const key = normalise(
        control.getAttribute("aria-label") ?? control.getAttribute("placeholder"),
      );
      if (!key) {
        unreadable += 1;
        continue;
      }
      if (seen.has(key)) continue;
      seen.add(key);
      if (fields.length < MAX_FIELDS) fields.push({ key, via: `${options.via}+shadow` });
    }
  }

  const total = controls.length + custom.length;
  return {
    url: options.url,
    fields,
    partial: unreadable > 0 || fields.length >= MAX_FIELDS,
    controls: total,
  };
}
