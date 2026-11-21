import { useState } from "react";
import { Empty, PageHeader, Skeleton, useLoad } from "./ui";
import { getJson, postJson } from "./api";

const STATUSES = [
  "saved",
  "preparing",
  "ready",
  "applied",
  "in_conversation",
  "interviewing",
  "offer",
  "closed",
];

const STATUS_LABEL = {
  saved: "Saved",
  preparing: "Preparing",
  ready: "Ready",
  applied: "Applied",
  in_conversation: "In conversation",
  interviewing: "Interviewing",
  offer: "Offer",
  closed: "Closed",
};

function statusTone(s) {
  if (s === "closed") return "var(--text-muted)";
  if (s === "offer") return "var(--brand)";
  if (s === "interviewing" || s === "in_conversation") return "#2ea043";
  return "inherit";
}

function OpportunityCard({ opp, onClick }) {
  return (
    <div
      className="card"
      onClick={() => onClick(opp)}
      style={{ cursor: "pointer", marginBottom: 8 }}
    >
      <div style={{ fontWeight: 600, fontSize: 14 }}>{opp.title}</div>
      {opp.company_domain && (
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{opp.company_domain}</div>
      )}
      <div style={{ fontSize: 12, marginTop: 4, color: statusTone(opp.status) }}>
        {STATUS_LABEL[opp.status] ?? opp.status}
      </div>
    </div>
  );
}

function PipelineBoard({ opps, onMove, onSelect }) {
  const cols = STATUSES.filter((s) => s !== "closed");
  return (
    <div style={{ display: "flex", gap: 12, overflowX: "auto", paddingBottom: 8 }}>
      {cols.map((s) => (
        <div key={s} style={{ minWidth: 180, flex: "0 0 180px" }}>
          <div style={{ fontWeight: 600, fontSize: 12, marginBottom: 8, color: "var(--text-muted)" }}>
            {STATUS_LABEL[s]} ({opps.filter((o) => o.status === s).length})
          </div>
          {opps
            .filter((o) => o.status === s)
            .map((o) => (
              <OpportunityCard key={o.id} opp={o} onClick={onSelect} />
            ))}
        </div>
      ))}
    </div>
  );
}

function OppDetail({ opp, onClose, onRefresh }) {
  const [events, evtLoad] = useLoad(() => getJson(`/opportunities/${opp.id}/events`), [opp.id]);
  const [tasks, taskLoad] = useLoad(() => getJson(`/opportunities/${opp.id}/tasks`), [opp.id]);
  const [newTask, setNewTask] = useState("");
  const [moving, setMoving] = useState(false);
  const [closingReason, setClosingReason] = useState("");

  async function addTask(e) {
    e.preventDefault();
    if (!newTask.trim()) return;
    await postJson(`/opportunities/${opp.id}/tasks`, { json: { title: newTask } });
    setNewTask("");
    taskLoad.reload();
  }

  async function move(toStatus) {
    setMoving(true);
    try {
      const body = { to_status: toStatus };
      if (toStatus === "closed") body.closed_reason = closingReason || "withdrawn";
      await postJson(`/opportunities/${opp.id}/status`, { json: body });
      onRefresh();
      onClose();
    } finally {
      setMoving(false);
    }
  }

  return (
    <div style={{ padding: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>{opp.title}</div>
          {opp.company_domain && (
            <div style={{ fontSize: 13, color: "var(--text-muted)" }}>{opp.company_domain}</div>
          )}
          <div style={{ fontSize: 12, marginTop: 2, color: statusTone(opp.status) }}>
            {STATUS_LABEL[opp.status] ?? opp.status}
          </div>
        </div>
        <button className="btn btn-ghost" onClick={onClose}>Close</button>
      </div>

      <section style={{ marginBottom: 16 }}>
        <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Move to</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {STATUSES.filter((s) => s !== opp.status && s !== "closed").map((s) => (
            <button
              key={s}
              className="btn btn-secondary"
              style={{ fontSize: 12 }}
              disabled={moving}
              onClick={() => move(s)}
            >
              {STATUS_LABEL[s]}
            </button>
          ))}
          {opp.status !== "closed" && (
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <select
                value={closingReason}
                onChange={(e) => setClosingReason(e.target.value)}
                style={{ fontSize: 12 }}
              >
                <option value="">Reason…</option>
                {["rejected","withdrawn","job_closed","skipped","no_response","offer_accepted","offer_declined"].map((r) => (
                  <option key={r} value={r}>{r.replace(/_/g, " ")}</option>
                ))}
              </select>
              <button
                className="btn btn-secondary"
                style={{ fontSize: 12 }}
                disabled={moving || !closingReason}
                onClick={() => move("closed")}
              >
                Close
              </button>
            </div>
          )}
        </div>
      </section>

      <section style={{ marginBottom: 16 }}>
        <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Tasks</div>
        {taskLoad.loading ? <Skeleton rows={2} /> : (
          <>
            {(tasks?.tasks ?? []).map((t) => (
              <div key={t.id} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 4 }}>
                <input
                  type="checkbox"
                  checked={t.status === "done"}
                  onChange={async () => {
                    await postJson(`/tasks/${t.id}`, {
                      json: { task_status: t.status === "done" ? "pending" : "done" },
                    });
                    taskLoad.reload();
                  }}
                />
                <span style={{ fontSize: 13, textDecoration: t.status === "done" ? "line-through" : "none" }}>
                  {t.title}
                </span>
              </div>
            ))}
            <form onSubmit={addTask} style={{ display: "flex", gap: 6, marginTop: 6 }}>
              <input
                className="input"
                style={{ flex: 1, fontSize: 13 }}
                placeholder="Add a task…"
                value={newTask}
                onChange={(e) => setNewTask(e.target.value)}
              />
              <button className="btn btn-secondary" type="submit" style={{ fontSize: 12 }}>Add</button>
            </form>
          </>
        )}
      </section>

      <section>
        <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Timeline</div>
        {evtLoad.loading ? <Skeleton rows={3} /> : (
          (events?.events ?? []).length === 0
            ? <div style={{ fontSize: 13, color: "var(--text-muted)" }}>No events yet.</div>
            : (events?.events ?? []).map((ev) => (
                <div key={ev.id} style={{ fontSize: 12, marginBottom: 6, color: "var(--text-muted)" }}>
                  <span style={{ color: "inherit", marginRight: 6 }}>
                    {new Date(ev.occurred_at).toLocaleDateString()}
                  </span>
                  {ev.event_kind.replace("opportunity.", "")}
                  {ev.from_status && ev.to_status && (
                    <span> · {STATUS_LABEL[ev.from_status]} → {STATUS_LABEL[ev.to_status]}</span>
                  )}
                </div>
              ))
        )}
      </section>
    </div>
  );
}

export default function Opportunities() {
  const [opps, load] = useLoad(() => getJson("/opportunities"));
  const [view, setView] = useState("board"); // board | list
  const [selected, setSelected] = useState(null);

  function refresh() {
    load.reload();
    setSelected(null);
  }

  const list = opps?.opportunities ?? [];

  return (
    <div style={{ padding: "0 16px" }}>
      <PageHeader title="Opportunities">
        <div style={{ display: "flex", gap: 8 }}>
          <button
            className={`btn ${view === "board" ? "btn-primary" : "btn-secondary"}`}
            style={{ fontSize: 12 }}
            onClick={() => setView("board")}
          >
            Board
          </button>
          <button
            className={`btn ${view === "list" ? "btn-primary" : "btn-secondary"}`}
            style={{ fontSize: 12 }}
            onClick={() => setView("list")}
          >
            List
          </button>
        </div>
      </PageHeader>

      {load.loading ? (
        <Skeleton rows={4} />
      ) : selected ? (
        <OppDetail
          opp={selected}
          onClose={() => setSelected(null)}
          onRefresh={refresh}
        />
      ) : list.length === 0 ? (
        <Empty message="No opportunities yet. Save a job post to get started." />
      ) : view === "board" ? (
        <PipelineBoard opps={list} onSelect={setSelected} />
      ) : (
        <div>
          {list.map((o) => (
            <OpportunityCard key={o.id} opp={o} onClick={setSelected} />
          ))}
        </div>
      )}
    </div>
  );
}
