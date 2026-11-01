/**
 * What `npm run build` actually produced.
 *
 * Two properties no source test can see, both of which fail at the one moment nobody is
 * watching -- the first injection into a real page:
 *
 * 1. **`collect.js` must contain no module syntax.** `chrome.scripting.executeScript({ files })`
 *    injects a classic script, so one `export` or `import` statement in that bundle is a syntax
 *    error on a page the user is about to fill in. Rollup emits an `export` the moment the entry
 *    exports anything, which is why `form.ts` exists separately from `collect.ts`.
 * 2. **No shared chunk.** A chunk two entries both import would be fetched by a relative import
 *    inside the content script, which fails for the same reason.
 *
 * Skipped with a message when `dist/` is absent, so `npm test` on a fresh clone says what to run
 * rather than failing on something that was never built.
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const DIST = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const built = existsSync(DIST);

describe.skipIf(!built)("the built package", () => {
  it("has the three entries and the manifest", () => {
    const names = readdirSync(DIST);

    expect(names).toContain("manifest.json");
    expect(names).toContain("panel.html");
    expect(names).toContain("background.js");
    expect(names).toContain("collect.js");
  });

  it("emits no shared chunk", () => {
    const chunks = readdirSync(DIST).filter((name) => name.startsWith("chunk-"));

    expect(chunks).toEqual([]);
  });

  it("builds the content script as a classic script", () => {
    const script = readFileSync(join(DIST, "collect.js"), "utf8");

    expect(script).not.toMatch(/^\s*export\s/m);
    expect(script).not.toMatch(/^\s*import\s/m);
  });

  it("ships no map of the source into the package", () => {
    // Source maps would publish the whole TypeScript source inside the extension. Harmless
    // here, since the repository is the source, and 150 KB of nothing in a side panel.
    expect(readdirSync(DIST).filter((name) => name.endsWith(".map"))).toEqual([]);
  });
});

describe.skipIf(built)("the build", () => {
  it("has not run yet", () => {
    // Not a failure: a fresh clone has no `dist/`. CI runs the build before the tests.
    expect(built).toBe(false);
  });
});
