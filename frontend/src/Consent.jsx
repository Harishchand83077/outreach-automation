// The consent screen (F14, ADR-058).
//
// **The page a person is redirected to by an application asking for access.** It is deliberately
// not inside the dashboard's shell: there is no rail, no campaign picker and nothing to click
// except Allow and Cancel, because every other thing on the page is a thing that could be
// mistaken for the decision.
//
// **Everything it says comes from our own rows, never from the query string.** The client's name
// is read back from the API by `client_id`; the scopes are described by the API in the same words
// the tick-boxes in Settings use. A consent screen that renders a name the caller supplied is a
// phishing page with our domain in the address bar.
//
// The query parameters are validated by the API *before* this draws, so a request that could
// never succeed is refused here rather than after somebody presses Allow.

import { useEffect, useState } from "react";

import { getJson, postJson } from "./api";
import { Mark, PRODUCT } from "./Brand";
import { Icons } from "./ui";

export default function Consent({ onError }) {
  const params = new URLSearchParams(window.location.search);
  const asked = {
    client_id: params.get("client_id") || "",
    redirect_uri: params.get("redirect_uri") || "",
    scope: params.get("scope") || "",
    resource: params.get("resource") || "",
    state: params.get("state") || "",
    code_challenge: params.get("code_challenge") || "",
    code_challenge_method: params.get("code_challenge_method") || "S256",
  };

  const [detail, setDetail] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const query = new URLSearchParams({
      client_id: asked.client_id,
      scope: asked.scope,
      redirect_uri: asked.redirect_uri,
      resource: asked.resource,
    });
    getJson(`/oauth/consent?${query}`).then(setDetail).catch(onError);
    // Once, on the parameters the browser arrived with. They cannot change without a new
    // navigation, which remounts this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const allow = () => {
    setBusy(true);
    postJson("/oauth/authorize", { json: asked })
      .then((body) => {
        // A full navigation, not a router push: the destination is the application that sent
        // the person here, and it is not part of this app.
        window.location.href = body.redirect_to;
      })
      .catch((err) => {
        setBusy(false);
        onError(err);
      });
  };

  const cancel = () => {
    // Back to where they came from, with the error the spec names, so the application can say
    // "you declined" rather than hanging on a window that never returns.
    const separator = asked.redirect_uri.includes("?") ? "&" : "?";
    const state = asked.state
      ? `&state=${encodeURIComponent(asked.state)}`
      : "";
    window.location.href = `${asked.redirect_uri}${separator}error=access_denied${state}`;
  };

  return (
    <div className="landing">
      <header className="landing-bar">
        <Mark size={30} title={PRODUCT} />
        <strong
          style={{ fontSize: "var(--text-lg)", letterSpacing: "-0.02em" }}
        >
          {PRODUCT}
        </strong>
      </header>

      <main
        className="landing-main"
        style={{ gridTemplateColumns: "minmax(0, 560px)" }}
      >
        <div className="hero-card">
          {!detail ? (
            <p className="muted">Checking what is being asked for…</p>
          ) : !detail.ok ? (
            <>
              <h2>This request cannot be granted</h2>
              <p style={{ color: "var(--text-muted)" }}>{detail.detail}</p>
              <p className="hint">
                Nothing has been granted and nothing was recorded. If an
                application sent you here, it is misconfigured — the error above
                is the part worth reporting.
              </p>
            </>
          ) : (
            <>
              <div className="eyebrow">
                <Icons.shield size={14} />
                Authorise an application
              </div>
              <h2 style={{ fontSize: "var(--text-2xl)" }}>
                Let <strong>{detail.client.name}</strong> act as you?
              </h2>
              <p
                style={{
                  color: "var(--text-muted)",
                  marginTop: "var(--space-3)",
                }}
              >
                {detail.note}
              </p>

              <ul className="list" style={{ marginTop: "var(--space-5)" }}>
                {detail.scopes.map((scope) => (
                  <li key={scope.name} className="list-row">
                    <div style={{ display: "flex", gap: "var(--space-3)" }}>
                      <Icons.check
                        size={18}
                        style={{ flex: "none", marginTop: 2 }}
                      />
                      <div>
                        <strong>{scope.describes}</strong>
                        <p className="hint" style={{ margin: "2px 0 0" }}>
                          {scope.name}
                        </p>
                      </div>
                    </div>
                  </li>
                ))}
              </ul>

              {/* Said plainly and near the buttons, because it is what makes the decision
                  reversible and people decide differently when they know that. */}
              <p className="hint" style={{ marginTop: "var(--space-4)" }}>
                You can revoke this at any time in Settings. Nothing listed here
                can send an email or approve one — no application can, whatever
                it is granted.
              </p>

              <div className="btn-row" style={{ marginTop: "var(--space-5)" }}>
                <button
                  type="button"
                  className="btn"
                  data-variant="brand"
                  data-size="lg"
                  disabled={busy}
                  onClick={allow}
                >
                  {busy ? "Granting…" : "Allow"}
                </button>
                <button
                  type="button"
                  className="btn"
                  data-size="lg"
                  onClick={cancel}
                >
                  Cancel
                </button>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  );
}
