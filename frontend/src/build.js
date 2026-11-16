/* Whether the running API is a different build from this screen (S17.3).
 *
 * **The problem this solves has cost this project a session four times.** Cloudflare rebuilds
 * the dashboard on every merge to `main`; the API is deployed by hand. So the two drift apart
 * routinely, and the symptom is a screen calling a route the running API does not serve --
 * which arrives as a bare 404. A 404 cannot say "this API is older than this screen", so the
 * investigation starts at DNS, CORS and connectivity every time.
 *
 * Pure, and in its own module, so the rules below are tested rather than inspected. The
 * dashboard's own suite renders nothing, which is exactly how a button once shipped throwing
 * `ReferenceError` -- a comparison living inside a component is a comparison nothing checks.
 */

/** The build this bundle was made from. Injected by `vite.config.js`. */
export const UI_BUILD = {
  commit: typeof __UI_COMMIT__ === "string" ? __UI_COMMIT__ : "",
  built_at: typeof __UI_BUILT_AT__ === "string" ? __UI_BUILT_AT__ : "",
};

const UNKNOWN = new Set(["", "unknown", "local", null, undefined]);

/** Whether a reported build identity is good enough to compare. */
export function known(commit) {
  return !UNKNOWN.has(commit);
}

/**
 * `null` when there is nothing worth saying, or `{ api, ui, apiOlder }`.
 *
 * **Silence is the default, and most of the design.** A banner that appears when it should not
 * is one people learn to ignore, and then it is not there on the day it matters -- the same
 * reasoning as alerting only after three consecutive worker failures. So:
 *
 * - Either side unknown means no claim. A development run bakes no commit and `local` is the
 *   Compose default, so a developer would otherwise see this on every page load for ever.
 * - Equal commits means no claim, obviously.
 *
 * `apiOlder` is reported only when **both** build times parse and the API's is the earlier one.
 * Two hashes are different and never older, and the times come from two different machines'
 * clocks -- so this is a statement about what those clocks said, which is why the banner shows
 * both rather than only the conclusion. Where it cannot be told, it says they differ and lets
 * the reader decide, because "the API is behind" asserted wrongly sends somebody to redeploy
 * something that was already current.
 */
export function buildMismatch(api, ui = UI_BUILD) {
  if (!api || !known(api.commit) || !known(ui.commit)) return null;
  if (api.commit === ui.commit) return null;

  const apiAt = Date.parse(api.built_at || "");
  const uiAt = Date.parse(ui.built_at || "");
  const comparable = Number.isFinite(apiAt) && Number.isFinite(uiAt);

  // A time is passed on only when it parses. The API answers the literal word `unknown` for an
  // image built without one, and the banner once printed it as a timestamp: "(unknownZ)".
  return {
    api: { commit: api.commit, built_at: Number.isFinite(apiAt) ? api.built_at : "" },
    ui: { commit: ui.commit, built_at: Number.isFinite(uiAt) ? ui.built_at : "" },
    apiOlder: comparable && apiAt < uiAt,
  };
}
