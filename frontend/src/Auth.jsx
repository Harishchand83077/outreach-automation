// Sign-in. Magic link only, deliberately.
//
// There is no password field anywhere in this app, and no sign-up form. Public sign-ups are
// disabled on the project (§14): a token only grants access if the owner has already put a row
// in `members`, so an invite is a database row rather than a form. A password field would add a
// credential to phish, lose and rotate, for an application with one user today.
//
// A magic link also means the API never sees a password, and neither does this bundle.

import React, { useEffect, useState } from "react";

import { authConfigured, supabase } from "./supabase";

export function useSession() {
  const [session, setSession] = useState(undefined); // undefined = still checking

  useEffect(() => {
    if (!supabase) {
      setSession(null);
      return undefined;
    }
    supabase.auth
      .getSession()
      .then(({ data }) => setSession(data?.session ?? null));
    const { data: listener } = supabase.auth.onAuthStateChange(
      (_event, next) => {
        setSession(next ?? null);
      },
    );
    return () => listener?.subscription?.unsubscribe();
  }, []);

  return session;
}

export function SignIn({ embedded = false }) {
  // `embedded` means "the caller has already given you a card": the landing page hosts this
  // form inside its own panel, and without this the form brought a second one with 80vh of
  // centring inside it. The standalone form is still used by nothing else today, and is kept
  // so this component works on its own.
  const Shell = embedded ? Bare : Panel;
  const [email, setEmail] = useState("");
  const [state, setState] = useState({ status: "idle", message: "" });

  if (!authConfigured) {
    return (
      <Shell title="Authentication is not configured">
        <p style={styles.text}>
          This build has no <code style={styles.code}>VITE_SUPABASE_URL</code>{" "}
          or <code style={styles.code}>VITE_SUPABASE_ANON_KEY</code>. Set both
          as build-time variables where the dashboard is deployed and rebuild.
          Both are public by design.
        </p>
      </Shell>
    );
  }

  const send = async (event) => {
    event.preventDefault();
    setState({ status: "sending", message: "" });
    const { error } = await supabase.auth.signInWithOtp({
      email: email.trim(),
      options: { emailRedirectTo: window.location.origin },
    });
    if (error) {
      setState({ status: "error", message: error.message });
      return;
    }
    // Deliberately the same message whether or not the address is a member: telling a stranger
    // which addresses exist is a free list of who to target.
    setState({
      status: "sent",
      message:
        "If that address has access, a sign-in link is on its way. It expires shortly.",
    });
  };

  return (
    <Shell title="Sign in">
      <form onSubmit={send}>
        <label htmlFor="email" style={styles.label}>
          Email address
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="you@yourdomain.com"
          style={styles.input}
        />
        <button
          type="submit"
          disabled={state.status === "sending"}
          style={styles.button}
        >
          {state.status === "sending" ? "Sending…" : "Email me a sign-in link"}
        </button>
      </form>
      {state.message ? (
        <p
          role="status"
          style={{
            ...styles.text,
            marginTop: 16,
            color:
              state.status === "error" ? "var(--danger)" : "var(--text-muted)",
          }}
        >
          {state.message}
        </p>
      ) : null}
      <p style={{ ...styles.text, marginTop: 20, fontSize: 13 }}>
        Access is invite-only. If you have not been added, the link will arrive
        but the dashboard will tell you there is no organisation for your
        account.
      </p>
    </Shell>
  );
}

/** No panel, because the caller is the card -- but the title stays.
 *
 * Dropping it was the first version of this, and it lost "Authentication is not configured",
 * which is the one sentence somebody with a half-built deployment needs to see. The heading
 * is this component's to render in both modes, so there is one place it comes from. */
function Bare({ title, children }) {
  return (
    <>
      <h2
        style={{
          margin: "0 0 var(--space-2)",
          fontSize: "var(--text-xl)",
          fontWeight: 650,
        }}
      >
        {title}
      </h2>
      {children}
    </>
  );
}

function Panel({ title, children }) {
  return (
    <div style={styles.wrap}>
      <div style={styles.panel}>
        <h1 style={styles.title}>{title}</h1>
        {children}
      </div>
    </div>
  );
}

const styles = {
  wrap: {
    display: "grid",
    placeItems: "center",
    minHeight: "80vh",
    padding: 20,
  },
  panel: {
    width: "100%",
    maxWidth: 420,
    background: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: 12,
    padding: 28,
  },
  title: { fontSize: 22, fontWeight: 700, margin: "0 0 18px" },
  label: {
    display: "block",
    fontSize: 13,
    marginBottom: 6,
    color: "var(--text-muted)",
  },
  input: { width: "100%", marginBottom: 14 },
  button: {
    width: "100%",
    background: "var(--accent)",
    color: "#fff",
    borderColor: "var(--accent)",
  },
  text: {
    color: "var(--text-muted)",
    fontSize: 14,
    lineHeight: 1.6,
    margin: 0,
  },
  code: {
    background: "var(--surface-2)",
    padding: "2px 6px",
    borderRadius: 4,
    fontSize: 13,
  },
};
