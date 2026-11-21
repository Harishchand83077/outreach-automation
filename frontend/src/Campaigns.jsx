// Campaigns table — one row per campaign with aggregate stats (S18.2, ADR-064).
//
// Counts come from the backend's grouped queries (not N+1 over /stats) and follow
// the same semantics as GET /campaigns/{id}/stats: messages for sends, events.to_state
// for "ever reached".  No opened or clicked columns — locked product rule.

import { useCallback, useEffect, useState } from "react";

import { getJson } from "./api";
import { navigate } from "./router";
import { Empty, Icons, PageHeader, SkeletonList, statusTone } from "./ui";

const STATUS_TABS = [
  { id: null, label: "All" },
  { id: "running", label: "Running" },
  { id: "paused", label: "Paused" },
  { id: "draft", label: "Draft" },
];

function pct(num, den) {
  if (!den) return "—";
  return Math.round((num / den) * 100) + "%";
}

function StatCell({ value, note }) {
  return (
    <td style={{ textAlign: "right", padding: "10px 12px", fontSize: 14 }}>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{value ?? "—"}</span>
      {note ? (
        <span style={{ color: "var(--text-muted)", fontSize: 12, marginLeft: 4 }}>
          {note}
        </span>
      ) : null}
    </td>
  );
}

export default function Campaigns({ onError, onToast }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState(null);

  const load = useCallback(() => {
    setLoading(true);
    getJson("/campaigns/overview")
      .then(setData)
      .catch(onError)
      .finally(() => setLoading(false));
  }, [onError]);

  useEffect(load, [load]);

  const rows = (data?.campaigns ?? []).filter(
    (c) => tab === null || c.status === tab
  );

  return (
    <div>
      <PageHeader title="Campaigns">
        All campaigns in this workspace, with outreach stats counted from the record.
      </PageHeader>

      {/* Status tabs */}
      <div
        style={{ display: "flex", gap: 4, flexWrap: "wrap", marginBottom: 16 }}
        role="tablist"
      >
        {STATUS_TABS.map((t) => (
          <button
            key={t.id ?? "all"}
            role="tab"
            aria-selected={tab === t.id}
            className="btn"
            data-variant={tab === t.id ? "brand" : undefined}
            data-size="sm"
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {loading ? (
        <SkeletonList count={4} rows={1} />
      ) : rows.length === 0 ? (
        <Empty
          title="No campaigns"
          note="Create a campaign to start researching and sending."
        />
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table
            style={{
              width: "100%",
              borderCollapse: "collapse",
              fontSize: 14,
            }}
          >
            <thead>
              <tr style={{ borderBottom: "2px solid var(--border)" }}>
                <th style={{ textAlign: "left", padding: "8px 12px", fontWeight: 600 }}>
                  Campaign
                </th>
                <th style={{ textAlign: "right", padding: "8px 12px", fontWeight: 600 }}>
                  Leads
                </th>
                <th style={{ textAlign: "right", padding: "8px 12px", fontWeight: 600 }}>
                  Sent
                </th>
                <th style={{ textAlign: "right", padding: "8px 12px", fontWeight: 600 }}>
                  Replied
                </th>
                <th style={{ textAlign: "right", padding: "8px 12px", fontWeight: 600 }}>
                  Meetings
                </th>
                <th style={{ textAlign: "right", padding: "8px 12px", fontWeight: 600 }}>
                  Bounced
                </th>
                <th style={{ textAlign: "right", padding: "8px 12px", fontWeight: 600 }}>
                  Opted out
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr
                  key={c.id}
                  style={{
                    borderBottom: "1px solid var(--border)",
                    cursor: "pointer",
                  }}
                  onClick={() => navigate(`/campaigns/${c.id}`)}
                >
                  <td style={{ padding: "10px 12px" }}>
                    <div style={{ fontWeight: 600 }}>{c.name}</div>
                    <div style={{ marginTop: 2 }}>
                      <span
                        className="badge"
                        data-tone={statusTone(c)}
                        style={{ fontSize: 11 }}
                      >
                        {c.status}
                      </span>
                    </div>
                  </td>
                  <StatCell value={c.leads_total} />
                  <StatCell
                    value={c.emails_sent}
                    note={c.leads_total ? `(${pct(c.emails_sent, c.leads_total)})` : undefined}
                  />
                  <StatCell
                    value={c.replied}
                    note={c.emails_sent ? `(${pct(c.replied, c.emails_sent)})` : undefined}
                  />
                  <StatCell value={c.meetings_booked} />
                  <StatCell value={c.bounced} />
                  <StatCell value={c.opted_out} />
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div style={{ marginTop: 20 }}>
        <button
          className="btn"
          data-variant="brand"
          onClick={() => navigate("/campaigns/new")}
        >
          <Icons.plus size={15} style={{ marginRight: 6 }} />
          New campaign
        </button>
      </div>
    </div>
  );
}
