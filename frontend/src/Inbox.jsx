// Inbox — one unified view of inbound threads across all campaigns (S18.1, ADR-063).
//
// A thread is a lead that has received at least one inbound message.  Every thread belongs
// to exactly one folder; the folder is computed by the backend from the lead state, the
// latest classification label, any pending draft, and the snooze/archive flags.
//
// Approval actions go through the existing review routes — this screen surfaces the draft
// and links to the approve/reject flow; it does not duplicate that logic.

import { useCallback, useEffect, useState } from "react";

import { getJson, postJson } from "./api";
import { navigate } from "./router";
import { Empty, Icons, PageHeader, SkeletonList } from "./ui";

const FOLDERS = [
  { id: null, label: "All" },
  { id: "needs_reply", label: "Needs reply" },
  { id: "drafts_waiting", label: "Drafts waiting" },
  { id: "interested", label: "Interested" },
  { id: "meeting_booked", label: "Meeting booked" },
  { id: "out_of_office", label: "Out of office" },
  { id: "wrong_person", label: "Wrong person" },
  { id: "not_interested", label: "Not interested" },
  { id: "snoozed", label: "Snoozed" },
  { id: "archived", label: "Archived" },
];

const LABEL_BADGE = {
  needs_reply: { text: "Needs reply", tone: "warn" },
  drafts_waiting: { text: "Draft waiting", tone: "warn" },
  interested: { text: "Interested", tone: "positive" },
  meeting_booked: { text: "Meeting booked", tone: "positive" },
  out_of_office: { text: "Out of office", tone: "neutral" },
  wrong_person: { text: "Wrong person", tone: "neutral" },
  not_interested: { text: "Not interested", tone: "muted" },
  snoozed: { text: "Snoozed", tone: "muted" },
  archived: { text: "Archived", tone: "muted" },
};

function FolderBadge({ folder }) {
  const badge = LABEL_BADGE[folder] ?? { text: folder, tone: "neutral" };
  return (
    <span className="badge" data-tone={badge.tone}>
      {badge.text}
    </span>
  );
}

function ThreadRow({ thread, onClick }) {
  return (
    <button
      className="thread-row"
      onClick={onClick}
      style={{
        display: "grid",
        gridTemplateColumns: "1fr auto",
        alignItems: "start",
        gap: "0 12px",
        padding: "12px 16px",
        border: "none",
        borderBottom: "1px solid var(--border)",
        background: "transparent",
        textAlign: "left",
        cursor: "pointer",
        width: "100%",
      }}
    >
      <div>
        <div style={{ fontWeight: 600 }}>
          {thread.lead_name || thread.email}
        </div>
        <div style={{ fontSize: 13, color: "var(--text-muted)", marginTop: 2 }}>
          {thread.company && <span>{thread.company} · </span>}
          {thread.email}
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 }}>
        <FolderBadge folder={thread.folder} />
        {thread.last_activity_at && (
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {new Date(thread.last_activity_at).toLocaleDateString()}
          </span>
        )}
      </div>
    </button>
  );
}

function ThreadDetail({ leadId, onBack, onError, onToast }) {
  const [thread, setThread] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    getJson(`/inbox/threads/${leadId}`)
      .then(setThread)
      .catch(onError)
      .finally(() => setLoading(false));
  }, [leadId, onError]);

  const handleSnooze = () => {
    // Snooze until tomorrow
    const until = new Date();
    until.setDate(until.getDate() + 1);
    until.setHours(9, 0, 0, 0);
    postJson(`/inbox/threads/${leadId}/snooze`, { json: { until: until.toISOString() } })
      .then(() => {
        onToast?.("Thread snoozed until tomorrow");
        onBack();
      })
      .catch(onError);
  };

  const handleArchive = () => {
    postJson(`/inbox/threads/${leadId}/archive`, { json: {} })
      .then(() => {
        onToast?.("Thread archived");
        onBack();
      })
      .catch(onError);
  };

  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <SkeletonList count={4} rows={2} />
      </div>
    );
  }

  if (!thread) return null;

  return (
    <div style={{ padding: 24, maxWidth: 720 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 20 }}>
        <button
          onClick={onBack}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            color: "var(--text-muted)",
            padding: 0,
          }}
          aria-label="Back to thread list"
        >
          ← Back
        </button>
        <div style={{ flex: 1 }}>
          <h2 style={{ margin: 0 }}>{thread.lead_name || thread.email}</h2>
          <div style={{ color: "var(--text-muted)", fontSize: 13 }}>
            {thread.company} · {thread.email}
          </div>
        </div>
        <FolderBadge folder={thread.folder} />
      </div>

      {/* Action buttons */}
      <div style={{ display: "flex", gap: 8, marginBottom: 20 }}>
        {thread.has_pending_draft && (
          <button
            className="btn"
            data-variant="brand"
            onClick={() => navigate("/review")}
          >
            Go to review queue
          </button>
        )}
        <button className="btn" onClick={handleSnooze}>
          Snooze 1 day
        </button>
        {thread.folder !== "archived" ? (
          <button className="btn" onClick={handleArchive}>
            Archive
          </button>
        ) : null}
      </div>

      {/* Pending draft preview */}
      {thread.pending_draft && (
        <div
          style={{
            border: "1px solid var(--border)",
            borderRadius: 6,
            padding: 16,
            marginBottom: 20,
            background: "var(--surface-2)",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 8 }}>
            Draft waiting for approval
          </div>
          <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 4 }}>
            Subject: {thread.pending_draft.subject}
          </div>
          <pre
            style={{
              fontFamily: "inherit",
              whiteSpace: "pre-wrap",
              fontSize: 13,
              margin: 0,
            }}
          >
            {thread.pending_draft.body}
          </pre>
          <div style={{ marginTop: 12 }}>
            <button
              className="btn"
              data-variant="brand"
              onClick={() => navigate("/review")}
            >
              Approve or edit in review queue
            </button>
          </div>
        </div>
      )}

      {/* Message thread */}
      <div>
        <h3 style={{ marginTop: 0, marginBottom: 12 }}>Conversation</h3>
        {(thread.messages || []).length === 0 ? (
          <p style={{ color: "var(--text-muted)" }}>No messages yet.</p>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {thread.messages.map((msg) => (
              <div
                key={msg.id}
                style={{
                  padding: 12,
                  borderRadius: 6,
                  background:
                    msg.direction === "out"
                      ? "var(--surface-2)"
                      : "var(--surface)",
                  border: "1px solid var(--border)",
                  alignSelf: msg.direction === "out" ? "flex-end" : "flex-start",
                  maxWidth: "80%",
                  width: "100%",
                }}
              >
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--text-muted)",
                    marginBottom: 6,
                  }}
                >
                  {msg.direction === "out" ? "You" : msg.from_addr} ·{" "}
                  {msg.sent_at
                    ? new Date(msg.sent_at).toLocaleString()
                    : msg.received_at
                    ? new Date(msg.received_at).toLocaleString()
                    : ""}
                </div>
                {msg.subject && (
                  <div style={{ fontWeight: 600, marginBottom: 4, fontSize: 13 }}>
                    {msg.subject}
                  </div>
                )}
                <pre
                  style={{
                    fontFamily: "inherit",
                    whiteSpace: "pre-wrap",
                    fontSize: 13,
                    margin: 0,
                  }}
                >
                  {msg.body_text || "(no body)"}
                </pre>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function Inbox({ onError, onToast }) {
  const [folder, setFolder] = useState(null);
  const [threads, setThreads] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(null);
  const [nextCursor, setNextCursor] = useState(null);

  const load = useCallback(
    (cursor = null, replace = true) => {
      setLoading(true);
      const params = new URLSearchParams();
      if (folder) params.set("folder", folder);
      if (cursor) params.set("cursor", cursor);
      getJson(`/inbox/threads?${params}`)
        .then((body) => {
          setThreads((prev) =>
            replace ? (body.threads ?? []) : [...prev, ...(body.threads ?? [])]
          );
          setNextCursor(body.next_cursor ?? null);
        })
        .catch(onError)
        .finally(() => setLoading(false));
    },
    [folder, onError]
  );

  useEffect(() => {
    setSelectedId(null);
    load(null, true);
  }, [load]);

  if (selectedId) {
    return (
      <ThreadDetail
        leadId={selectedId}
        onBack={() => setSelectedId(null)}
        onError={onError}
        onToast={onToast}
      />
    );
  }

  return (
    <div>
      <PageHeader title="Inbox">
        One list of all inbound threads, across campaigns. Approve or reject drafts
        from the{" "}
        <a href="/review" onClick={(e) => { e.preventDefault(); navigate("/review"); }}>
          review queue
        </a>
        .
      </PageHeader>

      {/* Folder rail */}
      <div
        style={{
          display: "flex",
          gap: 4,
          flexWrap: "wrap",
          marginBottom: 16,
        }}
        role="tablist"
      >
        {FOLDERS.map((f) => (
          <button
            key={f.id ?? "all"}
            role="tab"
            aria-selected={folder === f.id}
            className="btn"
            data-variant={folder === f.id ? "brand" : undefined}
            data-size="sm"
            onClick={() => setFolder(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Thread list */}
      {loading && threads.length === 0 ? (
        <SkeletonList count={5} rows={2} />
      ) : threads.length === 0 ? (
        <Empty title="No threads here" note="Replies and conversations appear as they arrive." />
      ) : (
        <div
          style={{
            border: "1px solid var(--border)",
            borderRadius: 6,
            overflow: "hidden",
          }}
        >
          {threads.map((t) => (
            <ThreadRow
              key={t.lead_id}
              thread={t}
              onClick={() => setSelectedId(t.lead_id)}
            />
          ))}
          {nextCursor && (
            <div style={{ padding: "12px 16px", textAlign: "center" }}>
              <button
                className="btn"
                onClick={() => load(nextCursor, false)}
              >
                Load more
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
