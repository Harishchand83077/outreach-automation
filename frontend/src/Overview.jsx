// The page the product opens on (S17.1).
//
// **It answers two questions and deliberately not more**: what needs me now, and is this
// working. Everything else has its own screen, and an overview that tried to be all of them
// would be the old dashboard again -- one long scroll of every section at once.
//
// **"Needs you now" is the half with the most thought in it.** This project has twice shipped a
// screen that stated something false while it waited: `campaigns` starting as `[]` said "No
// campaign yet" to somebody with two, and a lead sat `APPROVED` for eight days showing
// "next action: —" while the send loop knew the reason the whole time. So:
//
// - Nothing here says a number until the request that produces it has come back. `loaded` means
//   *we asked and heard*, including hearing an error -- not `data !== null`.
// - A row appears only when its count is non-zero, except the all-clear, which appears only when
//   every count is zero and we have heard from all of them. "Nothing needs you" is a claim, and
//   it needs the same evidence as "three things need you".
//
// **The funnel is a shape rather than seven numbers.** "1 reply" says nothing about whether a
// campaign is working; the step from 40 sent to 3 replied says what the problem is. Counted from
// `/stats`, which counts from the record -- `emails_sent` is messages and `replied` is leads that
// *ever reached* that state, so a lead that replied and then booked still counts in both.

import { useCallback, useEffect, useMemo, useState } from "react";

import { getJson } from "./api";
import { Link } from "./router";
import { ActivityChart, Icons, SkeletonList } from "./ui";

/** One load, with "we asked and heard back" as a separate fact from "we have data". */
function useAsked(path, onError) {
  const [data, setData] = useState(null);
  const [asked, setAsked] = useState(false);

  const load = useCallback(() => {
    let live = true;
    getJson(path)
      .then((body) => {
        if (live) setData(body);
      })
      .catch((err) => {
        onError?.(err);
      })
      .finally(() => {
        if (live) setAsked(true);
      });
    return () => {
      live = false;
    };
  }, [path, onError]);

  useEffect(load, [load]);
  return { data, asked, reload: load };
}

function Metric({ label, value, note, tone, featured, icon: IconComponent }) {
  return (
    <div
      className="metric"
      data-tone={tone}
      data-featured={featured ? "true" : undefined}
    >
      <span className="metric-label">
        {IconComponent ? <IconComponent size={15} /> : null}
        {label}
      </span>
      <span className="metric-value">{value}</span>
      {note ? <p className="metric-note">{note}</p> : null}
    </div>
  );
}

/** One stage of the pipeline, as a bar against the widest stage. */
function FunnelRow({ label, value, of, terminal }) {
  const share = of > 0 ? Math.max(value / of, value > 0 ? 0.02 : 0) : 0;
  return (
    <div className="funnel-row" data-terminal={terminal ? "true" : undefined}>
      <span className="funnel-label">{label}</span>
      <div className="funnel-track">
        <div
          className="funnel-fill"
          style={{ width: `${Math.round(share * 100)}%` }}
        />
      </div>
      <span className="funnel-value">{value}</span>
    </div>
  );
}

export default function Overview({ onError, mayAdminister }) {
  const campaigns = useAsked("/campaigns", onError);
  const queue = useAsked("/review/drafts?limit=1", onError);
  const signals = useAsked("/signals?state=new", onError);
  const mailboxes = useAsked("/mailboxes", onError);

  const list = campaigns.data?.campaigns || [];
  // The campaign a demo should be looking at: the one that is running, or the first one. Chosen
  // rather than asked for, because an overview that opens on a picker has made the operator
  // answer a question before it has told them anything.
  const focus = useMemo(
    () => list.find((one) => one.status === "running") || list[0] || null,
    [list],
  );

  const [stats, setStats] = useState(null);
  const [activity, setActivity] = useState(null);

  useEffect(() => {
    setStats(null);
    setActivity(null);
    if (!focus) return;
    getJson(`/campaigns/${focus.id}/stats`).then(setStats).catch(onError);
    // An older API without this endpoint draws no chart rather than an error: the heading and
    // the chart appear together or neither does, which is the fix from ADR-048's addendum.
    getJson(`/campaigns/${focus.id}/activity`)
      .then(setActivity)
      .catch(() => setActivity({ series: [] }));
  }, [focus, onError]);

  const needsReconnect = (mailboxes.data?.mailboxes || []).filter(
    (one) => one.needs_reconnect,
  );
  const withoutALead = signals.data?.without_a_lead || 0;
  const waiting = queue.data?.total || 0;

  const allAsked =
    campaigns.asked && queue.asked && signals.asked && mailboxes.asked;
  const nothingToDo =
    allAsked &&
    waiting === 0 &&
    needsReconnect.length === 0 &&
    withoutALead === 0;

  return (
    <>
      <div className="page-head">
        <div className="page-head-text">
          <div className="eyebrow">
            <Icons.home size={14} />
            Overview
          </div>
          <h1 className="page-title">
            {focus ? focus.name : "Your workspace"}
          </h1>
          <p className="page-sub">
            What needs you now, and whether the sending is working. Every number
            here is counted from what happened rather than from where leads are
            standing — a lead that replied and then booked a meeting counts in
            both.
          </p>
        </div>
        <div className="page-actions">
          <Link className="btn" to="/leads">
            Leads
          </Link>
          <Link className="btn" data-variant="brand" to="/review">
            Review queue
            {waiting > 0 ? ` (${waiting})` : ""}
          </Link>
        </div>
      </div>

      <div className="sections">
        {/* ---------------------------------------------------------- needs you now */}
        <section>
          <div className="section-head">
            <div>
              <h2>Needs you now</h2>
              <p>
                Only the things that are actually waiting. Nothing appears here
                until the request behind it has come back, because a screen that
                states a number while it is still loading has twice been wrong
                in this app.
              </p>
            </div>
          </div>

          {!allAsked ? (
            <SkeletonList count={2} rows={1} />
          ) : nothingToDo ? (
            <div className="card" data-emphasis="brand">
              <div
                style={{
                  display: "flex",
                  gap: "var(--space-3)",
                  alignItems: "center",
                }}
              >
                <Icons.check size={20} />
                <div>
                  <strong>Nothing is waiting on you.</strong>
                  <p className="hint" style={{ margin: "2px 0 0" }}>
                    No drafts to review, every sending account connected, and
                    every signal has somebody to write to.
                  </p>
                </div>
              </div>
            </div>
          ) : (
            <div className="card-grid">
              {waiting > 0 ? (
                <Link className="card row-link" to="/review">
                  <span className="pill" data-tone="brand">
                    <span className="pill-dot" />
                    {waiting} waiting
                  </span>
                  <h3 style={{ margin: "var(--space-3) 0 var(--space-1)" }}>
                    Drafts to review
                  </h3>
                  <p className="hint" style={{ margin: 0 }}>
                    Nothing sends until you approve it. J and K move, A
                    approves, R rejects.
                  </p>
                </Link>
              ) : null}

              {needsReconnect.length > 0 ? (
                <Link className="card row-link" to="/sending">
                  <span className="pill" data-tone="danger">
                    <span className="pill-dot" />
                    {needsReconnect.length} to reconnect
                  </span>
                  <h3 style={{ margin: "var(--space-3) 0 var(--space-1)" }}>
                    Sending accounts
                  </h3>
                  <p className="hint" style={{ margin: 0 }}>
                    {/* The failure this exists for: a lapsed grant stops sending *and* stops
                        replies arriving, and from inside the workspace everything looks
                        normal. */}
                    A connection that has lapsed cannot send and cannot fetch
                    replies.
                  </p>
                </Link>
              ) : null}

              {withoutALead > 0 ? (
                <Link className="card row-link" to="/signals">
                  <span className="pill" data-tone="accent">
                    <span className="pill-dot" />
                    {withoutALead} with nobody to write to
                  </span>
                  <h3 style={{ margin: "var(--space-3) 0 var(--space-1)" }}>
                    Signals
                  </h3>
                  <p className="hint" style={{ margin: 0 }}>
                    A company worth approaching and somebody to approach are
                    different things.
                  </p>
                </Link>
              ) : null}
            </div>
          )}
        </section>

        {/* ---------------------------------------------------------- the funnel */}
        {focus ? (
          <section>
            <div className="section-head">
              <div>
                <h2>The pipeline</h2>
                <p>
                  {focus.name} — counted from the record, so a reply that
                  happened in March still counts today.
                </p>
              </div>
              <Link className="btn" to={`/campaigns/${focus.id}`}>
                Open the campaign
              </Link>
            </div>

            {!stats ? (
              <SkeletonList count={1} rows={4} />
            ) : (
              <>
                <div
                  className="metric-row"
                  style={{ marginBottom: "var(--space-5)" }}
                >
                  <Metric
                    label="Emails sent"
                    value={stats.emails_sent}
                    note="Counts messages, so two sends to one person are two."
                    featured
                    icon={Icons.send}
                  />
                  <Metric
                    label="Replied"
                    value={stats.replied}
                    tone={stats.replied > 0 ? "success" : undefined}
                    note="Leads that ever reached a reply."
                  />
                  <Metric
                    label="Meetings booked"
                    value={stats.meetings_booked}
                    tone={stats.meetings_booked > 0 ? "success" : undefined}
                    note="Only a human click or a booking webhook sets this."
                  />
                  <Metric
                    label="Bounced"
                    value={stats.bounced}
                    tone={stats.bounced > 0 ? "danger" : undefined}
                    note="Above 5% over 20 sends, the mailbox pauses itself."
                  />
                </div>

                <div className="card">
                  <div className="funnel">
                    <FunnelRow
                      label="Leads"
                      value={stats.leads_total}
                      of={stats.leads_total}
                    />
                    <FunnelRow
                      label="Emailed"
                      value={stats.leads_emailed}
                      of={stats.leads_total}
                    />
                    <FunnelRow
                      label="Replied"
                      value={stats.replied}
                      of={stats.leads_total}
                    />
                    <FunnelRow
                      label="Meetings"
                      value={stats.meetings_booked}
                      of={stats.leads_total}
                      terminal
                    />
                  </div>
                  {stats.leads_total === 0 ? (
                    <p className="hint" style={{ marginTop: "var(--space-4)" }}>
                      No leads imported yet.{" "}
                      <Link to="/import">Import a sheet</Link> and the pipeline
                      fills in from the left.
                    </p>
                  ) : null}
                </div>
              </>
            )}
          </section>
        ) : null}

        {/* ---------------------------------------------------------- activity */}
        {focus && (!activity || activity.series?.length) ? (
          <section>
            <div className="section-head">
              <div>
                <h2>Activity</h2>
                <p>
                  Sends and replies per day, in the campaign's own timezone — a
                  send at 23:40 IST belongs to that day and not to the previous
                  one in UTC.
                </p>
              </div>
            </div>
            <div className="card">
              <ActivityChart series={activity?.series} loading={!activity} />
            </div>
          </section>
        ) : null}

        {/* ---------------------------------------------------------- no campaign yet */}
        {campaigns.asked && list.length === 0 ? (
          <section>
            <div className="card" data-emphasis="brand">
              <h2 style={{ marginTop: 0 }}>Start with a campaign</h2>
              <p style={{ color: "var(--text-muted)" }}>
                A campaign carries who the email is from and what it asks for.
                Everything else — importing, research, drafting, sending —
                happens inside one.
              </p>
              <div className="btn-row">
                <Link
                  className="btn"
                  data-variant="brand"
                  data-size="lg"
                  to="/campaigns/new"
                >
                  Create a campaign
                </Link>
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </>
  );
}
