// The team screen: who is in this workspace, and who may change what.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.

import { useCallback, useEffect, useState } from "react";

import { getJson } from "./api";

export default function Admin({ onError, onToast }) {
  const [data, setData] = useState(null);
  const [failed, setFailed] = useState(false);

  const load = useCallback(() => {
    getJson("/admin/overview")
      .then((body) => {
        setData(body);
        setFailed(false);
      })
      // "Loaded" has to mean *we asked and heard back*, including hearing an error. A skeleton
      // that spins for ever above a real problem is the bug this project already shipped once.
      .catch((err) => {
        setFailed(true);
        onError(err);
      });
  }, [onError]);

  useEffect(load, [load]);

  if (failed)
    return <p className="muted">Could not load the admin overview.</p>;
  if (!data) return <p className="muted">Loading…</p>;

  const { totals, workspace, google_test_users: google } = data;

  // The distinct domains this workspace sends from. Reputation attaches to the domain rather
  // than to the address, so two mailboxes on one domain share one answer and naming both would
  // imply they do not.
  const sendingDomains = [
    ...new Set(
      (data.mailboxes || [])
        .map((row) => (row.from_email || "").split("@")[1])
        .filter(Boolean)
        .map((domain) => domain.toLowerCase()),
    ),
  ];

  return (
    <>
      <h1>Admin</h1>
      <p className="muted">
        Only an owner or admin can open this screen, and the API checks the role
        itself rather than trusting the button being hidden.
      </p>

      <dl className="stat-grid" style={{ marginTop: 14 }}>
        <div className="stat">
          <dt>People</dt>
          <dd className="num">{totals.members}</dd>
        </div>
        <div className="stat">
          <dt>Invited, not yet signed in</dt>
          <dd className="num">{totals.invited}</dd>
        </div>
        <div className="stat">
          <dt>Mailboxes connected</dt>
          <dd className="num">
            {totals.connected}/{totals.mailboxes}
          </dd>
        </div>
        <div className="stat">
          <dt>Need reconnecting</dt>
          {/* Coloured only when non-zero: a red "0" is an alarm about nothing. */}
          <dd
            className="num"
            data-tone={totals.needs_reconnect ? "bad" : undefined}
          >
            {totals.needs_reconnect}
          </dd>
        </div>
      </dl>

      <h2 className="section-title">People</h2>
      <p className="muted">
        Whether somebody may sign in at all is decided by Supabase, which this
        system cannot read — it never joins across the identity provider&apos;s
        boundary. A row below means they have authenticated here at least once,
        which is the only honest signal we have.
      </p>
      <table className="table" style={{ marginTop: 10 }}>
        <thead>
          <tr>
            <th>Email</th>
            <th>Role</th>
            <th>Signed in</th>
          </tr>
        </thead>
        <tbody>
          {data.people.map((person) => (
            <tr key={person.id}>
              <td>
                {person.email || "no address on file"}
                {person.is_you ? <span className="badge">you</span> : null}
              </td>
              <td>
                <span className="badge">{person.role}</span>
              </td>
              <td>
                <span className="badge" data-tone="ok">
                  yes
                </span>
              </td>
            </tr>
          ))}
          {data.invited.map((row) => (
            <tr key={row.id}>
              <td className="muted">{row.email}</td>
              <td>
                <span className="badge">{row.role}</span>
              </td>
              <td>
                <span className="badge" data-tone="warn">
                  invited
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted" style={{ marginTop: 8 }}>
        Invite and remove people in Settings → Team.
      </p>

      <h2 className="section-title">Sending accounts</h2>
      {data.mailboxes.length ? (
        <table className="table" style={{ marginTop: 10 }}>
          <thead>
            <tr>
              <th>Address</th>
              <th>Connected</th>
              <th>By</th>
              <th>Days left</th>
            </tr>
          </thead>
          <tbody>
            {data.mailboxes.map((row) => (
              <tr key={row.id}>
                <td>{row.from_email}</td>
                <td>
                  {row.connected ? (
                    <span className="badge" data-tone="ok">
                      connected
                    </span>
                  ) : row.needs_reconnect ? (
                    <span className="badge" data-tone="bad">
                      needs reconnecting
                    </span>
                  ) : (
                    <span className="badge">not connected</span>
                  )}
                  {row.paused ? (
                    <span className="badge" data-tone="warn">
                      paused
                    </span>
                  ) : null}
                </td>
                <td className="muted">{row.connected_by || "—"}</td>
                <td>{row.days_left === null ? "—" : row.days_left}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">
          No mailbox yet. One is created when a campaign is made.
        </p>
      )}

      {/* Deliverability belongs to the sending domain, not to this deployment (ADR-023 left us
          with no mailbox of our own), so this is the only honest place to say it: beside the
          address that actually has the reputation. A link rather than a panel — showing the
          numbers here would mean holding OAuth credentials for every tenant's Google account to
          redraw a dashboard Google already draws for them. See docs/POSTMASTER.md. */}
      {sendingDomains.length ? (
        <p className="muted" style={{ marginTop: 8 }}>
          Whether your mail reaches inboxes is a property of{" "}
          {sendingDomains.length > 1
            ? "your sending domains"
            : "your sending domain"}{" "}
          (<span className="mono">{sendingDomains.join(", ")}</span>), not of
          this system — so it is yours to watch.{" "}
          <a
            href="https://postmaster.google.com"
            target="_blank"
            rel="noreferrer"
          >
            Google Postmaster Tools
          </a>{" "}
          is free and shows spam rate, reputation and whether SPF and DKIM are
          passing, for mail to <span className="mono">@gmail.com</span>{" "}
          addresses. It has no history before you verify the domain, so it is
          worth doing before your first campaign rather than after a problem.
          Expect it to be sparse at low volume — Google withholds data when a
          day&apos;s volume is small.
        </p>
      ) : null}

      {google.required_while_unpublished ? (
        <>
          <h2 className="section-title">Before somebody can connect Gmail</h2>
          <div className="banner" data-tone="warn">
            <p>{google.note}</p>
            <p style={{ marginTop: 8 }}>
              <a href={google.console_url} target="_blank" rel="noreferrer">
                Open the Google Cloud console
              </a>{" "}
              → Audience → Test users.
            </p>
          </div>
        </>
      ) : null}

      <h2 className="section-title">Workspace</h2>
      <table className="table" style={{ marginTop: 10 }}>
        <tbody>
          <tr>
            <td>Sign-up</td>
            <td>
              <span className="badge">{workspace.signup_mode}</span>{" "}
              {workspace.signup_mode === "open"
                ? "anyone Supabase lets sign in gets their own workspace"
                : workspace.signup_mode === "closed"
                  ? "nobody new, unless invited"
                  : "only approved email domains"}
            </td>
          </tr>
          <tr>
            <td>Gmail connections last</td>
            <td>
              {workspace.grant_days > 0
                ? `${workspace.grant_days} days, then each person reconnects`
                : "until revoked (the OAuth client is published)"}
            </td>
          </tr>
          <tr>
            <td>Connect Gmail available</td>
            <td>
              {workspace.google_oauth_configured
                ? "yes"
                : "no — the server has no OAuth client"}
            </td>
          </tr>
          <tr>
            <td>Workspace limit</td>
            <td>{workspace.max_orgs}</td>
          </tr>
        </tbody>
      </table>
    </>
  );
}

/* ---------------------------------------------------------------- settings */
