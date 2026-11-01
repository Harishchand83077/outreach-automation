/**
 * The promises this package makes, asserted over its own source (F8, ADR-039).
 *
 * The extension's whole claim is **"it only suggests"**: it does not fill a field, does not
 * submit a form, does not upload a file, and does not read what the user has typed. Those are
 * four things it must not *be able* to do, and a reviewer cannot establish that by reading a
 * privacy policy. So they are checked the way `test_send_guard.py` checks the send gate: by
 * walking the source and failing on any API that could do it.
 *
 * Comments and this file are excluded, because the prose explaining the rule names every one of
 * the forbidden APIs -- the same reason `test_dashboard_state_is_declared.py` strips comments
 * first. A guard a comment can satisfy is a guard.
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(here, "..", "src");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return name.endsWith(".ts") ? [path] : [];
  });
}

/** Line and block comments out, so the explanation of a rule cannot satisfy it. */
function code(path: string): string {
  return readFileSync(path, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/^\s*\/\/.*$/gm, " ");
}

/**
 * Each entry is an API that would break one of the four promises. The message says which promise
 * -- a failure naming only a regex sends whoever hits it looking for a style rule.
 */
const FORBIDDEN: ReadonlyArray<{ pattern: RegExp; promise: string }> = [
  {
    pattern: /\.submit\s*\(|requestSubmit\s*\(/,
    promise: "the extension never submits a form",
  },
  {
    pattern: /\.value\s*=(?!=)/,
    promise: "the extension never fills a field",
  },
  {
    pattern: /\bDataTransfer\b|\.files\s*=|new\s+File\s*\(|FormData/,
    promise: "the extension never uploads a file",
  },
  {
    pattern: /dispatchEvent\s*\(|\.click\s*\(/,
    promise: "the extension never acts on the page on the user's behalf",
  },
  {
    pattern: /\bcontent_scripts\b|chrome\.webNavigation|chrome\.history|chrome\.cookies/,
    promise: "the extension reads a page only on a button press, under activeTab",
  },
];

describe("the package cannot do what it promises not to", () => {
  const files = sources(SRC);

  it("has source to check", () => {
    expect(files.length).toBeGreaterThan(4);
  });

  for (const { pattern, promise } of FORBIDDEN) {
    it(`contains nothing that could break: ${promise}`, () => {
      const offenders = files
        .filter((path) => pattern.test(code(path)))
        // `panel.ts` assigns a textarea's value: that textarea is the panel's own, and writing a
        // suggestion into it is the whole feature. The rule is about the *page*, and the panel
        // has no handle on the page at all -- it never messages the content script, which is
        // itself asserted below.
        .filter((path) => !(pattern.source.includes("value") && path.endsWith("panel.ts")));

      expect(offenders.map((path) => path.replace(SRC, "src"))).toEqual([]);
    });
  }

  it("has no channel from the panel back into the page", () => {
    // The strongest form of "it cannot fill the form": even if the panel wanted to, there is
    // nothing listening. `collect.js` registers no message listener, and nothing sends one.
    const all = files.map(code).join("\n");

    expect(all).not.toMatch(/chrome\.tabs\.sendMessage|chrome\.runtime\.onMessage/);
  });

  it("reads a field's question and never its contents", () => {
    // `getAttribute("value")` would be the back door round the assignment rule above, and
    // `.checked` is the same question asked of a radio.
    const all = files.map(code).join("\n");

    expect(all).not.toMatch(/getAttribute\(\s*["']value["']\s*\)/);
    expect(all).not.toMatch(/\.checked\b/);
  });

  it("talks to one path on one host, and that host is the user's own API", () => {
    const all = files.map(code).join("\n");
    const fetches = all.match(/fetch\s*\(/g) ?? [];

    // One `fetch`, in `api.ts`, built by `endpoint()`. A second one anywhere is a second place a
    // URL is decided, which is how a credential ends up somewhere nobody chose.
    expect(fetches).toHaveLength(1);
    expect(code(join(SRC, "api.ts"))).toContain("/ext/v1");
  });
});

describe("the manifest asks for nothing it does not need", () => {
  const manifest = JSON.parse(
    readFileSync(resolve(here, "..", "public", "manifest.json"), "utf8"),
  ) as {
    permissions: string[];
    host_permissions: string[];
    optional_host_permissions: string[];
    content_scripts: unknown[];
    manifest_version: number;
  };

  it("is MV3", () => {
    expect(manifest.manifest_version).toBe(3);
  });

  it("declares no content script, so nothing runs until the button is pressed", () => {
    expect(manifest.content_scripts).toEqual([]);
    expect(manifest.permissions).toContain("activeTab");
  });

  it("ships with no host access at all", () => {
    // The user's API origin is requested at the moment they save its address, so the install
    // prompt covers nothing and the grant names one host. A packaged `https://*/*` would be a
    // permission over the entire web, accepted once for a reason nobody could evaluate.
    expect(manifest.host_permissions).toEqual([]);
    expect(manifest.optional_host_permissions).toEqual(["https://*/*"]);
  });

  it("asks for no permission that would let it watch browsing", () => {
    for (const forbidden of ["tabs", "webNavigation", "history", "cookies", "downloads", "<all_urls>"]) {
      expect(manifest.permissions).not.toContain(forbidden);
    }
  });
});
