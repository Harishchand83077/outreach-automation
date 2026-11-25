// The review queue: every draft waiting for a person, a few at a time.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change. It was one
// of thirty-odd components in a 5,374-line file, which made every diff that touched a screen a
// diff nobody could read in one sitting.
//
// `approval_mode = review_all` is a product rule rather than a setting this screen can relax: a
// first email and every reply wait here until somebody presses Approve.

import { useCallback, useEffect, useState } from "react";

import { getJson, postJson } from "./api";
import { Empty, Icons, PageHeader } from "./ui";

const REVIEW_PAGE = 5;

export default function ReviewQueue({ campaignId, onError, onToast, onChanged }) {
  const [page, setPage] = useState({ drafts: [], total: 0, next_cursor: null });
  const [cursor, setCursor] = useState(null);
  const [trail, setTrail] = useState([]);
  const [edits, setEdits] = useState({});
  const [busy, setBusy] = useState(null);
  const [selected, setSelected] = useState([]);
  const [skipped, setSkipped] = useState({});
  const [bulkFeedback, setBulkFeedback] = useState("");
  const [dismissing, setDismissing] = useState(new Set());
  //: Which card the keyboard is on. Reviewing a hundred drafts with a mouse is a hundred
  //: round trips to a small button; J/K and A/R is the difference between a queue somebody
  //: works through and one they put off.
  const [focused, setFocused] = useState(0);
  // Campaign filter — the picker is internal; the prop is the initial value.
  const [activeCampaignId, setActiveCampaignId] = useState(campaignId || "");
  const [campaigns, setCampaigns] = useState([]);
  const [showHelp, setShowHelp] = useState(false);

  // Load campaign list for the picker once on mount.
  useEffect(() => {
    getJson("/campaigns/overview")
      .then((data) => setCampaigns(data.campaigns ?? []))
      .catch(() => {}); // picker is a convenience; silently degraded is fine
  }, []);

  const load = useCallback(() => {
    const params = new URLSearchParams({ limit: String(REVIEW_PAGE) });
    if (activeCampaignId) params.set("campaign_id", activeCampaignId);
    if (cursor) params.set("cursor", cursor);
    getJson(`/review/drafts?${params}`).then(setPage).catch(onError);
  }, [activeCampaignId, cursor, onError]);

  useEffect(load, [load]);

  // Judging a draft removes it from the queue, so the page refills from the same cursor rather
  // than advancing. Advancing would skip the drafts that moved up into the gap.
  const afterDecision = () => {
    load();
    onChanged();
  };

  const dismiss = (id) => {
    setDismissing((current) => new Set([...current, id]));
    return new Promise((resolve) => setTimeout(resolve, 180));
  };

  const approve = async (item) => {
    setBusy(item.draft_id);
    try {
      const edit = edits[item.draft_id];
      await postJson(`/drafts/${item.draft_id}/approve`, {
        json: edit ? edit : {},
      });
      onToast(
        "Approved. The send loop decides when it actually goes.",
        "success",
      );
      await dismiss(item.draft_id);
      afterDecision();
    } catch (err) {
      setDismissing((current) => { const s = new Set(current); s.delete(item.draft_id); return s; });
      onError(err);
    } finally {
      setBusy(null);
    }
  };

  // Whether the reviewer has changed anything. Used to decide if a draft that failed
  // validation may be approved: the check objected to something concrete in *this* text, so
  // approving it untouched is a reviewer clicking past a warning rather than answering it.
  const hasEdit = (item) => {
    const edit = edits[item.draft_id] || {};
    const subject = edit.subject;
    const body = edit.body;
    return (
      (subject !== undefined && subject !== item.subject) ||
      (body !== undefined && body !== item.body)
    );
  };

  const reject = async (item) => {
    const feedback = (edits[item.draft_id] || {}).feedback || "";
    if (!feedback.trim()) {
      onToast(
        "Rejecting needs a reason — it is what the next draft is written from.",
        "error",
      );
      return;
    }
    setBusy(item.draft_id);
    try {
      await postJson(`/drafts/${item.draft_id}/reject`, { json: { feedback } });
      onToast("Rejected. It will be regenerated with your note.", "info");
      await dismiss(item.draft_id);
      afterDecision();
    } catch (err) {
      setDismissing((current) => { const s = new Set(current); s.delete(item.draft_id); return s; });
      onError(err);
    } finally {
      setBusy(null);
    }
  };

  const setEdit = (id, patch) =>
    setEdits((current) => ({
      ...current,
      [id]: { ...(current[id] || {}), ...patch },
    }));

  const toggle = (id) =>
    setSelected((current) =>
      current.includes(id)
        ? current.filter((value) => value !== id)
        : [...current, id],
    );

  // Only drafts that passed validation can be selected. The ones that failed are exactly the
  // ones worth reading, and the API refuses them anyway — disabling the box says so before the
  // click rather than after it.
  const selectable = page.drafts
    .filter((item) => item.status !== "validation_failed")
    .map((item) => item.draft_id);

  // The reviewer's attachment choices, keyed by draft, applied over whatever the server last
  // said. Without this the box would snap back for the moment between the click and the
  // refetch, which reads as the click not having worked.
  const [attachOverrides, setAttachOverrides] = useState({});

  const toggleAttach = async (item, next) => {
    setAttachOverrides((current) => ({ ...current, [item.draft_id]: next }));
    try {
      await postJson(`/drafts/${item.draft_id}/attach`, {
        json: { attach: next },
      });
    } catch (err) {
      // Put it back. A control that stays where the operator left it after the server refused
      // is a control that lies about what will be sent.
      setAttachOverrides((current) => ({ ...current, [item.draft_id]: !next }));
      onError(err);
    }
  };

  const bulkApprove = async () => {
    setBusy("bulk");
    try {
      const result = await postJson("/review/bulk-approve", {
        json: { draft_ids: selected },
      });
      setSkipped(result.skipped || {});
      const held = Object.keys(result.skipped || {}).length;
      onToast(
        held
          ? `Approved ${result.approved.length}. ${held} still need you — see the notes below.`
          : `Approved ${result.approved.length}.`,
        held ? "info" : "success",
      );
      setSelected([]);
      afterDecision();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(null);
    }
  };

  const bulkReject = async () => {
    const fb = bulkFeedback.trim();
    if (!fb) {
      onToast("Add a note first — it is what the next draft is written from.", "error");
      return;
    }
    setBusy("bulk-reject");
    try {
      const result = await postJson("/review/bulk-reject", {
        json: { draft_ids: selected, feedback: fb },
      });
      const held = Object.keys(result.skipped || {}).length;
      onToast(
        held
          ? `Rejected ${result.rejected.length}. ${held} could not be rejected — see notes below.`
          : `Rejected ${result.rejected.length}. Each will be regenerated with your note.`,
        held ? "info" : "info",
      );
      setBulkFeedback("");
      setSelected([]);
      afterDecision();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(null);
    }
  };

  const shown = page.drafts.length;
  const seen = trail.length * REVIEW_PAGE;

  // Keyboard review. Deliberately *not* active while typing: an editor fixing a subject line
  // must be able to write "approach" without approving three drafts on the way through.
  useEffect(() => {
    const onKey = (event) => {
      const tag = event.target?.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        event.target?.isContentEditable
      )
        return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      const item = page.drafts[focused];
      const key = event.key.toLowerCase();

      if (key === "j" || key === "arrowdown") {
        event.preventDefault();
        setFocused((current) => Math.min(current + 1, page.drafts.length - 1));
      } else if (key === "k" || key === "arrowup") {
        event.preventDefault();
        setFocused((current) => Math.max(current - 1, 0));
      } else if (key === "a" && item) {
        event.preventDefault();
        approve(item);
      } else if (key === "r" && item) {
        event.preventDefault();
        reject(item);
      } else if (key === "x" && item) {
        event.preventDefault();
        toggle(item.draft_id);
      } else if (key === "?") {
        event.preventDefault();
        setShowHelp((v) => !v);
      } else if (key === "escape") {
        setShowHelp(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // A page that shrank under the cursor must not leave the focus past its end.
  useEffect(() => {
    setFocused((current) =>
      Math.min(current, Math.max(page.drafts.length - 1, 0)),
    );
  }, [page.drafts.length]);

  // Keep the focused card on screen when the keyboard moves it, not when a mouse does.
  useEffect(() => {
    const node = document.querySelector('.card[data-active="true"]');
    node?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [focused]);

  return (
    <>
      <PageHeader
        icon={Icons.review}
        eyebrow="Workspace"
        title={<>Review queue</>}
      >
        Nothing is sent without a decision here. Approving makes a lead
        eligible; the send loop re-checks the suppression list, the window and
        today&apos;s cap before anything leaves.
      </PageHeader>

      {/* Campaign filter — lets the reviewer scope the queue without leaving the screen */}
      {campaigns.length > 1 && (
        <div style={{ marginBottom: 12 }}>
          <select
            className="input"
            style={{ maxWidth: 280 }}
            value={activeCampaignId}
            aria-label="Filter by campaign"
            onChange={(e) => {
              setActiveCampaignId(e.target.value);
              setCursor(null);
              setTrail([]);
              setSelected([]);
            }}
          >
            <option value="">All campaigns</option>
            {campaigns.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
      )}

      <div className="queue-head">
        <span className="badge" data-tone={page.total > 0 ? "warn" : "good"}>
          <span className="dot" data-pulse={page.total > 0} />
          {page.total} waiting
        </span>
        {shown ? (
          <>
            <span className="muted num">
              {seen + 1}–{seen + shown} of {page.total}
            </span>
            <span
              className="progress"
              role="progressbar"
              aria-valuenow={seen}
              aria-valuemin={0}
              aria-valuemax={page.total}
              aria-label="Reviewed so far"
            >
              <span
                style={{
                  width: `${page.total ? (seen / page.total) * 100 : 0}%`,
                }}
              />
            </span>
          </>
        ) : null}
        <span className="spacer" />
        <span
          className="muted"
          style={{ display: "flex", alignItems: "center", gap: 6 }}
        >
          <span className="kbd">J</span>
          <span className="kbd">K</span>
          move
          <span className="kbd">A</span>
          approve
          <span className="kbd">R</span>
          reject
          <span className="kbd">?</span>
          help
        </span>
      </div>

      {shown === 0 ? null : (
        <div
          className="filters"
          style={{ gridTemplateColumns: "1fr auto auto auto", gap: 8 }}
        >
          <span className="muted" style={{ alignSelf: "center" }}>
            {selected.length
              ? `${selected.length} selected`
              : "Select drafts to judge several at once. Weak ones are held back on approval, not waved through."}
          </span>
          <button
            type="button"
            className="btn"
            onClick={() =>
              setSelected(
                selected.length === selectable.length ? [] : selectable,
              )
            }
          >
            {selected.length === selectable.length && selectable.length
              ? "Clear selection"
              : "Select all on this page"}
          </button>
          {selected.length > 0 && (
            <input
              type="text"
              className="input"
              placeholder="Rejection note (required)…"
              value={bulkFeedback}
              onChange={(e) => setBulkFeedback(e.target.value)}
              style={{ minWidth: 220 }}
              aria-label="Shared note for bulk rejection"
            />
          )}
          {selected.length > 0 && (
            <button
              type="button"
              className="btn"
              data-variant="danger"
              disabled={busy === "bulk-reject"}
              onClick={bulkReject}
            >
              {busy === "bulk-reject"
                ? "Rejecting…"
                : `Reject ${selected.length}`}
            </button>
          )}
          <button
            type="button"
            className="btn"
            data-variant="primary"
            disabled={!selected.length || busy === "bulk"}
            onClick={bulkApprove}
          >
            {busy === "bulk"
              ? "Approving…"
              : `Approve ${selected.length || ""}`.trim()}
          </button>
        </div>
      )}

      {shown === 0 ? (
        <Empty icon={Icons.check} title="Nothing waiting">
          Every draft for this campaign has been judged. New ones appear here
          once research and drafting finish — or straight away for a template
          campaign.
        </Empty>
      ) : (
        page.drafts.map((item, index) => (
          <article
            key={item.draft_id}
            className="card"
            data-interactive="true"
            data-active={index === focused}
            data-dismissing={dismissing.has(item.draft_id) ? "true" : undefined}
            onMouseEnter={() => setFocused(index)}
          >
            <div className="card-head">
              <input
                type="checkbox"
                aria-label={`Select the draft for ${item.lead.email}`}
                checked={selected.includes(item.draft_id)}
                disabled={item.status === "validation_failed"}
                onChange={() => toggle(item.draft_id)}
                style={{ width: "auto", margin: 0 }}
              />
              <strong>{item.lead.full_name || item.lead.email}</strong>
              <span className="badge">
                {item.lead.company_name || "no company"}
              </span>
              <span className="badge">{item.kind}</span>
              <span className="badge">v{item.version}</span>
              {item.status === "validation_failed" ? (
                <span className="badge" data-tone="bad">
                  validation failed
                </span>
              ) : null}
              <span className="spacer" />
              <span className="muted mono">{item.lead.email}</span>
            </div>

            <div className="field">
              <label htmlFor={`s-${item.draft_id}`}>Subject</label>
              <input
                id={`s-${item.draft_id}`}
                type="text"
                value={(edits[item.draft_id] || {}).subject ?? item.subject}
                onChange={(event) =>
                  setEdit(item.draft_id, { subject: event.target.value })
                }
              />
            </div>

            <div className="field">
              <label htmlFor={`b-${item.draft_id}`}>Body</label>
              <textarea
                id={`b-${item.draft_id}`}
                rows={11}
                value={(edits[item.draft_id] || {}).body ?? item.body}
                onChange={(event) =>
                  setEdit(item.draft_id, { body: event.target.value })
                }
              />
            </div>

            <Evidence item={item} />

            <div className="field">
              <label htmlFor={`f-${item.draft_id}`}>Reason, if rejecting</label>
              <input
                id={`f-${item.draft_id}`}
                type="text"
                value={(edits[item.draft_id] || {}).feedback || ""}
                onChange={(event) =>
                  setEdit(item.draft_id, { feedback: event.target.value })
                }
                placeholder="Too long; lead with the pipeline rebuild"
              />
            </div>

            {/* Only drawn when the campaign actually has a file. A permanently disabled
                checkbox would raise a question the screen then refuses to answer. */}
            {item.attachment_filename ? (
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={attachOverrides[item.draft_id] ?? item.attached}
                  onChange={(event) => toggleAttach(item, event.target.checked)}
                />
                <Icons.paperclip size={14} />
                <span>
                  Attach <strong>{item.attachment_filename}</strong> to this
                  email
                </span>
              </label>
            ) : null}

            {/* The webhook URL looks like any booking link and is a secret; the server refuses
                to approve it, and this says why before somebody presses the button. */}
            {`${edits[item.draft_id]?.body ?? item.body}`.includes("/hooks/booking/") ? (
              <div className="banner" data-tone="bad" role="alert" style={{ marginTop: 10 }}>
                <span>
                  The link in this email is your booking <strong>webhook</strong> URL, which is a
                  secret and cannot be sent. Replace it with your real booking page, and fix the
                  campaign's booking link.
                </span>
              </div>
            ) : null}
            <div className="btn-row">
              <button
                type="button"
                className="btn"
                data-variant="primary"
                onClick={() => approve(item)}
                disabled={
                  busy === item.draft_id ||
                  (item.status === "validation_failed" && !hasEdit(item))
                }
              >
                Approve
              </button>
              <button
                type="button"
                className="btn"
                onClick={() => reject(item)}
                disabled={busy === item.draft_id}
              >
                Reject
              </button>
            </div>
            {item.status === "validation_failed" ? (
              <p className="muted" style={{ marginTop: 10 }}>
                This draft failed the checks above, so it cannot be sent as it
                stands. Reject it with a note and it will be written again, or
                edit the text yourself — Approve becomes available once you have
                changed something.
              </p>
            ) : null}
            {skipped[item.draft_id] ? (
              <div
                className="banner"
                data-tone="warn"
                style={{ marginTop: 10 }}
              >
                <span>
                  Held back from the bulk approval: {skipped[item.draft_id]}
                </span>
              </div>
            ) : null}
          </article>
        ))
      )}

      <div className="pager">
        <button
          type="button"
          className="btn"
          disabled={trail.length === 0}
          onClick={() => {
            const back = trail.slice(0, -1);
            setTrail(back);
            setCursor(back.length ? back[back.length - 1] : null);
          }}
        >
          Previous
        </button>
        <button
          type="button"
          className="btn"
          disabled={!page.next_cursor}
          onClick={() => {
            setTrail([...trail, page.next_cursor]);
            setCursor(page.next_cursor);
          }}
        >
          Next
        </button>
        <span className="spacer" />
        <span>Oldest first, so nothing waits indefinitely.</span>
      </div>

      {/* Keyboard shortcut overlay — press ? to toggle */}
      {showHelp && (
        <>
          <div
            style={{
              position: "fixed",
              inset: 0,
              background: "rgba(0,0,0,0.4)",
              zIndex: 199,
            }}
            onClick={() => setShowHelp(false)}
          />
          <div
            className="card"
            style={{
              position: "fixed",
              top: "50%",
              left: "50%",
              transform: "translate(-50%,-50%)",
              zIndex: 200,
              minWidth: 320,
              padding: 24,
            }}
            role="dialog"
            aria-label="Keyboard shortcuts"
          >
            <strong style={{ display: "block", marginBottom: 16 }}>
              Keyboard shortcuts
            </strong>
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr",
                gap: "6px 16px",
                margin: 0,
              }}
            >
              {[
                [["J", "↓"], "Move down"],
                [["K", "↑"], "Move up"],
                [["A"], "Approve focused draft"],
                [["R"], "Reject focused draft"],
                [["X"], "Toggle select focused draft"],
                [["?"], "Show / hide this overlay"],
              ].map(([keys, label]) => (
                <>
                  <dt style={{ display: "flex", gap: 4, alignItems: "center" }}>
                    {keys.map((k) => (
                      <span key={k} className="kbd">
                        {k}
                      </span>
                    ))}
                  </dt>
                  <dd style={{ margin: 0 }}>{label}</dd>
                </>
              ))}
            </dl>
            <div style={{ marginTop: 16, textAlign: "right" }}>
              <button
                className="btn"
                data-size="sm"
                onClick={() => setShowHelp(false)}
              >
                Close
              </button>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function Evidence({ item }) {
  const hooks = item.hooks || [];
  return (
    <details className="evidence">
      <summary>
        Evidence — {hooks.length} research hook{hooks.length === 1 ? "" : "s"}
        {item.thin_research ? " · thin" : ""} · confidence{" "}
        {Number(item.research_confidence || 0).toFixed(2)}
      </summary>
      <div className="evidence-body">
        {hooks.length === 0 ? (
          <p className="muted">
            No verified research. Anything specific in this draft has nothing
            behind it.
          </p>
        ) : (
          hooks.map((hook) => (
            <div key={hook.id || hook.text} className="hook">
              <strong>{hook.id}</strong> — {hook.text}
              {hook.snippet ? (
                <div className="snippet">“{hook.snippet}”</div>
              ) : null}
              {hook.source_url ? (
                <div className="muted">
                  <a
                    href={hook.source_url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {hook.source_url}
                  </a>
                </div>
              ) : null}
            </div>
          ))
        )}
      </div>
    </details>
  );
}

/* ------------------------------------------------------------------- leads */
