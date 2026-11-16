// One campaign: its settings, its sending checks, its funnel and its experiment.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.

import { useCallback, useEffect, useState } from "react";

import {
  deleteJson,
  getJson,
  patchJson,
  postJson,
  putJson,
  uploadFile,
} from "./api";
import {
  ActivityChart,
  Field,
  Icons,
  PageHeader,
  SkeletonList,
  formatBytes,
  stateTone,
  toLocalInput,
} from "./ui";

// Named for the person reading them, not for the protocol. "imap" on screen is a reason to ask
// somebody what IMAP is; "receiving replies" is the thing that actually stops working.
const CHECK_LABELS = {
  smtp: "Sending",
  imap: "Receiving replies",
  test_message: "Test email",
};

/* ------------------------------------------------------- the experiment (F13, ADR-053)

   Two things on one panel, because they are one question: which arms are running, and what has
   come back from each.

   **"Pending" is a column, not a rounding error.** Most cold email gets no reply, and a lead
   still inside its wait window has not produced a result yet. Counting those as failures would
   make every arm look worse the sooner somebody looked, which is the one way a funnel actively
   misleads. So rates are over settled leads and the screen says so.

   The **effective** share is shown rather than the weight the operator typed, because the
   exploration floor means the two are different numbers and being surprised by that is how
   somebody concludes the allocator is broken. */

function ExperimentPanel({ campaignId, mayAdminister, onError, onToast }) {
  const [state, setState] = useState(null);
  const [funnel, setFunnel] = useState(null);
  const [by, setBy] = useState("variant");
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [arms, setArms] = useState("a\nb");

  const load = useCallback(() => {
    getJson(`/campaigns/${campaignId}/experiment`)
      .then(setState)
      .catch(onError);
    getJson(`/campaigns/${campaignId}/funnel?by=${by}`)
      .then(setFunnel)
      .catch(onError);
  }, [campaignId, by, onError]);

  useEffect(load, [load]);

  const start = (event) => {
    event.preventDefault();
    const parsed = arms
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const [variant, ...rest] = line.split("|");
        return {
          variant: variant.trim(),
          weight: 1,
          instruction: rest.join("|").trim(),
        };
      });
    setBusy(true);
    putJson(`/campaigns/${campaignId}/experiment`, {
      json: { experiment: { name, arms: parsed } },
    })
      .then((body) => {
        onToast(body.note, "success");
        setName("");
        load();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  const stop = () => {
    setBusy(true);
    deleteJson(`/campaigns/${campaignId}/experiment`)
      .then((body) => {
        onToast(body.note, "success");
        load();
      })
      .catch(onError)
      .finally(() => setBusy(false));
  };

  return (
    <>
      <h2 className="section-title">Experiment</h2>
      {!state ? (
        <SkeletonList count={1} rows={3} />
      ) : state.experiment ? (
        <div className="card">
          <strong>{state.experiment.name}</strong>
          <p className="hint">{state.note}</p>
          <ul className="list">
            {state.experiment.arms.map((arm) => (
              <li key={arm.variant} className="list-row">
                <div>
                  <strong>Arm {arm.variant}</strong>{" "}
                  <span className="badge">
                    {Math.round(arm.effective_share * 100)}% of sends
                  </span>
                  <p className="hint">
                    {arm.instruction ||
                      "No style instruction: this arm is the control."}
                  </p>
                </div>
              </li>
            ))}
          </ul>
          <p className="hint">
            {Math.round(state.experiment.exploration_share * 100)}% of
            assignments ignore the weights entirely. That floor is deliberate: a
            weighted allocator with nothing held back can drive an arm to zero,
            and that is exactly when it stops producing the result that would
            change your mind.
          </p>
          {mayAdminister ? (
            <div className="btn-row">
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={stop}
              >
                Stop allocating
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="card">
          <p className="muted">{state.note}</p>
          {mayAdminister ? (
            <form onSubmit={start}>
              <div className="field">
                <label className="form-label">What are you testing?</label>
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </div>
              <div className="field">
                <label className="form-label">Arms</label>
                <textarea
                  rows={4}
                  value={arms}
                  onChange={(event) => setArms(event.target.value)}
                />
                <p className="hint">
                  One per line, as <code>label | how to write this one</code>.
                  The instruction is a style request and cannot widen what may
                  be said: the draft still goes through the same claim check, so
                  an arm asking for a statistic the research does not support
                  fails validation instead of sending one.
                </p>
              </div>
              <button
                type="submit"
                className="btn"
                data-variant="primary"
                disabled={busy || !name.trim()}
              >
                Start the experiment
              </button>
            </form>
          ) : null}
        </div>
      )}

      <h2 className="section-title">Funnel</h2>
      <div className="btn-row">
        {(
          funnel?.dimensions || ["variant", "weekday", "hour", "warmup_day"]
        ).map((name_) => (
          <button
            key={name_}
            type="button"
            className="btn"
            data-variant={by === name_ ? "primary" : undefined}
            onClick={() => setBy(name_)}
          >
            by {name_.replace("_", " ")}
          </button>
        ))}
      </div>
      {!funnel ? (
        <SkeletonList count={1} rows={3} />
      ) : funnel.buckets.length === 0 ? (
        <p className="muted">
          Nothing has been sent yet, so there is nothing to compare.
        </p>
      ) : (
        <>
          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>{by.replace("_", " ")}</th>
                  <th>Sent</th>
                  <th>Settled</th>
                  <th>Pending</th>
                  <th>No reply</th>
                  <th>Encouraging</th>
                  <th>Rate</th>
                </tr>
              </thead>
              <tbody>
                {funnel.buckets.map((bucket) => (
                  <tr key={bucket.key}>
                    <td>{bucket.label}</td>
                    <td>{bucket.sent}</td>
                    <td className="muted">{bucket.settled}</td>
                    {/* Shown as its own column rather than folded into a denominator: a
                        pending lead is one we have not finished watching. */}
                    <td className="muted">{bucket.pending}</td>
                    <td className="muted">{bucket.censored}</td>
                    <td>{bucket.encouraging}</td>
                    <td>
                      {bucket.encouraging_rate === null
                        ? "-"
                        : `${Math.round(bucket.encouraging_rate * 100)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">{funnel.note}</p>
        </>
      )}
    </>
  );
}

export default function CampaignScreen({
  campaign,
  mayAdminister,
  onError,
  onToast,
  onChanged,
}) {
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState(null);
  const [showPersona, setShowPersona] = useState(false);
  const [attaching, setAttaching] = useState(false);
  const [file, setFile] = useState(null);
  const [fileBusy, setFileBusy] = useState(false);
  const [conn, setConn] = useState(null);
  // The last connection check. Cleared whenever the mailbox changes, because a result about a
  // different mailbox sitting under this one is worse than no result at all.
  const [checks, setChecks] = useState(null);
  const [mailboxes, setMailboxes] = useState([]);
  const [schedule, setSchedule] = useState({ starts_at: "", ends_at: "" });
  const [mailbox, setMailbox] = useState({ from_name: "", from_email: "" });

  const [stats, setStats] = useState(null);
  const [activity, setActivity] = useState(null);

  useEffect(() => {
    setDetail(null);
    setStats(null);
    setActivity(null);
    setFile(null);
    setConn(null);
    setSchedule({
      starts_at: toLocalInput(campaign.starts_at),
      ends_at: toLocalInput(campaign.ends_at),
    });
    getJson(`/campaigns/${campaign.id}`).then(setDetail).catch(onError);
    getJson(`/campaigns/${campaign.id}/stats`).then(setStats).catch(onError);
    // `null` means not loaded; `{ attachment: null }` means loaded and there is none. The
    // difference matters: a screen that says "no file attached" before it has asked is a screen
    // stating something it does not know.
    getJson(`/campaigns/${campaign.id}/attachment`)
      .then(setFile)
      .catch(() => setFile({ attachment: null }));
    // Degrades rather than erroring: an older server without this route should leave the panel
    // out, not put a red banner over a screen whose numbers are all fine.
    //
    // **Ask about THIS campaign's mailbox.** Without the id the server falls back to the org's
    // oldest mailbox, so an org with more than one showed, and would have connected, a mailbox
    // the operator was not looking at -- and the callback refuses an account that does not match
    // the mailbox's own address, so the only symptom was a mismatch message naming an address
    // from a different campaign.
    getJson(
      campaign.mailbox_id
        ? `/connect/google/status?mailbox_id=${encodeURIComponent(campaign.mailbox_id)}`
        : "/connect/google/status",
    )
      .then(setConn)
      .catch(() => setConn({ configured: false, connected: false }));
    setChecks(null);
    // Every sending account this workspace holds, so an existing campaign can be switched to one
    // instead of its address being typed again into a second mailbox row.
    getJson("/mailboxes")
      .then((data) => setMailboxes(data.mailboxes))
      .catch(() => setMailboxes([]));
    // A chart is a nicety, so a failure to load one must not put a red banner over a screen
    // whose numbers are all fine. It simply does not draw.
    getJson(`/campaigns/${campaign.id}/activity`)
      .then(setActivity)
      .catch(() => setActivity({ series: [] }));
    // `mailbox_id` is a dependency because the connect panel is about that mailbox: attaching a
    // different one has to re-ask, or the panel describes the mailbox that was there before.
  }, [campaign.id, campaign.mailbox_id, onError]);

  const setStatus = async (status) => {
    setBusy(true);
    try {
      await patchJson(`/campaigns/${campaign.id}`, { json: { status } });
      onToast(
        status === "running"
          ? "Campaign is running. Approved drafts will send inside the window, under the cap."
          : `Campaign is now ${status}. Nothing further will send.`,
      );
      onChanged(false);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const connectGmail = async () => {
    setBusy(true);
    try {
      // The same id the status call asked about, so what gets connected is what was displayed.
      // Sending `{}` connected whichever mailbox happened to be oldest in the org.
      const { authorize_url } = await postJson("/connect/google/start", {
        json: campaign.mailbox_id ? { mailbox_id: campaign.mailbox_id } : {},
      });
      // A full navigation, not a popup: Google blocks its consent screen inside an iframe, and
      // a popup is the thing browsers and extensions are most likely to swallow silently.
      window.location.href = authorize_url;
    } catch (err) {
      onError(err);
      setBusy(false);
    }
  };

  const checkConnection = async (sendTest) => {
    setBusy(true);
    try {
      const report = await postJson("/connect/google/verify", {
        json: {
          ...(campaign.mailbox_id ? { mailbox_id: campaign.mailbox_id } : {}),
          send_test: Boolean(sendTest),
        },
      });
      setChecks(report);
      // The toast says the headline; the per-check detail stays on screen, because "it failed"
      // without saying which half failed is the thing that sent people looking in the wrong
      // place -- a mailbox that sends but cannot be read looks perfectly healthy.
      onToast(
        report.ok
          ? "This mailbox can send and be read."
          : "Something about this connection is not working. See the details below.",
      );
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const useMailbox = async (mailboxId) => {
    setBusy(true);
    try {
      await patchJson(`/campaigns/${campaign.id}`, {
        json: { mailbox_id: mailboxId },
      });
      onToast("This campaign now sends from that account.");
      onChanged(false);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const disconnectGmail = async () => {
    setBusy(true);
    try {
      await deleteJson(`/connect/google/${conn.mailbox_id}`);
      setConn({ ...conn, connected: false, needs_reconnect: false });
      onToast(
        "Disconnected. Revoke access at myaccount.google.com/permissions too.",
      );
      onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const saveSchedule = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      await putJson(`/campaigns/${campaign.id}/schedule`, {
        json: {
          starts_at: schedule.starts_at
            ? new Date(schedule.starts_at).toISOString()
            : null,
          ends_at: schedule.ends_at
            ? new Date(schedule.ends_at).toISOString()
            : null,
        },
      });
      onToast("Schedule saved.");
      onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const chooseFile = async (event) => {
    const chosen = event.target.files?.[0];
    // Cleared so that picking the *same* file again fires a change event — otherwise a failed
    // upload cannot be retried with the file it failed on.
    event.target.value = "";
    if (!chosen) return;
    setFileBusy(true);
    try {
      const result = await uploadFile(
        `/campaigns/${campaign.id}/attachment`,
        chosen,
        { method: "PUT" },
      );
      setFile(result);
      onToast(
        `${result.attachment.filename} will be attached to this campaign's emails.`,
      );
    } catch (err) {
      onError(err);
    } finally {
      setFileBusy(false);
    }
  };

  const removeFile = async () => {
    setFileBusy(true);
    try {
      await deleteJson(`/campaigns/${campaign.id}/attachment`);
      setFile({ attachment: null });
      onToast("Attachment removed. Emails already sent are unchanged.");
    } catch (err) {
      onError(err);
    } finally {
      setFileBusy(false);
    }
  };

  const attachMailbox = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      // The API is idempotent on the address, so posting one that already exists returns it
      // rather than failing. Say which happened: silently reusing a mailbox somebody thought
      // they were creating is the kind of surprise that gets noticed much later.
      const created = await postJson("/mailboxes", { json: mailbox });
      await patchJson(`/campaigns/${campaign.id}`, {
        json: { mailbox_id: created.id },
      });
      onToast(
        created.created
          ? "Mailbox created and attached. Set the campaign running when you are ready."
          : `Attached the existing mailbox for ${created.from_email}.`,
      );
      setAttaching(false);
      onChanged(false);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      const result = await deleteJson(`/campaigns/${campaign.id}`);
      // An archive is not a failure: it is the API refusing to destroy the record of who was
      // contacted, and saying so.
      onToast(result.deleted ? "Campaign deleted." : result.detail);
      onChanged(result.deleted);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const counts = detail?.lead_counts || {};
  const persona = detail?.persona || {};

  return (
    <>
      <PageHeader
        icon={Icons.campaign}
        eyebrow="Outreach"
        title={<>{campaign.name}</>}
      >
        What this campaign is, who it comes from, and whether it is allowed to
        send.
      </PageHeader>

      {/* Every reason an approved draft would sit still, computed by the API from the same
          models the send loop validates with. These used to be two hand-written banners here
          covering the two causes somebody had thought of — so a campaign whose persona the
          worker could not read showed a clean screen, a running status and approved drafts,
          and the reason existed only in the worker log. */}
      {detail?.blockers?.length ? (
        <div className="banner" data-tone="warn">
          <span>
            <strong>Nothing will send from this campaign yet.</strong>
            <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
              {detail.blockers.map((blocker) => (
                <li key={blocker.code}>{blocker.message}</li>
              ))}
            </ul>
            {detail.blockers.some((b) => b.code === "no_mailbox") ? (
              <span className="muted">
                A mailbox must be an address your SPF and DKIM records cover —
                otherwise mail goes out unauthenticated against a domain with no
                matching records, which is worse than no setup at all.
              </span>
            ) : null}
          </span>
        </div>
      ) : null}

      {/* Counted from the record of what happened, not from each lead's current state. A lead
          that replied is still a lead that was emailed — the prototype's count fell as a
          campaign succeeded (issue NEW-3). */}
      {/* **Two groups, not seven equal tiles.** Volume and outcome answer different
          questions, and seven identical boxes made "Meetings booked: 0" look exactly as
          important as "Leads: 2" -- so the number anybody actually cares about was the hardest
          one to find. The outcomes also carry a tone only when they are non-zero: a red
          "Bounced 0" is an alarm about nothing. */}
      <h2 className="section-title" style={{ marginTop: "var(--space-5)" }}>
        Volume
      </h2>
      <dl className="stat-grid">
        <div className="stat">
          <dt>Leads</dt>
          <dd className="num">{stats ? stats.leads_total : "—"}</dd>
        </div>
        <div className="stat">
          <dt>Emails sent</dt>
          <dd className="num">{stats ? stats.emails_sent : "—"}</dd>
        </div>
        <div className="stat">
          <dt>People emailed</dt>
          <dd className="num">{stats ? stats.leads_emailed : "—"}</dd>
        </div>
      </dl>

      {/* The totals are snapshots: "1 reply" says nothing about whether this is working,
          only that it once did. The shape answers what they cannot — is it accelerating, did
          it stall on Tuesday, is the follow-up doing anything at all. */}
      {/* Heading and chart together, or neither. A chart that declines to draw -- no data
          yet, or an older API without the endpoint -- used to leave its title sitting over
          empty space, which reads as something broken rather than something absent. */}
      {!activity || activity.series?.length ? (
        <>
          <h2 className="section-title">Activity</h2>
          <ActivityChart series={activity?.series} loading={!activity} />
        </>
      ) : null}

      <ExperimentPanel
        campaignId={campaign.id}
        mayAdminister={mayAdminister}
        onError={onError}
        onToast={onToast}
      />

      <h2 className="section-title">Outcomes</h2>
      <dl className="stat-grid">
        <div
          className="stat"
          data-tone={stats && stats.replied ? "good" : undefined}
        >
          <dt>Replied</dt>
          <dd className="num">{stats ? stats.replied : "—"}</dd>
        </div>
        <div
          className="stat"
          data-tone={stats && stats.meetings_booked ? "good" : undefined}
        >
          <dt>Meetings booked</dt>
          <dd className="num">{stats ? stats.meetings_booked : "—"}</dd>
        </div>
        <div
          className="stat"
          data-tone={stats && stats.bounced ? "bad" : undefined}
        >
          <dt>Bounced</dt>
          <dd className="num">{stats ? stats.bounced : "—"}</dd>
        </div>
        <div
          className="stat"
          data-tone={stats && stats.opted_out ? "warn" : undefined}
        >
          <dt>Opted out</dt>
          <dd className="num">{stats ? stats.opted_out : "—"}</dd>
        </div>
      </dl>

      <h2 className="section-title">In flight right now</h2>
      <p className="muted">
        A snapshot of where each lead currently is, which is a different
        question from what has happened — a lead counted under “Replied” above
        is no longer in the SENT bucket here.
      </p>
      <div className="day-row" style={{ marginTop: 12 }}>
        {Object.entries(counts).length === 0 ? (
          <span className="muted">No leads yet.</span>
        ) : (
          Object.entries(counts).map(([state, count]) => (
            <span key={state} className="badge" data-tone={stateTone(state)}>
              {state} · {count}
            </span>
          ))
        )}
      </div>

      {mayAdminister ? (
        <div className="btn-row">
          {campaign.mailbox_id ? null : (
            <button
              type="button"
              className="btn"
              onClick={() => setAttaching(!attaching)}
              disabled={busy}
            >
              {attaching ? "Cancel" : "Attach a mailbox"}
            </button>
          )}
          {campaign.mailbox_id && campaign.status !== "running" ? (
            <button
              type="button"
              className="btn"
              data-variant="primary"
              disabled={busy}
              onClick={() => setStatus("running")}
            >
              Set running
            </button>
          ) : null}
          {campaign.status === "running" ? (
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => setStatus("paused")}
            >
              Pause sending
            </button>
          ) : null}
          {/* Pushed away from the routine controls. A destructive action a thumb-width from
              "Pause sending" is a destructive action somebody will eventually hit by accident. */}
          <span className="spacer" />
          <button
            type="button"
            className="btn"
            data-variant="danger"
            disabled={busy}
            onClick={remove}
          >
            Delete campaign
          </button>
        </div>
      ) : null}

      {attaching ? (
        <form
          className="card"
          onSubmit={attachMailbox}
          style={{ marginTop: 14 }}
        >
          <Field
            label="Send from (email)"
            value={mailbox.from_email}
            onChange={(event) =>
              setMailbox({ ...mailbox, from_email: event.target.value })
            }
            required
            hint="The address you will connect a Google account for. It is what recipients see, and what your SPF and DKIM records must cover."
          />
          <Field
            label="Sender name recipients see"
            value={mailbox.from_name}
            onChange={(event) =>
              setMailbox({ ...mailbox, from_name: event.target.value })
            }
            required
          />
          <div className="btn-row">
            <button
              type="submit"
              className="btn"
              data-variant="primary"
              disabled={busy}
            >
              {busy ? "Attaching…" : "Attach mailbox"}
            </button>
          </div>
        </form>
      ) : null}

      <h2 className="section-title">Sending account</h2>
      <p className="muted">
        The Gmail account this campaign sends from. Connecting it means
        approving us on Google's own screen — no password is typed here, and you
        can revoke access at any time from your Google account.
      </p>
      {conn === null ? (
        <SkeletonList count={1} rows={2} />
      ) : !conn.configured ? (
        <p className="muted" style={{ marginTop: 12 }}>
          This server has no Google credentials configured, so accounts cannot
          be connected yet. It falls back to the mailbox set in the server
          environment.
        </p>
      ) : (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="card-head">
            <Icons.inbox size={16} />
            <strong>
              {conn.account_email || conn.from_email || "No mailbox yet"}
            </strong>
            {conn.connected ? (
              <span className="badge" data-tone="ok">
                connected
              </span>
            ) : conn.needs_reconnect ? (
              <span className="badge" data-tone="warn">
                needs reconnecting
              </span>
            ) : (
              <span className="badge">not connected</span>
            )}
            <span className="spacer" />
            {mayAdminister ? (
              <>
                <button
                  type="button"
                  className="btn"
                  data-size="sm"
                  data-variant={conn.connected ? undefined : "primary"}
                  disabled={busy}
                  onClick={connectGmail}
                >
                  {conn.connected ? "Reconnect" : "Connect Gmail"}
                </button>
                {conn.connected || conn.needs_reconnect ? (
                  <button
                    type="button"
                    className="btn"
                    data-size="sm"
                    data-variant="danger"
                    disabled={busy}
                    onClick={disconnectGmail}
                  >
                    Disconnect
                  </button>
                ) : null}
              </>
            ) : null}
          </div>
          {conn.needs_reconnect ? (
            <p className="muted" style={{ marginTop: 10 }}>
              {conn.reason} Google expires these connections about once a week
              while this app is unverified, so reconnecting is expected rather
              than a sign something is broken.
            </p>
          ) : null}
          {!conn.needs_reconnect && conn.expiry_notice ? (
            <p className="muted" data-tone="warn" style={{ marginTop: 10 }}>
              {conn.expiry_notice}
            </p>
          ) : null}
          {conn.connected && mayAdminister ? (
            <div
              style={{
                marginTop: 10,
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
              }}
            >
              <button
                type="button"
                className="btn"
                data-size="sm"
                disabled={busy}
                onClick={() => checkConnection(false)}
              >
                Check connection
              </button>
              <button
                type="button"
                className="btn"
                data-size="sm"
                disabled={busy}
                onClick={() => checkConnection(true)}
              >
                Send myself a test email
              </button>
            </div>
          ) : null}
          {mayAdminister && mailboxes.length > 1 ? (
            <label className="field" style={{ marginTop: 12 }}>
              <span>Send this campaign from</span>
              <select
                value={campaign.mailbox_id || ""}
                disabled={busy}
                onChange={(event) => useMailbox(event.target.value)}
              >
                {mailboxes.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.from_email}
                    {row.usable ? "" : " — not connected yet"}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {checks ? (
            <ul className="muted" style={{ marginTop: 10, paddingLeft: 18 }}>
              {checks.checks.map((check) => (
                <li key={check.name}>
                  <strong>{CHECK_LABELS[check.name] || check.name}:</strong>{" "}
                  {check.ok ? "ok" : "failed"} — {check.detail}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      )}

      <h2 className="section-title">Schedule</h2>
      <p className="muted">
        When this campaign may run at all, as whole dates. The daily send window
        inside those days is in Settings. Leave both empty to start and stop it
        by hand.
        {campaign.schedule_note ? ` ${campaign.schedule_note}` : ""}
      </p>
      {mayAdminister ? (
        <form
          className="card"
          onSubmit={saveSchedule}
          style={{ marginTop: 12 }}
        >
          <div className="field-row">
            <Field
              label="Starts"
              type="datetime-local"
              value={schedule.starts_at}
              onChange={(event) =>
                setSchedule({ ...schedule, starts_at: event.target.value })
              }
            />
            <Field
              label="Ends"
              type="datetime-local"
              value={schedule.ends_at}
              onChange={(event) =>
                setSchedule({ ...schedule, ends_at: event.target.value })
              }
            />
          </div>
          <div className="btn-row">
            <button
              type="submit"
              className="btn"
              data-variant="primary"
              disabled={busy}
            >
              {busy ? "Saving…" : "Save schedule"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => setSchedule({ starts_at: "", ends_at: "" })}
            >
              Clear
            </button>
          </div>
        </form>
      ) : null}

      <h2 className="section-title">Attachment</h2>
      <p className="muted">
        One file, carried by every first email this campaign sends. A reviewer
        can untick it for an individual email in the review queue, and
        follow-ups leave it off unless Settings says otherwise — the same
        document twice in one thread is noise.
      </p>
      {file === null ? (
        <SkeletonList count={1} rows={2} />
      ) : file.attachment ? (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="card-head">
            <Icons.file size={16} />
            <strong>{file.attachment.filename}</strong>
            <span className="badge">
              {formatBytes(file.attachment.size_bytes)}
            </span>
            <span className="spacer" />
            {mayAdminister ? (
              <button
                type="button"
                className="btn"
                data-size="sm"
                data-variant="danger"
                disabled={fileBusy}
                onClick={removeFile}
              >
                Remove
              </button>
            ) : null}
          </div>
          <p className="muted" style={{ marginTop: 10 }}>
            Uploaded by {file.attachment.uploaded_by || "somebody"} on{" "}
            {new Date(file.attachment.created_at).toLocaleDateString()}.
            Replacing it changes every email not yet sent; the ones already gone
            keep what they carried.
          </p>
        </div>
      ) : (
        <p className="muted" style={{ marginTop: 12 }}>
          No file attached. Emails go as plain text on their own.
        </p>
      )}
      {mayAdminister ? (
        <div className="btn-row">
          <label
            className="btn"
            data-variant={file?.attachment ? undefined : "primary"}
          >
            {fileBusy
              ? "Uploading…"
              : file?.attachment
                ? "Replace file"
                : "Attach a file"}
            {/* A real file input inside a label: clicking the label opens the picker and the
                keyboard still reaches the control. A styled div with an onClick does neither. */}
            <input
              type="file"
              hidden
              disabled={fileBusy}
              onChange={chooseFile}
              accept=".pdf,.docx,.pptx,.xlsx,.csv,.txt,.png,.jpg,.jpeg,.webp"
            />
          </label>
        </div>
      ) : null}
      <p className="muted" style={{ marginTop: 8 }}>
        Up to 5 MB. Documents and images only — executables, macro-enabled
        Office files and archives are refused, because nothing here inspects
        what is inside a file.{" "}
        <strong>
          An attachment makes a cold email more likely to be filtered as spam
        </strong>
        , so a link to the document is usually the better opening.
      </p>

      <h2 className="section-title">Sender persona</h2>
      <p className="muted">
        The only things a draft is allowed to claim about us. Everything else
        has to come from cited research.
      </p>
      <div className="btn-row">
        <button
          type="button"
          className="btn"
          onClick={() => setShowPersona(!showPersona)}
        >
          {showPersona
            ? "Hide persona"
            : "Show what this campaign was created with"}
        </button>
      </div>

      {showPersona ? (
        <div className="card" style={{ marginTop: 14 }}>
          {Object.keys(persona).length === 0 ? (
            <p className="muted">No persona stored.</p>
          ) : (
            <table className="kv">
              <tbody>
                {Object.entries(persona).map(([key, value]) => (
                  <tr key={key}>
                    <td>{key.replace(/_/g, " ")}</td>
                    <td>
                      {value === null || value === "" ? (
                        <span className="muted">not set</span>
                      ) : Array.isArray(value) ? (
                        value.length === 0 ? (
                          <span className="muted">none</span>
                        ) : (
                          <ul style={{ margin: 0, paddingLeft: 18 }}>
                            {value.map((entry, index) => (
                              <li key={index}>
                                {typeof entry === "object"
                                  ? JSON.stringify(entry)
                                  : String(entry)}
                              </li>
                            ))}
                          </ul>
                        )
                      ) : (
                        String(value)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}
    </>
  );
}
