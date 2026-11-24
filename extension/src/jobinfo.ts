/**
 * Reading one job posting's identity off the page the user is looking at (S19.4, ADR-070).
 *
 * Three facts and no more: the posting's URL, its title, and the employer's domain when the page
 * states one. No page text and no form value leaves the browser -- the server dedupes on the URL
 * and the person reads the rest on the posting itself.
 *
 * Pages that publish a `JobPosting` JSON-LD block are read from it, because it is the employer's
 * own statement of title and organisation; otherwise the page's headline is used.
 */

export interface JobInfo {
  url: string;
  title: string;
  company_domain: string | null;
}

function jsonLdPostings(doc: Document): Array<Record<string, unknown>> {
  const found: Array<Record<string, unknown>> = [];
  const visit = (node: unknown): void => {
    if (Array.isArray(node)) return node.forEach(visit);
    if (!node || typeof node !== "object") return;
    const record = node as Record<string, unknown>;
    const type = record["@type"];
    const types = Array.isArray(type) ? type : [type];
    if (types.includes("JobPosting")) found.push(record);
    if (record["@graph"]) visit(record["@graph"]);
  };
  for (const script of Array.from(doc.querySelectorAll('script[type="application/ld+json"]'))) {
    try {
      visit(JSON.parse(script.textContent ?? ""));
    } catch {
      /* malformed block: ignore it, the headline fallback still works */
    }
  }
  return found;
}

function domainOf(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  try {
    return new URL(raw).hostname.replace(/^www\./, "").toLowerCase() || null;
  } catch {
    return null;
  }
}

function clean(raw: string | null | undefined): string {
  return (raw ?? "").replace(/\s+/g, " ").trim();
}

export function readJob(doc: Document, href: string): JobInfo {
  const posting = jsonLdPostings(doc)[0];
  const organisation = posting?.["hiringOrganization"] as Record<string, unknown> | undefined;
  const fromLd = typeof posting?.["title"] === "string" ? clean(posting["title"] as string) : "";
  const headline =
    clean(doc.querySelector("h1")?.textContent) ||
    clean(doc.querySelector('meta[property="og:title"]')?.getAttribute("content")) ||
    clean(doc.title);
  const canonical = doc.querySelector('link[rel="canonical"]')?.getAttribute("href");
  let url = href;
  if (canonical) {
    try {
      url = new URL(canonical, href).toString();
    } catch {
      /* keep the address the user is on */
    }
  }
  return {
    url,
    title: (fromLd || headline).slice(0, 500),
    company_domain: domainOf(organisation?.["sameAs"] ?? organisation?.["url"]),
  };
}
