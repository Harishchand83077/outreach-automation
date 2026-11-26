/**
 * Reading the one person whose page the user is looking at (ADR-084).
 *
 * Four facts and no more: the page address, a name, a headline and a company when the headline
 * names one. Only the page's title, its first heading and its social-card meta tag are read --
 * no body text, no contact details, no form field, and nothing is followed to another page.
 * Press a button on a page you opened yourself; this never navigates, searches or pages through.
 */

export interface PersonInfo {
  url: string;
  full_name: string;
  headline: string | null;
  company_name: string | null;
}

function clean(raw: string | null | undefined): string {
  return (raw ?? "").replace(/\s+/g, " ").trim();
}

/** "Asha Rao - CTO at Acme | LinkedIn" -> name and the rest, with the site suffix dropped. */
function splitTitle(title: string): { name: string; rest: string } {
  const withoutSite = title.replace(/\s*[|–—]\s*[^|–—]*$/, "");
  const [name, ...rest] = withoutSite.split(/\s+-\s+/);
  return { name: clean(name), rest: clean(rest.join(" - ")) };
}

export function readPerson(doc: Document, href: string): PersonInfo {
  const social = clean(doc.querySelector('meta[property="og:title"]')?.getAttribute("content"));
  const { name: fromTitle, rest } = splitTitle(social || clean(doc.title));
  const heading = clean(doc.querySelector("h1")?.textContent);
  const fullName = (heading && heading.length <= 80 ? heading : fromTitle).slice(0, 300);
  const headline = rest || null;
  const company = headline?.match(/\bat\s+([^|,·-]+)/i)?.[1];
  let url = href;
  try {
    const parsed = new URL(href);
    url = `${parsed.origin}${parsed.pathname}`;
  } catch {
    /* keep the address as given */
  }
  return {
    url,
    full_name: fullName,
    headline: headline ? headline.slice(0, 500) : null,
    company_name: company ? clean(company).slice(0, 300) : null,
  };
}
