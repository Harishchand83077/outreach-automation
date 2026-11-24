// The two kinds of credential a workspace issues: OAuth clients and connector tokens.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.
//
// A token is shown once, at creation, and never again: the API stores a hash (ADR-058), so there
// is nothing left to show even if a screen asked for it.

import { useCallback, useEffect, useState } from "react";

import { API, deleteJson, getJson, postJson } from "./api";
import { useLoad } from "./useLoad";
import { CopyButton, Field, SkeletonList, TextArea } from "./ui";

export function OAuthClients({ isOwner, onError, onToast }) {
  const clients = useLoad("/oauth/clients", onError);
  const [clientId, setClientId] = useState("");
  const [name, setName] = useState("");
  const [uris, setUris] = useState("");
  const [busy, setBusy] = useState(false);

  const add = (event) => {
    event.preventDefault();
    setBusy(true);
    postJson("/oauth/clients", {
      json: {
        client_id: clientId.trim(),
        name: name.trim(),
        // One per line is the shape somebody can paste out of a provider's documentation, and
        // the server matches exactly -- so trailing whitespace would silently break a URI that
        // looks right on the screen.
        redirect_uris: uris
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter(Boolean),
      },
    })
      .then(() => {
        setClientId("");
        setName("");
        setUris("");
        onToast("Registered. It can now ask people in this workspace for access.");
        clients.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  const disable = (row) => {
    setBusy(true);
    deleteJson(`/oauth/clients/${row.id}`)
      .then((body) => {
        onToast(body?.note || "Disabled.");
        clients.reload();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  // Not `clients.data?.clients?.length ? ... : "none yet"` -- that states a falsehood for the
  // half-second before the fetch returns, which this dashboard has already shipped once.
  if (!clients.asked) return <SkeletonList count={2} rows={2} />;

  return (
    <>
      <p className="hint">
        A client is an application allowed to ask the people in this workspace for access to it
        -- your own Claude, for instance. Registering one is what lets somebody connect by
        pressing Allow instead of pasting a token from the panel above.
        {clients.data?.note ? ` ${clients.data.note}` : ""}
      </p>

      {clients.data?.clients?.length ? (
        <ul className="list">
          {clients.data.clients.map((row) => (
            <li className="list-row" key={row.id}>
              <div style={{ minWidth: 0 }}>
                <strong>{row.name}</strong>{" "}
                {row.disabled ? (
                  <span className="pill">disabled</span>
                ) : (
                  <span className="pill" data-tone="success">
                    <span className="pill-dot" />
                    active
                  </span>
                )}
                <p className="hint mono" style={{ margin: "4px 0 0" }}>
                  {row.client_id}
                </p>
                {/* Shown because an exact-match rule cannot be debugged without seeing what was
                    actually stored: a wrong port or a trailing slash is the commonest reason a
                    flow dies, and from the outside it reads as our fault. */}
                <p className="hint" style={{ margin: "2px 0 0" }}>
                  {(row.redirect_uris || []).join(" / ") || "no redirect URI"}
                </p>
              </div>
              {isOwner && !row.disabled ? (
                <div className="btn-row">
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() => disable(row)}
                  >
                    Disable
                  </button>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">
          No clients registered, so nobody can connect by pressing Allow. The tokens above still
          work.
        </p>
      )}

      {isOwner ? (
        <form onSubmit={add} style={{ marginTop: 16 }}>
          <Field
            label="Client ID"
            required
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
            hint="Whatever the application identifies itself as. Matched exactly."
          />
          <Field
            label="Name"
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
            hint="What the consent screen shows. Use a name the people being asked will recognise."
          />
          <TextArea
            label="Redirect URIs"
            required
            rows={3}
            value={uris}
            onChange={(event) => setUris(event.target.value)}
            hint="One per line, https (localhost is exempt). Matched exactly -- the scheme, the port and any path all count."
          />
          <div className="btn-row">
            <button
              type="submit"
              className="btn"
              data-variant="brand"
              disabled={busy || !clientId.trim() || !name.trim() || !uris.trim()}
            >
              Register client
            </button>
          </div>
        </form>
      ) : (
        <p className="hint">Only an owner can register or disable a client.</p>
      )}
    </>
  );
}

export function McpTokens({ onError, onToast }) {
  const [data, setData] = useState(null);
  const [name, setName] = useState("My Claude");
  const [busy, setBusy] = useState(false);
  // Off unless somebody ticks it, which is the same decision as the send mode defaulting to
  // disabled: the narrow setting is the one you get without choosing.
  const [mayPropose, setMayPropose] = useState(false);
  // One box each, and each off unless ticked (F24, ADR-056). Separate rather than a single
  // "full access", because the question somebody can actually answer is "may this read my own
  // career facts?" and not "do I trust it".
  const [mayReadProfile, setMayReadProfile] = useState(false);
  const [mayWriteNotes, setMayWriteNotes] = useState(false);
  // The minted secret, held in memory only and shown once. Never put in localStorage: a
  // credential that outlives the page it was shown on is one nobody remembers is there.
  const [minted, setMinted] = useState(null);

  const load = useCallback(() => {
    getJson("/mcp-tokens").then(setData).catch(onError);
  }, [onError]);

  useEffect(load, [load]);

  const mint = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await postJson("/mcp-tokens", {
        json: {
          name,
          may_propose: mayPropose,
          may_read_profile: mayReadProfile,
          may_write_notes: mayWriteNotes,
        },
      });
      setMinted(created);
      onToast(
        "Token created. Copy it now \u2014 it is not shown again.",
        "success",
      );
      load();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (id) => {
    try {
      await deleteJson(`/mcp-tokens/${id}`);
      onToast("Token revoked.", "success");
      load();
    } catch (err) {
      onError(err);
    }
  };

  const endpoint = `${API.replace(/\/api\/v1$/, "")}/mcp`;

  return (
    <>
      <p className="muted">
        Add this workspace to your own Claude as a custom connector, and ask it
        about your campaigns, leads, research and replies. It spends your model
        quota rather than ours.
      </p>
      <p className="muted">
        <strong>It cannot send.</strong> There is no tool that sends mail,
        approves a draft or changes a sending setting, and no permission that
        could grant one. A token reads your own workspace, and may optionally
        write a draft into your review queue &mdash; where a person still has to
        read it and press approve, exactly as for a draft this system wrote
        itself.
      </p>

      <dl className="kv">
        <dt>Server URL</dt>
        <dd className="mono">{endpoint}</dd>
        <dt>Where to add it</dt>
        <dd>
          In Claude: Customize &rarr; Connectors &rarr; Add custom connector.
        </dd>
        <dt>Authentication</dt>
        <dd>
          A request header:{" "}
          <span className="mono">Authorization: Bearer &lt;token&gt;</span>
        </dd>
      </dl>

      {minted ? (
        <div className="banner" data-tone="warn" role="alert">
          <p>
            <strong>Copy this now.</strong> It is not stored and cannot be shown
            again. If you lose it, revoke it and make another.
          </p>
          <p className="mono" style={{ wordBreak: "break-all" }}>
            {minted.token}
          </p>
          <span className="row">
            <CopyButton text={minted.token} label="Copy token" />
            <button type="button" className="btn" onClick={() => setMinted(null)}>
              I have copied it
            </button>
          </span>
        </div>
      ) : null}

      <form onSubmit={mint} className="field-row">
        <label className="field">
          <span>Name this token</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={80}
            placeholder="My Claude"
          />
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={mayPropose}
            onChange={(event) => setMayPropose(event.target.checked)}
          />
          <span>
            Let it <strong>propose drafts</strong> into the review queue
          </span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={mayReadProfile}
            onChange={(event) => setMayReadProfile(event.target.checked)}
          />
          <span>
            Let it read your <strong>confirmed career facts</strong>
          </span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={mayWriteNotes}
            onChange={(event) => setMayWriteNotes(event.target.checked)}
          />
          <span>
            Let it <strong>record notes about companies</strong>, each with its
            source
          </span>
        </label>
        <button type="submit" className="btn" disabled={busy || !name.trim()}>
          {busy ? "Creating\u2026" : "Create token"}
        </button>
      </form>
      <p className="muted">
        Leave them all unticked and the token can only read this workspace's own
        work &mdash; campaigns, leads, replies, postings, signals. Each box adds
        exactly one thing and nothing else.
      </p>
      <p className="muted">
        <strong>Propose drafts:</strong> your Claude can write a draft or a
        reply for one of your leads. It goes through the same grounding checks
        as ours, lands in the review queue as <em>pending review</em>, and still
        needs a person to approve it before anything is sent.
      </p>
      <p className="muted">
        <strong>Career facts:</strong> the facts you have confirmed about your
        own work, by id, so anything it writes can cite them. It never includes
        the answers in your answer bank &mdash; your expected pay, notice period
        and work authorisation are given to a form you are filling in, and are
        not ours to pass on to a model. It can see which of those questions you
        have answered, and not what you answered.
      </p>
      <p className="muted">
        <strong>Company notes:</strong> it can record one line about a company
        <em> with the URL of the page that says it</em>. A note with no source
        is refused by the database, not by a rule somebody could relax. A note
        is not a research finding: nothing this system writes may cite it until
        you have read it.
      </p>

      {data?.tokens?.length ? (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Token</th>
                <th>Can do</th>
                <th>Created</th>
                <th>Expires</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.tokens.map((row) => (
                <tr key={row.id}>
                  <td>{row.name}</td>
                  <td className="mono">{row.prefix}</td>
                  <td className="muted">
                    {/* The scope the server actually issues, named by the server, so this
                        cannot label a row with a string it does not use. */}
                    {data.ext_scope &&
                    (row.scopes || []).includes(data.ext_scope)
                      ? "browser extension"
                      : data.propose_scope &&
                          (row.scopes || []).includes(data.propose_scope)
                        ? "read + propose drafts"
                        : "read only"}
                  </td>
                  <td className="muted">{row.created_at.slice(0, 10)}</td>
                  <td className="muted">
                    {row.expires_at ? row.expires_at.slice(0, 10) : "never"}
                  </td>
                  <td className="muted">
                    {row.last_used_at
                      ? row.last_used_at.slice(0, 10)
                      : "never used"}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => revoke(row.id)}
                    >
                      Revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">No tokens yet.</p>
      )}
    </>
  );
}

// The browser extension's own credential. A separate door from the connector (ADR-039): this token
// opens `/ext/v1` and nothing else, so it can be pasted into a browser without that browser being
// able to read the workspace.
export function ExtensionToken({ onError, onToast }) {
  const [name, setName] = useState("Chrome extension");
  const [busy, setBusy] = useState(false);
  // Held in memory only and shown once, as for the connector tokens above.
  const [minted, setMinted] = useState(null);
  const apiBase = API.replace(/\/api\/v1$/, "");

  const mint = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await postJson("/mcp-tokens", {
        json: { name, for_extension: true },
      });
      setMinted(created);
      onToast("Extension token created. Copy it now — it is not shown again.", "success");
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="muted">
        The Chrome side panel suggests answers to application questions from your confirmed
        answer bank, and can save a job posting you are looking at. It never fills in or submits
        a form. Its token opens one door only &mdash; it cannot read your campaigns, leads or
        replies. Revoke it from the token list above.
      </p>
      <dl className="kv">
        <dt>API address</dt>
        <dd className="mono">
          {apiBase} <CopyButton text={apiBase} />
        </dd>
        <dt>Install</dt>
        <dd>
          <span className="mono">cd extension &amp;&amp; npm ci &amp;&amp; npm run build</span>, then
          chrome://extensions &rarr; Developer mode &rarr; Load unpacked &rarr; extension/dist.
        </dd>
      </dl>
      {minted ? (
        <div className="banner" data-tone="warn" role="alert">
          <p>
            <strong>Copy this now.</strong> It is not stored and cannot be shown again.
          </p>
          <p className="mono" style={{ wordBreak: "break-all" }}>
            {minted.token}
          </p>
          <span className="row">
            <CopyButton text={minted.token} label="Copy token" />
            <button type="button" className="btn" onClick={() => setMinted(null)}>
              I have copied it
            </button>
          </span>
        </div>
      ) : null}
      <form onSubmit={mint} className="field-row">
        <label className="field">
          <span>Name this token</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            maxLength={80}
          />
        </label>
        <button type="submit" className="btn" disabled={busy || !name.trim()}>
          {busy ? "Creating…" : "Create extension token"}
        </button>
      </form>
    </>
  );
}
