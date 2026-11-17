// A campaign's rules: the schedule, the caps, the footer, and the controls behind a role.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.
//
// Settings sit behind a role rather than a hidden button: a `member` can review drafts but
// cannot change the rules underneath them, and the API refuses them too.

import { useCallback, useEffect, useState } from "react";

import { getJson, patchJson } from "./api";
import { Icons, PageHeader, modeExplanation, modeTone } from "./ui";
import { SendAllowlist, SendModeControl, Suppressions } from "./SendControls";
import Team from "./Team";
import { McpTokens, OAuthClients } from "./Tokens";

//: Weekday 0 is Monday, matching `CampaignSettings.send_days`. Checkboxes rather than a text
//: field precisely because the one production outage this screen prevents was a hand-written
//: UPDATE that put the *names* in that column.
const WEEKDAYS = [
  [0, "Mon"],
  [1, "Tue"],
  [2, "Wed"],
  [3, "Thu"],
  [4, "Fri"],
  [5, "Sat"],
  [6, "Sun"],
];

export default function Settings({ campaign, isOwner, onError, onToast, onChanged }) {
  const [config, setConfig] = useState(null);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [fieldErrors, setFieldErrors] = useState([]);

  const loadConfig = useCallback(() => {
    getJson("/admin/sending-config").then(setConfig).catch(onError);
  }, [onError]);

  useEffect(loadConfig, [loadConfig]);

  useEffect(() => {
    setDraft(campaign?.settings ? { ...campaign.settings } : null);
    setFieldErrors([]);
  }, [campaign?.id, campaign?.settings]);

  const set = (key, value) => setDraft({ ...draft, [key]: value });
  const number = (key) => (event) => set(key, Number(event.target.value));

  const toggleDay = (day) => {
    const days = draft?.send_days || [];
    set(
      "send_days",
      days.includes(day)
        ? days.filter((d) => d !== day)
        : [...days, day].sort(),
    );
  };

  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setFieldErrors([]);
    try {
      await patchJson(`/campaigns/${campaign.id}`, {
        json: { settings: draft },
      });
      onToast(
        "Settings saved. The send loop picks them up on its next cycle.",
        "success",
      );
      onChanged();
    } catch (err) {
      const detail = err?.detail;
      if (detail && Array.isArray(detail.errors)) setFieldErrors(detail.errors);
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHeader
        icon={Icons.settings}
        eyebrow="Controls"
        title={<>Settings</>}
      >
        Sending rules are per campaign, so choosing a different one in the bar
        above changes what is edited here. Only an owner or admin can open this
        screen, and the API checks the role as well — a hidden button is not a
        permission.
      </PageHeader>

      <h2 className="section-title">Sending</h2>
      <p className="muted">
        The send mode is the one setting that decides whether mail actually
        leaves. Restricting it takes effect within seconds, on the send
        loop&apos;s next cycle. Enabling <strong>live</strong> is owner-only,
        refused outside production, and has to be typed out — it means every
        address in a campaign can be emailed, not only the allowlist.
      </p>

      {config ? (
        <div className="card" style={{ marginTop: 12 }}>
          <table className="kv">
            <tbody>
              <tr>
                <td>Send mode</td>
                <td>
                  <span
                    className="badge"
                    data-tone={modeTone(config.send_mode)}
                  >
                    {config.send_mode}
                  </span>{" "}
                  <span className="muted">{modeExplanation(config)}</span>
                </td>
              </tr>
              <tr>
                <td>Set by</td>
                <td className="muted">
                  {config.source === "dashboard"
                    ? `this dashboard${config.set_by ? ` — ${config.set_by}` : ""}${
                        config.set_at
                          ? ` on ${new Date(config.set_at).toLocaleString()}`
                          : ""
                      }`
                    : `the server environment (${config.environment_default})`}
                </td>
              </tr>
              <tr>
                <td>
                  {(config.sending_accounts || []).length > 1
                    ? "Sending accounts"
                    : "Sending account"}
                </td>
                <td className="mono">
                  {(config.sending_accounts || []).length
                    ? (config.sending_accounts || []).join(", ")
                    : config.sending_account ||
                      "none yet — add one when you create a campaign"}
                </td>
              </tr>
              <tr>
                <td>SMTP</td>
                <td className="mono">
                  {config.smtp_host}:{config.smtp_port}
                </td>
              </tr>
              <tr>
                <td>Environment</td>
                <td>{config.environment}</td>
              </tr>
              <tr>
                <td>Always on</td>
                <td className="muted">
                  Every first email and every reply is reviewed by a human ·
                  plain text only · no tracking pixels · no link rewriting · a
                  meeting is only ever marked booked by a person
                </td>
              </tr>
            </tbody>
          </table>

          {config.editable ? (
            <SendModeControl
              config={config}
              onError={onError}
              onToast={onToast}
              onDone={loadConfig}
            />
          ) : (
            <p className="muted" style={{ marginTop: 14 }}>
              Only an owner can change the send mode.
            </p>
          )}
          <SendAllowlist
            mode={config.send_mode}
            onError={onError}
            onToast={onToast}
          />
        </div>
      ) : (
        <p className="muted">Loading…</p>
      )}

      <h2 className="section-title">Sending rules for {campaign.name}</h2>
      <p className="muted">
        When and how fast this campaign may send. Saved through the same
        validation the worker uses, so a value this form accepts is a value the
        worker can read.
      </p>

      {draft ? (
        <form className="card" onSubmit={save} style={{ marginTop: 12 }}>
          <span className="form-label">Send days</span>
          <div className="day-row">
            {WEEKDAYS.map(([day, name]) => {
              const on = (draft.send_days || []).includes(day);
              return (
                <label key={day} className="day" data-on={on}>
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => toggleDay(day)}
                  />
                  {name}
                </label>
              );
            })}
          </div>

          <div className="field-grid">
            <div className="field">
              <label htmlFor="ws">Window opens ({draft.timezone})</label>
              <input
                id="ws"
                type="time"
                step="1"
                value={draft.window_start || ""}
                onChange={(event) => set("window_start", event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="we">Window closes ({draft.timezone})</label>
              <input
                id="we"
                type="time"
                step="1"
                value={draft.window_end || ""}
                onChange={(event) => set("window_end", event.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label htmlFor="cap">Daily cap</label>
            <input
              id="cap"
              type="number"
              min="1"
              max="2000"
              value={draft.daily_cap ?? ""}
              onChange={number("daily_cap")}
            />
            <p className="hint">
              Counts every outbound email, including follow-ups and replies. At
              steady state each lead costs two sends, so 100 is roughly 50 new
              leads a day plus 50 follow-ups.
            </p>
          </div>

          <div className="field-grid">
            <div className="field">
              <label htmlFor="fad">Days before a follow-up</label>
              <input
                id="fad"
                type="number"
                min="1"
                max="60"
                value={draft.followup_after_days ?? ""}
                onChange={number("followup_after_days")}
              />
            </div>
            <div className="field">
              <label htmlFor="mf">Maximum follow-ups</label>
              <input
                id="mf"
                type="number"
                min="0"
                max="5"
                value={draft.max_followups ?? ""}
                onChange={number("max_followups")}
              />
              <p className="hint">
                0 means none. Any reply, bounce or opt-out cancels them.
              </p>
            </div>
          </div>

          {fieldErrors.length ? (
            <div className="banner" data-tone="bad">
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {fieldErrors.map((item) => (
                  <li key={item.field}>
                    <strong>{item.field}</strong>: {item.message}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="btn-row">
            <button
              type="submit"
              className="btn"
              data-variant="primary"
              disabled={busy}
            >
              {busy ? "Saving…" : "Save settings"}
            </button>
            <button
              type="button"
              className="btn"
              data-variant="quiet"
              disabled={busy}
              onClick={() => setDraft({ ...campaign.settings })}
            >
              Discard changes
            </button>
          </div>
        </form>
      ) : null}

      <h2 className="section-title">Team</h2>
      <Team onError={onError} onToast={onToast} />

      <h2 className="section-title">Connect your own Claude</h2>
      <McpTokens onError={onError} onToast={onToast} />

      <h2 className="section-title">Connected applications</h2>
      <OAuthClients isOwner={isOwner} onError={onError} onToast={onToast} />

      <h2 className="section-title">Suppression list</h2>
      <Suppressions onError={onError} onToast={onToast} />
    </>
  );
}


/* ---------------------------------------------------------- OAuth clients (ADR-058, S17.3)

   **The server has been able to run an authorization code flow since ADR-058 and nobody could
   use it**, because a flow needs a registered client and registering one was a `POST` with
   curl. That is ADR-048's failure exactly: built, correct and unreachable -- so the feature
   whose entire point was "you no longer paste a token" could only be set up by somebody
   already able to paste anything.

   Owner-only, because this is the one screen in the product that decides what a consent screen
   says. The redirect URIs are where authorization codes are delivered, so whoever can add one
   can put their own address on a page that reads "allow this". The gate is the route's; this
   only decides what to draw, and offering a form that always 403s is worse than offering
   none. */
