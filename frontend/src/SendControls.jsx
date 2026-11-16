// The three controls that decide whether anything can be sent, and to whom.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.
//
// **Hard rule 1 lives behind these screens, not inside them.** The mode resolves in
// `effective_send_mode()` and is checked in exactly one function, both in
// `app/services/mail/smtp.py`. These are the controls that ask the API to change it, and
// `tests/unit/test_send_guard.py` asserts structurally that nothing else decides.

import { useCallback, useEffect, useState } from "react";

import { deleteJson, getJson, patchJson, postJson } from "./api";
import { Field } from "./ui";

export function SendAllowlist({ mode, onError, onToast }) {
  const [rows, setRows] = useState(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    // Degrades rather than erroring: an older server without this route leaves the panel out.
    getJson("/admin/send-allowlist")
      .then((data) => setRows(data.allowlist))
      .catch(() => setRows(null));
  }, []);

  useEffect(load, [load]);

  const add = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const made = await postJson("/admin/send-allowlist", {
        json: { email: value },
      });
      setValue("");
      onToast(
        made.added
          ? `${made.email} can now receive mail from this workspace.`
          : "Already listed.",
      );
      load();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id, email) => {
    try {
      await deleteJson(`/admin/send-allowlist/${id}`);
      onToast(`${email} removed. Nothing will be sent to it.`);
      load();
    } catch (err) {
      onError(err);
    }
  };

  if (!rows) return null;

  return (
    <div style={{ marginTop: 18 }}>
      <h3>Allowlist</h3>
      <p className="muted">
        While the send mode is <strong>allowlist</strong>, these are the only
        addresses this workspace can reach. It is the list for <em>this</em>{" "}
        workspace only — nobody else&apos;s rehearsal can put an address on it,
        and nothing here affects anybody else.
        {mode === "allowlist"
          ? ""
          : " The mode is not allowlist right now, so it is not in use."}
      </p>
      {rows.length ? (
        <ul style={{ listStyle: "none", padding: 0, margin: "10px 0" }}>
          {rows.map((row) => (
            <li
              key={row.id}
              style={{ display: "flex", alignItems: "center", gap: 8 }}
            >
              <span>{row.email}</span>
              <span className="spacer" />
              <button
                type="button"
                className="btn"
                data-size="sm"
                onClick={() => remove(row.id, row.email)}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted" data-tone="warn" style={{ marginTop: 10 }}>
          Empty, so in allowlist mode nothing can be sent at all. Add the
          address you want to rehearse against — usually one of your own.
        </p>
      )}
      <form onSubmit={add} className="field-row">
        <Field
          label="Allow an address"
          type="email"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          required
        />
        <button type="submit" className="btn" disabled={busy || !value}>
          Add
        </button>
      </form>
    </div>
  );
}

export function SendModeControl({ config, onError, onToast, onDone }) {
  const [choice, setChoice] = useState(config.send_mode);
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);

  // Typing LIVE is required by the API, and asking for it here rather than only showing the
  // server's 400 means the person reads what live actually does before they commit to it.
  const needsConfirm = choice === "live";
  const blocked = needsConfirm && (!config.live_allowed || confirm !== "LIVE");

  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const body = { send_mode: choice === "environment" ? null : choice };
      if (needsConfirm) body.confirm = confirm;
      const result = await patchJson("/admin/sending-config", { json: body });
      onToast(
        result.send_mode === "live"
          ? "Live sending is on. Every address in a running campaign can now be emailed."
          : `Send mode is now ${result.send_mode}.`,
        result.send_mode === "live" ? "error" : "success",
      );
      setConfirm("");
      onDone();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={save} style={{ marginTop: 16 }}>
      <div className="field">
        <label htmlFor="mode">Change send mode</label>
        <select
          id="mode"
          value={choice}
          onChange={(event) => setChoice(event.target.value)}
        >
          <option value="disabled">
            disabled — nothing is sent, to anybody
          </option>
          <option value="allowlist">
            allowlist — only this workspace&apos;s own list
          </option>
          <option value="live" disabled={!config.live_allowed}>
            live — any recipient
            {config.live_allowed ? "" : " (production only)"}
          </option>
          <option value="environment">
            follow the server ({config.environment_default})
          </option>
        </select>
        {config.live_allowed ? null : (
          <p className="hint">
            Live is unavailable because this server is running as{" "}
            <strong>{config.environment}</strong>, not production.
          </p>
        )}
      </div>

      {needsConfirm ? (
        <div className="banner" data-tone="bad">
          <div>
            <strong>This sends real email to real people.</strong> Every lead in
            a running campaign becomes eligible, not just the allowlist.
            Approval is still required for each draft, and the window and daily
            cap still apply.
            <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
              <label htmlFor="confirm">Type LIVE to confirm</label>
              <input
                id="confirm"
                type="text"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                placeholder="LIVE"
              />
            </div>
          </div>
        </div>
      ) : null}

      <div className="btn-row">
        <button
          type="submit"
          className="btn"
          data-variant={needsConfirm ? "danger" : "primary"}
          disabled={busy || blocked || choice === config.send_mode}
        >
          {busy ? "Saving…" : "Apply send mode"}
        </button>
      </div>
    </form>
  );
}

export function Suppressions({ onError, onToast }) {
  const [rows, setRows] = useState([]);
  const [value, setValue] = useState("");

  const load = useCallback(() => {
    getJson("/suppressions")
      .then((data) => setRows(data.suppressions))
      .catch(onError);
  }, [onError]);

  useEffect(load, [load]);

  const add = async (event) => {
    event.preventDefault();
    try {
      await postJson("/suppressions", { json: { value, reason: "manual" } });
      setValue("");
      onToast("Added. Nothing will be sent to that address again.", "success");
      load();
    } catch (err) {
      onError(err);
    }
  };

  const remove = async (id) => {
    try {
      await deleteJson(`/suppressions/${id}`);
      load();
    } catch (err) {
      onError(err);
    }
  };

  return (
    <>
      <p className="muted">
        Checked immediately before every send. An address erased on request is
        kept here as a hash, so a later import of the same sheet still cannot
        reach them.
      </p>

      <form className="card" onSubmit={add} style={{ marginTop: 12 }}>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="sup">Never contact</label>
          <input
            id="sup"
            type="email"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="someone@example.com"
          />
        </div>
        <div className="btn-row">
          <button type="submit" className="btn" disabled={!value.trim()}>
            Suppress
          </button>
        </div>
      </form>

      {rows.length === 0 ? (
        <p className="empty" style={{ marginTop: 14 }}>
          Nobody suppressed yet.
        </p>
      ) : (
        <div className="table-wrap" style={{ marginTop: 14 }}>
          <table>
            <thead>
              <tr>
                <th>Value</th>
                <th>Kind</th>
                <th>Reason</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.value}</td>
                  <td>
                    <span className="badge">{row.kind}</span>
                  </td>
                  <td>{row.reason}</td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      data-variant="quiet"
                      onClick={() => remove(row.id)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ bits */
