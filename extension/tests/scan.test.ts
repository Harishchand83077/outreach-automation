/**
 * The scanner against saved application forms (F8, slice S13.4).
 *
 * `tests/fixtures/README.md` says exactly what these files are and what that makes these tests
 * prove: the structures are hand-written to each board's published conventions, not captured
 * from a live page. So the assertions here are about the scanner's rules -- scope to the form,
 * take the visible question, never read a value, report what could not be read -- rather than
 * about any board's current markup.
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeAll, describe, expect, it } from "vitest";

import { collectForm } from "../src/form";
import { mapperFor } from "../src/mappers/index";
import { questionFor } from "../src/scan";

const here = dirname(fileURLToPath(import.meta.url));

function load(name: string, url: string): { doc: Document; url: string } {
  const html = readFileSync(resolve(here, "fixtures", name), "utf8");
  const doc = new DOMParser().parseFromString(html, "text/html");
  return { doc, url };
}

describe("a Greenhouse application", () => {
  const { doc, url } = load("greenhouse.html", "https://job-boards.greenhouse.io/acme/jobs/1");

  it("is recognised by its form rather than only by its host", () => {
    // A company embedding the board serves it from their own hostname, which is the common case.
    const embedded = mapperFor(doc, "https://acme.example.com/careers/1");
    expect(embedded.name).toBe("greenhouse");
  });

  it("reads the questions and not the page's own search box", () => {
    const result = collectForm(doc, url);

    expect(result.via).toBe("greenhouse");
    expect(result.fields.map((field) => field.key)).toEqual([
      "First Name *",
      "Last Name *",
      "Email *",
      "Why do you want to work at Acme?",
      "Are you authorised to work in India?",
      "Notice period (in days)",
    ]);
    expect(result.fields.map((field) => field.key)).not.toContain("Search jobs");
  });

  it("reports the unlabelled resume control rather than inventing a question for it", () => {
    const result = collectForm(doc, url);

    expect(result.partial).toBe(true);
  });

  it("never reads what the applicant typed", () => {
    // The fixture has values in three fields on purpose: a name, a surname and an email, which
    // are exactly the personal data this feature must not collect.
    const result = collectForm(doc, url);
    const serialised = JSON.stringify(result);

    for (const typed of ["Priya", "Nair", "priya@example.com", "I have admired the work"]) {
      expect(serialised).not.toContain(typed);
    }
  });
});

describe("a Lever application", () => {
  const { doc, url } = load("lever.html", "https://jobs.lever.co/globex/abc/apply");

  it("takes the label's text when the control is wrapped in it", () => {
    const result = collectForm(doc, url);

    expect(result.via).toBe("lever");
    expect(result.fields.map((field) => field.key)).toContain("Full name");
    expect(result.fields.map((field) => field.key)).toContain("Additional information");
  });

  it("uses a fieldset's legend for a radio group, once rather than per option", () => {
    const keys = collectForm(doc, url).fields.map((field) => field.key);

    expect(keys.filter((key) => key.startsWith("How did you hear"))).toHaveLength(1);
  });

  it("accepts an aria-label when there is no label element", () => {
    const keys = collectForm(doc, url).fields.map((field) => field.key);

    expect(keys).toContain("Resume/CV");
  });
});

describe("an Ashby application", () => {
  const { doc, url } = load("ashby.html", "https://jobs.ashbyhq.com/initech/abc");

  it("is recognised by its hashed class names when the host is somebody else's", () => {
    expect(mapperFor(doc, "https://initech.example.com/careers").name).toBe("ashby");
  });

  it("follows aria-labelledby to a question that is not a label element", () => {
    const keys = collectForm(doc, url).fields.map((field) => field.key);

    expect(keys).toContain("Which city are you based in?");
  });

  it("reports the custom combobox as unread", () => {
    const result = collectForm(doc, url);

    expect(result.partial).toBe(true);
    expect(result.fields).toHaveLength(4);
  });
});

describe("a form rendered inside shadow roots", () => {
  let doc: Document;

  beforeAll(() => {
    const loaded = load("shadow.html", "https://careers.hooli.test/apply");
    doc = loaded.doc;
    // The fixture is static HTML, so the roots are attached here -- which is what the board's
    // own script does on load, and is the state the content script would find.
    for (const host of Array.from(doc.querySelectorAll("hooli-field"))) {
      const root = (host as HTMLElement).attachShadow({ mode: "open" });
      const input = doc.createElement("input");
      input.setAttribute("aria-label", host.getAttribute("data-q") ?? "");
      root.append(input);
    }
  });

  it("reaches an open shadow root and marks where it came from", () => {
    const result = collectForm(doc, "https://careers.hooli.test/apply");

    expect(result.fields.map((field) => field.key)).toEqual([
      "Full name",
      "Email",
      "Tell us about a project you are proud of",
    ]);
    expect(result.fields.every((field) => field.via.endsWith("+shadow"))).toBe(true);
  });

  it("is capture-only when a root is closed, rather than reported as an empty form", () => {
    // A closed root is not reachable from the page at all -- `host.shadowRoot` is null -- so the
    // honest answer is "this page has fields we could not read", which is what `partial` says.
    const closed = load("shadow.html", "https://careers.hooli.test/apply").doc;
    for (const host of Array.from(closed.querySelectorAll("hooli-field"))) {
      (host as HTMLElement).attachShadow({ mode: "closed" }).append(closed.createElement("input"));
    }

    const result = collectForm(closed, "https://careers.hooli.test/apply");

    expect(result.fields).toHaveLength(0);
    expect(result.partial).toBe(true);
    expect(result.controls).toBeGreaterThan(0);
  });
});

describe("the question for one control", () => {
  it("is empty rather than a guess when the page associates nothing", () => {
    const doc = new DOMParser().parseFromString(
      "<form><div>Years of experience</div><input type='text'></form>",
      "text/html",
    );
    const input = doc.querySelector("input");

    // Nearby text is *not* used. A div above an input is a question often enough to be tempting
    // and wrong often enough to produce a confidently misapplied answer.
    expect(questionFor(input as Element)).toBe("");
  });

  it("never falls back to the control's name attribute", () => {
    const doc = new DOMParser().parseFromString(
      "<form><input name='job_application[answers_attributes][3][text_value]'></form>",
      "text/html",
    );

    expect(questionFor(doc.querySelector("input") as Element)).toBe("");
  });
});
