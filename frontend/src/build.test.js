import { describe, expect, it } from "vitest";

import { buildMismatch, known } from "./build";

const UI = { commit: "abc1234", built_at: "2026-11-15T12:00:00Z" };

describe("buildMismatch", () => {
  it("says nothing when the builds agree", () => {
    expect(buildMismatch({ commit: "abc1234", built_at: UI.built_at }, UI)).toBe(null);
  });

  it("says nothing when the API does not know its own build", () => {
    // A development run bakes no commit. Without this a developer sees the banner on every
    // page load for ever, and then nobody reads it on the day it is true.
    expect(buildMismatch({ commit: "unknown", built_at: "" }, UI)).toBe(null);
    expect(buildMismatch({ commit: "", built_at: "" }, UI)).toBe(null);
    // `local` is the Compose default tag, so it means the same thing as unset.
    expect(buildMismatch({ commit: "local", built_at: "" }, UI)).toBe(null);
  });

  it("says nothing when this bundle does not know its own build", () => {
    const anonymous = { commit: "", built_at: "" };
    expect(buildMismatch({ commit: "def5678", built_at: UI.built_at }, anonymous)).toBe(null);
  });

  it("reports a mismatch with both builds named", () => {
    const out = buildMismatch({ commit: "def5678", built_at: "2026-11-10T09:00:00Z" }, UI);
    expect(out.api.commit).toBe("def5678");
    expect(out.ui.commit).toBe("abc1234");
  });

  it("calls the API older only when both times parse and it is earlier", () => {
    const older = buildMismatch({ commit: "def5678", built_at: "2026-11-10T09:00:00Z" }, UI);
    expect(older.apiOlder).toBe(true);

    const newer = buildMismatch({ commit: "def5678", built_at: "2026-11-20T09:00:00Z" }, UI);
    expect(newer.apiOlder).toBe(false);
  });

  it("refuses to call the API older when a time is missing or unreadable", () => {
    // Two hashes are different and never older. Asserting "the API is behind" on no evidence
    // sends somebody to redeploy something that was already current.
    expect(buildMismatch({ commit: "def5678", built_at: "" }, UI).apiOlder).toBe(false);
    expect(buildMismatch({ commit: "def5678", built_at: "whenever" }, UI).apiOlder).toBe(false);
    expect(
      buildMismatch({ commit: "def5678", built_at: UI.built_at }, { commit: "abc1234", built_at: "x" })
        .apiOlder,
    ).toBe(false);
  });

  it("treats a missing report as nothing to say rather than as a mismatch", () => {
    // /version is unreachable on an API too old to serve it, which is itself the condition this
    // banner is about -- but a failed fetch gives no commit to name, and a banner that cannot
    // say which builds differ is a banner nobody can act on.
    expect(buildMismatch(null, UI)).toBe(null);
    expect(buildMismatch(undefined, UI)).toBe(null);
  });
});

describe("buildMismatch with an unknown time", () => {
  it("does not pass an unparseable build time on as a timestamp", () => {
    // Seen on the live banner: "Server b6ffde5 (unknownZ)".
    const result = buildMismatch({ commit: "b6ffde5", built_at: "unknown" }, UI);
    expect(result.api.built_at).toBe("");
    expect(result.apiOlder).toBe(false);
  });
});

describe("known", () => {
  it("counts the placeholders as unknown", () => {
    expect(known("abc1234")).toBe(true);
    for (const value of ["", "unknown", "local", null, undefined]) {
      expect(known(value)).toBe(false);
    }
  });
});
