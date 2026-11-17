// The leads list, one lead's drawer, and the research brief behind an email.
//
// Extracted from `App.jsx` in S18.0 -- moves and imports only, no behaviour change.
//
// An operator with a thousand leads cannot scroll to find one, so email is a search box, and
// name, company and state are filters that combine as AND.

import { useCallback, useEffect, useState } from "react";

import { getJson, postJson } from "./api";
import { navigate } from "./router";
import { Drawer, Icons, PageHeader, SkeletonList, stateTone } from "./ui";

const LEADS_PAGE = 25;

const STATES = [
  "NEW",
  "ENRICHING",
  "RESEARCHED",
  "DRAFTED",
  "AWAITING_APPROVAL",
  "APPROVED",
  "SENT",
  "FOLLOWUP_SCHEDULED",
  "REPLIED",
  "MEETING_BOOKED",
  "BOUNCED",
  "OPTED_OUT",
  "NO_RESPONSE",
  "SKIPPED",
  "ERROR",
];

export default function Leads({ campaignId, openLeadId = null, onError, onToast }) {
  const [page, setPage] = useState({ leads: [], next_cursor: null });
  const [filters, setFilters] = useState({
    email: "",
    name: "",
    company: "",
    state: "",
  });
  const [applied, setApplied] = useState({
    email: "",
    name: "",
    company: "",
    state: "",
  });
  const [cursor, setCursor] = useState(null);
  const [trail, setTrail] = useState([]);
  /* **The URL is the source of truth for which lead is open.** `/leads/:id` opens the drawer
     on load, so a lead is a link somebody can send and the browser's back button closes it.
     Clicking a row navigates rather than setting state, which is what keeps the two in step --
     two sources for one fact is the defect this project has recorded a dozen times. */
  const openLead = openLeadId;
  const [reload, setReload] = useState(0);

  // Typing does not fetch. A request per keystroke is a request per keystroke against a table
  // that will have thousands of rows; the state dropdown applies at once because picking from a
  // list is already a deliberate act.
  useEffect(() => {
    const params = new URLSearchParams({ limit: String(LEADS_PAGE) });
    for (const [key, value] of Object.entries(applied)) {
      if (value.trim()) params.set(key, value.trim());
    }
    if (cursor) params.set("cursor", cursor);
    getJson(`/campaigns/${campaignId}/leads?${params}`)
      .then(setPage)
      .catch(onError);
  }, [campaignId, applied, cursor, onError, reload]);

  const apply = (next) => {
    setCursor(null);
    setTrail([]);
    setApplied(next);
  };

  const set = (key) => (event) =>
    setFilters({ ...filters, [key]: event.target.value });

  const dirty = Object.keys(filters).some(
    (key) => filters[key] !== applied[key],
  );
  const anyFilter = Object.values(applied).some((value) => value.trim());

  return (
    <>
      <PageHeader icon={Icons.leads} eyebrow="Workspace" title={<>Leads</>}>
        Email is unique, so it is a search box. Name, company and state are
        filters, and they combine — all of them have to match. Bodies are not
        searched: a search box over drafts and replies would be an export of
        everything anybody ever wrote.
      </PageHeader>

      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault();
          apply(filters);
        }}
      >
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-email">Email</label>
          <input
            id="f-email"
            type="search"
            value={filters.email}
            onChange={set("email")}
          />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-name">Name</label>
          <input
            id="f-name"
            type="search"
            value={filters.name}
            onChange={set("name")}
          />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-company">Company</label>
          <input
            id="f-company"
            type="search"
            value={filters.company}
            onChange={set("company")}
          />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-state">State</label>
          <select
            id="f-state"
            value={filters.state}
            onChange={(event) => {
              const next = { ...filters, state: event.target.value };
              setFilters(next);
              apply(next);
            }}
          >
            <option value="">Any state</option>
            {STATES.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>
        <div className="btn-row" style={{ marginTop: 0 }}>
          <button
            type="submit"
            className="btn"
            data-variant={dirty ? "primary" : undefined}
          >
            Search
          </button>
          {anyFilter || dirty ? (
            <button
              type="button"
              className="btn"
              data-variant="quiet"
              onClick={() => {
                const empty = { email: "", name: "", company: "", state: "" };
                setFilters(empty);
                apply(empty);
              }}
            >
              Clear
            </button>
          ) : null}
        </div>
      </form>

      {page.leads.length === 0 ? (
        <p className="empty">
          {anyFilter
            ? "No lead matches those filters."
            : "No leads yet. Import a sheet to start."}
        </p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Name</th>
                <th>Company</th>
                <th>State</th>
                <th>Next action</th>
              </tr>
            </thead>
            <tbody>
              {page.leads.map((lead) => (
                <tr
                  key={lead.id}
                  onClick={() => navigate(`/leads/${lead.id}`)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="mono">{lead.email}</td>
                  <td>{lead.full_name || "—"}</td>
                  <td>{lead.company_name || "—"}</td>
                  <td>
                    <span className="badge" data-tone={stateTone(lead.state)}>
                      {lead.state}
                    </span>
                    {lead.paused ? (
                      <>
                        {" "}
                        <span className="badge" data-tone="warn">
                          paused
                        </span>
                      </>
                    ) : null}
                  </td>
                  <td className="muted">
                    {lead.next_action_at
                      ? new Date(lead.next_action_at).toLocaleString()
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
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
        <span>Newest first. Select a row to see the full history.</span>
      </div>

      {openLead ? (
        <LeadDetail
          leadId={openLead}
          onClose={() => navigate("/leads")}
          onError={onError}
          onToast={onToast}
          onChanged={() => setReload((n) => n + 1)}
        />
      ) : null}
    </>
  );
}

/* ------------------------------------------------------------------ the brief (S15.3)

   Five sections, and **every line shows the source that says so**. The source sits beside the
   line rather than in a block at the end, because a footnote list is exactly how a line gets
   separated from the thing that justified it -- and this is text somebody repeats to a stranger.

   "Improve the wording" is a button rather than something that happens on open: it spends a
   model call, and a read that spends is the pattern this project refuses everywhere else. What
   comes back is the same lines with the same sources; a rewording that asserted anything its
   own line does not is refused, and the refusal is shown, because a model trying to add a claim
   is worth seeing. */

function Brief({ leadId, onError }) {
  const [brief, setBrief] = useState(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setFailed(false);
    getJson(`/leads/${leadId}/brief`)
      .then(setBrief)
      .catch((err) => {
        setFailed(true);
        onError(err);
      });
  }, [leadId, onError]);

  const improve = () => {
    setBusy(true);
    postJson(`/leads/${leadId}/brief/phrasing`)
      .then(setBrief)
      .catch(onError)
      .finally(() => setBusy(false));
  };

  if (failed) return <p className="muted">Could not load the brief.</p>;
  if (!brief) return <SkeletonList count={2} rows={3} />;

  return (
    <>
      <h3 className="section-title">Brief</h3>
      <p className="muted">
        {brief.header?.note} <strong>{brief.cited}</strong> line
        {brief.cited === 1 ? "" : "s"} we can point at.
      </p>

      {brief.sections.map((section) => (
        <div key={section.name} style={{ marginTop: 12 }}>
          <strong>{section.title}</strong>
          {section.lines.length === 0 ? (
            <p className="hint">Nothing citable yet.</p>
          ) : (
            <ul className="list">
              {section.lines.map((line) => (
                <li key={line.line_id} className="list-row">
                  <div>
                    <span>{line.text}</span>
                    <p className="hint">
                      {line.source_url ? (
                        <a
                          href={line.source_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          source
                        </a>
                      ) : line.from_a_message ? (
                        "from a message in this thread"
                      ) : (
                        "no source"
                      )}
                      {line.observed_at
                        ? ` - ${new Date(line.observed_at).toLocaleDateString()}`
                        : ""}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}

      {/* The fifth thing a brief owes its reader: what it does not know. A brief with four thin
          sections invites whoever reads it to fill the rest in from memory. */}
      {(brief.gaps || []).length ? (
        <div style={{ marginTop: 12 }}>
          <strong>What we do not know</strong>
          <ul className="list">
            {brief.gaps.map((gap, index) => (
              <li key={index} className="list-row">
                <span className="hint">{gap}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(brief.refusals || []).length ? (
        <div className="banner" data-tone="warn" style={{ marginTop: 12 }}>
          <Icons.alert size={16} />
          <span>
            <strong>Some rewordings were refused.</strong>{" "}
            {brief.refusals.join(" ")}
          </span>
        </div>
      ) : null}

      <div className="btn-row">
        <button
          type="button"
          className="btn"
          disabled={busy || !brief.cited}
          onClick={improve}
        >
          {busy ? "Improving..." : "Improve the wording"}
        </button>
      </div>
      <p className="hint">
        A model may say these lines better and can never add one - there would
        be nothing to put in the source column.
      </p>
    </>
  );
}

function LeadDetail({ leadId, onClose, onError, onToast, onChanged }) {
  const [lead, setLead] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    getJson(`/leads/${leadId}`).then(setLead).catch(onError);
  }, [leadId, onError]);

  useEffect(load, [load]);

  const setPaused = async (paused) => {
    setBusy(true);
    try {
      await postJson(`/leads/${leadId}/${paused ? "pause" : "resume"}`);
      onToast(
        paused
          ? "Paused. Nothing further is sent to this lead, and it keeps its place."
          : "Resumed from exactly where it was.",
        "success",
      );
      load();
      onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  if (!lead) {
    return (
      <Drawer title="Loading…" onClose={onClose}>
        <SkeletonList count={2} rows={4} />
      </Drawer>
    );
  }

  return (
    <Drawer
      title={lead.full_name || lead.email}
      onClose={onClose}
      badges={
        <>
          <span className="badge" data-tone={stateTone(lead.state)}>
            {lead.state}
          </span>
          {lead.paused ? (
            <span className="badge" data-tone="warn">
              paused
            </span>
          ) : null}
        </>
      }
    >
      <table className="kv">
        <tbody>
          <tr>
            <td>Email</td>
            <td className="mono">{lead.email}</td>
          </tr>
          <tr>
            <td>Company</td>
            <td>{lead.company_name || "—"}</td>
          </tr>
          <tr>
            <td>Next action</td>
            <td className="muted">
              {lead.next_action_at
                ? new Date(lead.next_action_at).toLocaleString()
                : "—"}
            </td>
          </tr>
        </tbody>
      </table>

      {/* **Only drawn when there is something wrong.** This lead had an approved draft sitting
          for eight days showing "next action: —", which reads exactly like "waiting its turn".
          The send loop knew the reason the whole time and wrote it to a log. A panel that is
          usually absent is one people read when it appears. */}
      {lead.send_block ? (
        <div className="banner" data-tone="warn" style={{ marginTop: 14 }}>
          <Icons.alert size={16} />
          <span>
            <strong>This {lead.send_block.kind} email is not going out.</strong>{" "}
            {lead.send_block.reason}
          </span>
        </div>
      ) : null}

      <div className="btn-row">
        <button
          type="button"
          className="btn"
          disabled={busy}
          onClick={() => setPaused(!lead.paused)}
        >
          {lead.paused ? "Resume this lead" : "Pause this lead"}
        </button>
      </div>

      <Brief leadId={leadId} onError={onError} />

      <h3 className="section-title">Messages</h3>
      {(lead.messages || []).length === 0 ? (
        <p className="muted">Nothing sent or received yet.</p>
      ) : (
        (lead.messages || []).map((message) => (
          <details key={message.id} className="evidence">
            <summary>
              {message.direction === "out" ? "Sent" : "Received"} ·{" "}
              {message.status} · {message.subject || "(no subject)"} ·{" "}
              {new Date(message.created_at).toLocaleString()}
            </summary>
            <div className="evidence-body">
              {/* Plain text, never dangerouslySetInnerHTML: this is a lead's own words. */}
              <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontSize: 13 }}>
                {message.body_text}
              </pre>
            </div>
          </details>
        ))
      )}

      <h3 className="section-title">Timeline</h3>
      <p className="muted">
        The append-only audit trail — what happened to this lead, in order, and
        who caused it.
      </p>
      {(lead.timeline || []).length === 0 ? (
        <p className="muted">No events recorded.</p>
      ) : (
        <div className="table-wrap" style={{ marginTop: 12 }}>
          <table>
            <thead>
              <tr>
                <th>When</th>
                <th>Event</th>
                <th>By</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {lead.timeline.map((event, index) => (
                <tr key={index}>
                  <td className="muted">
                    {new Date(event.at).toLocaleString()}
                  </td>
                  <td>{event.type}</td>
                  <td className="muted">{event.actor || "—"}</td>
                  <td className="muted mono">
                    {event.data && Object.keys(event.data).length
                      ? JSON.stringify(event.data)
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Drawer>
  );
}

/* ------------------------------------------------------------------ import */
