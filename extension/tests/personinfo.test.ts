import { describe, expect, it } from "vitest";
import { readPerson } from "../src/personinfo";

const page = (html: string): Document => new DOMParser().parseFromString(html, "text/html");

describe("readPerson", () => {
  it("reads name, headline and company from the page title and drops the query", () => {
    const doc = page(
      `<html><head><title>Asha Rao - CTO at Acme Labs | LinkedIn</title></head><body></body></html>`,
    );
    expect(readPerson(doc, "https://www.linkedin.com/in/asha-rao?trk=x#top")).toEqual({
      url: "https://www.linkedin.com/in/asha-rao",
      full_name: "Asha Rao",
      headline: "CTO at Acme Labs",
      company_name: "Acme Labs",
    });
  });

  it("prefers the first heading and never reads body text", () => {
    const doc = page(`<html><head><title>Profile</title></head><body><h1>B Singh</h1>
      <p>secret@example.com call 555-0100</p></body></html>`);
    const person = readPerson(doc, "https://example.com/team/b");
    expect(person.full_name).toBe("B Singh");
    expect(JSON.stringify(person)).not.toContain("secret");
  });
});
