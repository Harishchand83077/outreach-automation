import { describe, expect, it } from "vitest";
import { readJob } from "../src/jobinfo";

function page(html: string): Document {
  return new DOMParser().parseFromString(html, "text/html");
}

describe("readJob", () => {
  it("prefers the employer's JobPosting JSON-LD", () => {
    const doc = page(`<html><head><title>Careers</title>
      <script type="application/ld+json">{"@graph":[{"@type":"JobPosting","title":" Backend  Engineer ",
      "hiringOrganization":{"sameAs":"https://www.acme.example.com/about"}}]}</script></head>
      <body><h1>Join us</h1></body></html>`);
    expect(readJob(doc, "https://jobs.example.com/1")).toMatchObject({
      url: "https://jobs.example.com/1",
      title: "Backend Engineer",
      company_domain: "acme.example.com",
    });
  });

  it("falls back to the headline and survives malformed JSON-LD", () => {
    const doc = page(`<html><head><script type="application/ld+json">{oops</script>
      <link rel="canonical" href="/jobs/9"></head><body><h1>Platform Engineer</h1></body></html>`);
    expect(readJob(doc, "https://jobs.example.com/jobs/9?utm=x")).toMatchObject({
      url: "https://jobs.example.com/jobs/9",
      title: "Platform Engineer",
      company_domain: null,
    });
  });

  it("reads the posting text but never a form field's contents", () => {
    const doc = page(`<html><body><main><h1>Dev</h1><p>Build   APIs.</p>
      <input value="secret-typed-by-user"><textarea>draft cover letter</textarea></main></body></html>`);
    const { text } = readJob(doc, "https://jobs.example.com/3");
    expect(text).toBe("Dev Build APIs.");
    expect(text).not.toContain("secret");
  });
});
