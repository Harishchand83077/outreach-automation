// The scope above orgs (ADR-024): granted by hand, held by almost nobody.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.

import { useCallback, useEffect, useState } from "react";

import { getJson } from "./api";

export default function Platform({ onError }) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    getJson("/platform/overview")
      .then((body) => {
        setData(body);
        setFailed(false);
      })
      // "Loaded" means we asked and heard back, including hearing an error.
      .catch((err) => {
        setFailed(true);
        onError(err);
      });
  }, [onError]);

  useEffect(load, [load]);

  if (failed)
    return <p className="muted">Could not load the platform overview.</p>;
  if (!data) return <p className="muted">Loading…</p>;

  const { workspaces, totals, health, usage, deployment } = data;
  const stalled = health.workspaces_with_no_working_mailbox || [];
  const workerAge = health.worker_seen_seconds_ago;

  return (
    <>
      <h1>Platform</h1>
      <p className="muted">
        Every workspace on this deployment, in aggregate. This screen
        deliberately shows no leads, drafts, replies or campaign names — a
        workspace owner agreed to this system sending their mail, not to its
        operator reading their pipeline. The API enforces that rather than the
        layout: nothing behind this screen can reach one org&apos;s data.
      </p>

      <dl className="stat-grid" style={{ marginTop: 14 }}>
        <div className="stat">
          <dt>Workspaces</dt>
          <dd className="num">
            {totals.workspaces}
            {deployment.max_orgs ? (
              <span className="muted">/{deployment.max_orgs}</span>
            ) : null}
          </dd>
        </div>
        <div className="stat">
          <dt>People</dt>
          <dd className="num">{totals.members}</dd>
        </div>
        <div className="stat">
          <dt>Mailboxes connected</dt>
          <dd className="num">
            {totals.mailboxes_connected}/{totals.mailboxes}
          </dd>
        </div>
        <div className="stat">
          <dt>Emails sent</dt>
          <dd className="num">{totals.emails_sent}</dd>
        </div>
      </dl>

      <h2 className="section-title">Health</h2>
      {stalled.length ? (
        <p className="banner" data-tone="bad">
          {stalled.length} workspace(s) have a mailbox but none that
          authenticates, so they have silently stopped sending <em>and</em>{" "}
          stopped fetching replies. Nothing inside those workspaces says so —
          the owner has to press Reconnect.
        </p>
      ) : (
        <p className="muted">
          Every workspace with a mailbox has at least one that works.
        </p>
      )}
      <dl className="stat-grid">
        <div className="stat">
          <dt>Worker last seen</dt>
          <dd
            className="num"
            data-tone={workerAge == null || workerAge > 120 ? "bad" : undefined}
          >
            {workerAge == null ? "never" : workerAge + "s ago"}
          </dd>
        </div>
        <div className="stat">
          <dt>Dead jobs</dt>
          {/* Coloured only when non-zero: a red zero is an alarm about nothing. */}
          <dd className="num" data-tone={health.dead_jobs ? "bad" : undefined}>
            {health.dead_jobs}
          </dd>
        </div>
        <div className="stat">
          <dt>Alerts unacknowledged</dt>
          <dd
            className="num"
            data-tone={health.alerts_open ? "warn" : undefined}
          >
            {health.alerts_open}
          </dd>
        </div>
      </dl>

      <h2 className="section-title">Quota</h2>
      <p className="muted">
        The free-tier model chain is the real ceiling on this system — not CPU
        and not the database — so this is the number that decides whether it
        keeps working.
      </p>
      <dl className="stat-grid">
        <div className="stat">
          <dt>LLM calls, 24h</dt>
          <dd className="num">{usage.llm_calls_24h}</dd>
        </div>
        <div className="stat">
          <dt>Tokens, 24h</dt>
          <dd className="num">{usage.llm_tokens_24h.toLocaleString()}</dd>
        </div>
        <div className="stat">
          <dt>LLM failures, 24h</dt>
          <dd
            className="num"
            data-tone={usage.llm_failures_24h ? "warn" : undefined}
          >
            {usage.llm_failures_24h}
          </dd>
        </div>
        <div className="stat">
          <dt>Search credits, 30d</dt>
          <dd className="num">
            {usage.search_credits_30d}
            <span className="muted">/{usage.search_budget}</span>
          </dd>
        </div>
      </dl>

      <h2 className="section-title">Workspaces</h2>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Workspace</th>
              <th>Joined</th>
              <th>People</th>
              <th>Mailboxes</th>
              <th>Campaigns</th>
              <th>Leads</th>
              <th>Sent</th>
              <th>Send mode</th>
            </tr>
          </thead>
          <tbody>
            {workspaces.map((row) => (
              <tr key={row.id}>
                <td>
                  {row.name}
                  {row.paused ? (
                    <span className="badge" data-tone="warn">
                      paused
                    </span>
                  ) : null}
                </td>
                <td className="muted">{row.created_at.slice(0, 10)}</td>
                <td>{row.members}</td>
                <td
                  data-tone={
                    row.mailboxes && !row.mailboxes_connected
                      ? "bad"
                      : undefined
                  }
                >
                  {row.mailboxes_connected}/{row.mailboxes}
                </td>
                <td>
                  {row.campaigns_running}/{row.campaigns}
                </td>
                <td>{row.leads}</td>
                <td>{row.emails_sent}</td>
                <td>
                  <span
                    className="badge"
                    data-tone={row.send_mode === "live" ? "warn" : undefined}
                  >
                    {row.send_mode}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="section-title">Deployment</h2>
      <dl className="kv">
        <dt>Environment</dt>
        <dd>{deployment.app_env}</dd>
        <dt>Sign-up mode</dt>
        <dd>
          {deployment.signup_mode} — only as open as Supabase is. Turning off{" "}
          <em>Allow new users to sign up</em> there leaves this invite-only,
          with the guest list in a UI rather than in SQL.
        </dd>
        <dt>Google OAuth</dt>
        <dd>
          {deployment.google_oauth_configured ? "configured" : "not configured"}
        </dd>
        <dt>Grant length</dt>
        <dd>
          {deployment.grant_days === 0
            ? "never expires, so the OAuth client has been published"
            : deployment.grant_days +
              " days, then every mailbox needs reconnecting"}
        </dd>
      </dl>
    </>
  );
}
