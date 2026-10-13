// The dashboard, against `/api/v1`.
//
// Rebuilt again for Phase 8. The previous version was one long scroll of every section at once,
// styled with inline objects. Three things forced the change, and each one is a layout decision
// rather than a cosmetic one:
//
// **A reviewer cannot be handed a hundred drafts.** Each carries a subject, a body and its
// research hooks, so the queue pages — a few at a time, with the total still waiting, because
// "47 left" is what tells somebody whether to start now.
//
// **An operator with a thousand leads cannot scroll to find one.** Email is unique so it gets a
// search box; name, company and state get filters, and they combine as AND.
//
// **Everything is scoped to one campaign.** It sits in the bar above the content rather than
// inside each section, because choosing it changes what every screen means.
//
// Settings are behind a role, not merely a hidden button: `member` can review drafts but cannot
// change the rules underneath them, and the API refuses them too.
//
// Plain text throughout. No `dangerouslySetInnerHTML` anywhere — a draft body is a lead's name
// and a model's output, neither of which is trusted (hard rule 7).

import React, { useCallback, useEffect, useMemo, useState } from 'react'

import { SignIn, useSession } from './Auth'
import {
  ApiUnreachable,
  NotAuthenticated,
  deleteJson,
  getJson,
  patchJson,
  postJson,
  uploadFile,
} from './api'
import { signOut } from './supabase'
import './app.css'

const STATES = [
  'NEW',
  'ENRICHING',
  'RESEARCHED',
  'DRAFTED',
  'AWAITING_APPROVAL',
  'APPROVED',
  'SENT',
  'FOLLOWUP_SCHEDULED',
  'REPLIED',
  'MEETING_BOOKED',
  'BOUNCED',
  'OPTED_OUT',
  'NO_RESPONSE',
  'SKIPPED',
  'ERROR',
]

//: Weekday 0 is Monday, matching `CampaignSettings.send_days`. Checkboxes rather than a text
//: field precisely because the one production outage this screen prevents was a hand-written
//: UPDATE that put the *names* in that column.
const WEEKDAYS = [
  [0, 'Mon'],
  [1, 'Tue'],
  [2, 'Wed'],
  [3, 'Thu'],
  [4, 'Fri'],
  [5, 'Sat'],
  [6, 'Sun'],
]

const REVIEW_PAGE = 5
const LEADS_PAGE = 25

export default function App() {
  const session = useSession()

  if (session === undefined) return <Centered>Checking your session…</Centered>
  if (!session) return <SignIn />
  return <Dashboard session={session} />
}

function Dashboard({ session }) {
  const [me, setMe] = useState(null)
  const [error, setError] = useState(null)
  const [toasts, setToasts] = useState([])
  const [campaigns, setCampaigns] = useState([])
  const [campaignId, setCampaignId] = useState(null)
  const [view, setView] = useState('review')
  const [attention, setAttention] = useState(null)
  const [queueCount, setQueueCount] = useState(null)

  const toast = useCallback((message, kind = 'info') => {
    const id = Math.random().toString(36).slice(2)
    setToasts((current) => [...current, { id, message, kind }])
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 6000)
  }, [])

  // One place that decides what an error means. An expired session is not something to show in a
  // red box — it is a reason to send somebody back to the sign-in screen.
  const handle = useCallback((err) => {
    if (err instanceof NotAuthenticated) {
      signOut()
      return
    }
    setError(err instanceof ApiUnreachable ? err.message : String(err.message || err))
  }, [])

  useEffect(() => {
    getJson('/me')
      .then(setMe)
      .catch((err) => {
        if (err instanceof NotAuthenticated) {
          signOut()
          return
        }
        // 403 means authenticated but not a member: say so plainly rather than showing an
        // empty dashboard that looks broken.
        setError(String(err.message || err))
      })
  }, [])

  const refreshCampaigns = useCallback(() => {
    getJson('/campaigns')
      .then((data) => {
        setCampaigns(data.campaigns)
        setCampaignId((current) => current || data.campaigns[0]?.id || null)
      })
      .catch(handle)
  }, [handle])

  const refreshAttention = useCallback(() => {
    getJson('/admin/needs-attention').then(setAttention).catch(handle)
  }, [handle])

  // The sidebar badge is the count for the *selected* campaign, not the org, because the screen
  // it labels is campaign-scoped — a badge saying 12 above a queue showing 3 is worse than no
  // badge. `limit=1` because only `total` is wanted; the page itself is fetched by the queue.
  const refreshQueueCount = useCallback(() => {
    if (!campaignId) {
      setQueueCount(null)
      return
    }
    getJson(`/review/drafts?campaign_id=${campaignId}&limit=1`)
      .then((data) => setQueueCount(data.total))
      .catch(handle)
  }, [campaignId, handle])

  useEffect(() => {
    if (!me) return
    refreshCampaigns()
    refreshAttention()
  }, [me, refreshCampaigns, refreshAttention])

  useEffect(() => {
    if (!me) return
    refreshQueueCount()
  }, [me, refreshQueueCount])

  const campaign = useMemo(
    () => campaigns.find((row) => row.id === campaignId) || null,
    [campaigns, campaignId]
  )

  // The gate is the API's; this only decides what to draw. A hidden button is not security, so
  // every endpoint behind these screens checks the role itself.
  const mayAdminister = me ? ['owner', 'admin'].includes(me.role) : false

  if (error && !me) {
    return (
      <Centered>
        <h1 className="page-title">Cannot load the dashboard</h1>
        <p className="page-note">{error}</p>
        <button type="button" onClick={() => signOut()} className="btn">
          Sign out
        </button>
      </Centered>
    )
  }

  if (!me) return <Centered>Loading…</Centered>

  const nav = [
    { id: 'review', label: 'Review queue', count: queueCount ?? undefined },
    { id: 'leads', label: 'Leads' },
    { id: 'import', label: 'Import' },
    { id: 'campaign', label: 'Campaign' },
    ...(mayAdminister ? [{ id: 'settings', label: 'Settings' }] : []),
  ]

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Sections">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            O
          </span>
          <span className="brand-name">Outreach</span>
        </div>

        <span className="nav-label">Workspace</span>
        {nav.map((item) => (
          <button
            key={item.id}
            type="button"
            className="nav-item"
            aria-current={view === item.id ? 'page' : undefined}
            onClick={() => setView(item.id)}
          >
            {item.label}
            {item.count === undefined ? null : (
              <span className="count" data-zero={item.count === 0}>
                {item.count}
              </span>
            )}
          </button>
        ))}

        <div className="sidebar-foot">
          {me.email || session.user?.email}
          <br />
          {me.role}
          <button
            type="button"
            className="btn"
            data-variant="quiet"
            style={{ marginTop: 10, padding: '6px 10px' }}
            onClick={() => signOut()}
          >
            Sign out
          </button>
        </div>
      </nav>

      <div className="main">
        <div className="topbar">
          <label className="form-label" htmlFor="campaign" style={{ margin: 0 }}>
            Campaign
          </label>
          <select
            id="campaign"
            value={campaignId || ''}
            onChange={(event) => setCampaignId(event.target.value || null)}
            style={{ maxWidth: 280 }}
          >
            {campaigns.length === 0 ? <option value="">No campaigns yet</option> : null}
            {campaigns.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
          {campaign ? (
            <>
              <span className="badge" data-tone={statusTone(campaign)}>
                {campaign.paused ? 'paused' : campaign.status}
              </span>
              {campaign.mailbox_id ? null : (
                <span className="badge" data-tone="warn">
                  no mailbox
                </span>
              )}
            </>
          ) : null}
          <span className="spacer" />
          <button
            type="button"
            className="btn"
            data-variant="primary"
            onClick={() => setView('new')}
          >
            New campaign
          </button>
        </div>

        <div className="content">
          {error ? (
            <div className="banner" data-tone="bad" role="alert">
              <span>{error}</span>
              <button type="button" className="close" onClick={() => setError(null)}>
                ×
              </button>
            </div>
          ) : null}

          {view === 'new' ? (
            <NewCampaign
              onError={handle}
              onToast={toast}
              onCreated={() => {
                setView('campaign')
                refreshCampaigns()
              }}
              onCancel={() => setView('review')}
            />
          ) : !campaign ? (
            <>
              <h1 className="page-title">No campaign yet</h1>
              <p className="page-note">
                A campaign carries the sender persona and the goal, and every other screen is
                scoped to one. Nothing can be imported, researched or sent until one exists.
              </p>
              <button
                type="button"
                className="btn"
                data-variant="primary"
                onClick={() => setView('new')}
              >
                Create a campaign
              </button>
            </>
          ) : view === 'review' ? (
            <ReviewQueue
              campaignId={campaign.id}
              onError={handle}
              onToast={toast}
              onChanged={() => {
                refreshAttention()
                refreshQueueCount()
              }}
            />
          ) : view === 'leads' ? (
            <Leads campaignId={campaign.id} onError={handle} />
          ) : view === 'import' ? (
            <Import campaignId={campaign.id} onError={handle} onToast={toast} />
          ) : view === 'campaign' ? (
            <CampaignScreen
              campaign={campaign}
              mayAdminister={mayAdminister}
              onError={handle}
              onToast={toast}
              onChanged={(removed) => {
                if (removed) {
                  setCampaignId(null)
                  setView('review')
                }
                refreshCampaigns()
              }}
            />
          ) : view === 'settings' ? (
            <Settings
              campaign={campaign}
              onError={handle}
              onToast={toast}
              onChanged={refreshCampaigns}
            />
          ) : null}
        </div>
      </div>

      <div className="toast-host">
        {toasts.map((t) => (
          <div key={t.id} className="toast" data-kind={t.kind} role="status">
            {t.message}
          </div>
        ))}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ review */

function ReviewQueue({ campaignId, onError, onToast, onChanged }) {
  const [page, setPage] = useState({ drafts: [], total: 0, next_cursor: null })
  const [cursor, setCursor] = useState(null)
  const [trail, setTrail] = useState([])
  const [edits, setEdits] = useState({})
  const [busy, setBusy] = useState(null)

  const load = useCallback(() => {
    const params = new URLSearchParams({ campaign_id: campaignId, limit: String(REVIEW_PAGE) })
    if (cursor) params.set('cursor', cursor)
    getJson(`/review/drafts?${params}`).then(setPage).catch(onError)
  }, [campaignId, cursor, onError])

  useEffect(load, [load])

  // Judging a draft removes it from the queue, so the page refills from the same cursor rather
  // than advancing. Advancing would skip the drafts that moved up into the gap.
  const afterDecision = () => {
    load()
    onChanged()
  }

  const approve = async (item) => {
    setBusy(item.draft_id)
    try {
      const edit = edits[item.draft_id]
      await postJson(`/drafts/${item.draft_id}/approve`, { json: edit ? edit : {} })
      onToast('Approved. The send loop decides when it actually goes.', 'success')
      afterDecision()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(null)
    }
  }

  const reject = async (item) => {
    const feedback = (edits[item.draft_id] || {}).feedback || ''
    if (!feedback.trim()) {
      onToast('Rejecting needs a reason — it is what the next draft is written from.', 'error')
      return
    }
    setBusy(item.draft_id)
    try {
      await postJson(`/drafts/${item.draft_id}/reject`, { json: { feedback } })
      onToast('Rejected. It will be regenerated with your note.', 'info')
      afterDecision()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(null)
    }
  }

  const setEdit = (id, patch) =>
    setEdits((current) => ({ ...current, [id]: { ...(current[id] || {}), ...patch } }))

  const shown = page.drafts.length
  const seen = trail.length * REVIEW_PAGE

  return (
    <>
      <h1 className="page-title">Review queue</h1>
      <p className="page-note">
        Nothing is sent without a decision here. Approving makes a lead eligible; the send loop
        re-checks the suppression list, the window and today&apos;s cap before anything leaves.
      </p>

      <dl className="stat-grid">
        <div className="stat" data-tone={page.total > 0 ? 'warn' : 'good'}>
          <dt>Waiting for a decision</dt>
          <dd className="num">{page.total}</dd>
        </div>
        <div className="stat">
          <dt>On this page</dt>
          <dd className="num">
            {shown ? `${seen + 1}–${seen + shown}` : '—'}
          </dd>
        </div>
      </dl>

      {shown === 0 ? (
        <p className="empty">
          Nothing waiting. Drafts appear here once research and drafting finish.
        </p>
      ) : (
        page.drafts.map((item) => (
          <article key={item.draft_id} className="card">
            <div className="card-head">
              <strong>{item.lead.full_name || item.lead.email}</strong>
              <span className="badge">{item.lead.company_name || 'no company'}</span>
              <span className="badge">{item.kind}</span>
              <span className="badge">v{item.version}</span>
              {item.status === 'validation_failed' ? (
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
                onChange={(event) => setEdit(item.draft_id, { subject: event.target.value })}
              />
            </div>

            <div className="field">
              <label htmlFor={`b-${item.draft_id}`}>Body</label>
              <textarea
                id={`b-${item.draft_id}`}
                rows={11}
                value={(edits[item.draft_id] || {}).body ?? item.body}
                onChange={(event) => setEdit(item.draft_id, { body: event.target.value })}
              />
            </div>

            <Evidence item={item} />

            <div className="field">
              <label htmlFor={`f-${item.draft_id}`}>Reason, if rejecting</label>
              <input
                id={`f-${item.draft_id}`}
                type="text"
                value={(edits[item.draft_id] || {}).feedback || ''}
                onChange={(event) => setEdit(item.draft_id, { feedback: event.target.value })}
                placeholder="Too long; lead with the pipeline rebuild"
              />
            </div>

            <div className="btn-row">
              <button
                type="button"
                className="btn"
                data-variant="primary"
                onClick={() => approve(item)}
                disabled={busy === item.draft_id || item.status === 'validation_failed'}
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
            {item.status === 'validation_failed' ? (
              <p className="muted" style={{ marginTop: 10 }}>
                This draft failed the grounding checks, so it cannot be approved. Reject it with a
                note and it will be rewritten.
              </p>
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
            const back = trail.slice(0, -1)
            setTrail(back)
            setCursor(back.length ? back[back.length - 1] : null)
          }}
        >
          Previous
        </button>
        <button
          type="button"
          className="btn"
          disabled={!page.next_cursor}
          onClick={() => {
            setTrail([...trail, page.next_cursor])
            setCursor(page.next_cursor)
          }}
        >
          Next
        </button>
        <span className="spacer" />
        <span>Oldest first, so nothing waits indefinitely.</span>
      </div>
    </>
  )
}

function Evidence({ item }) {
  const hooks = item.hooks || []
  return (
    <details className="evidence">
      <summary>
        Evidence — {hooks.length} research hook{hooks.length === 1 ? '' : 's'}
        {item.thin_research ? ' · thin' : ''} · confidence{' '}
        {Number(item.research_confidence || 0).toFixed(2)}
      </summary>
      <div className="evidence-body">
        {hooks.length === 0 ? (
          <p className="muted">
            No verified research. Anything specific in this draft has nothing behind it.
          </p>
        ) : (
          hooks.map((hook) => (
            <div key={hook.id || hook.text} className="hook">
              <strong>{hook.id}</strong> — {hook.text}
              {hook.snippet ? <div className="snippet">“{hook.snippet}”</div> : null}
              {hook.source_url ? (
                <div className="muted">
                  <a href={hook.source_url} target="_blank" rel="noreferrer noopener">
                    {hook.source_url}
                  </a>
                </div>
              ) : null}
            </div>
          ))
        )}
      </div>
    </details>
  )
}

/* ------------------------------------------------------------------- leads */

function Leads({ campaignId, onError }) {
  const [page, setPage] = useState({ leads: [], next_cursor: null })
  const [filters, setFilters] = useState({ email: '', name: '', company: '', state: '' })
  const [applied, setApplied] = useState({ email: '', name: '', company: '', state: '' })
  const [cursor, setCursor] = useState(null)
  const [trail, setTrail] = useState([])

  // Typing does not fetch. A request per keystroke is a request per keystroke against a table
  // that will have thousands of rows; the state dropdown applies at once because picking from a
  // list is already a deliberate act.
  useEffect(() => {
    const params = new URLSearchParams({ limit: String(LEADS_PAGE) })
    for (const [key, value] of Object.entries(applied)) {
      if (value.trim()) params.set(key, value.trim())
    }
    if (cursor) params.set('cursor', cursor)
    getJson(`/campaigns/${campaignId}/leads?${params}`).then(setPage).catch(onError)
  }, [campaignId, applied, cursor, onError])

  const apply = (next) => {
    setCursor(null)
    setTrail([])
    setApplied(next)
  }

  const set = (key) => (event) => setFilters({ ...filters, [key]: event.target.value })

  const dirty = Object.keys(filters).some((key) => filters[key] !== applied[key])
  const anyFilter = Object.values(applied).some((value) => value.trim())

  return (
    <>
      <h1 className="page-title">Leads</h1>
      <p className="page-note">
        Email is unique, so it is a search box. Name, company and state are filters, and they
        combine — all of them have to match. Bodies are not searched: a search box over drafts
        and replies would be an export of everything anybody ever wrote.
      </p>

      <form
        className="filters"
        onSubmit={(event) => {
          event.preventDefault()
          apply(filters)
        }}
      >
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-email">Email</label>
          <input id="f-email" type="search" value={filters.email} onChange={set('email')} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-name">Name</label>
          <input id="f-name" type="search" value={filters.name} onChange={set('name')} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-company">Company</label>
          <input id="f-company" type="search" value={filters.company} onChange={set('company')} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="f-state">State</label>
          <select
            id="f-state"
            value={filters.state}
            onChange={(event) => {
              const next = { ...filters, state: event.target.value }
              setFilters(next)
              apply(next)
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
          <button type="submit" className="btn" data-variant={dirty ? 'primary' : undefined}>
            Search
          </button>
          {anyFilter || dirty ? (
            <button
              type="button"
              className="btn"
              data-variant="quiet"
              onClick={() => {
                const empty = { email: '', name: '', company: '', state: '' }
                setFilters(empty)
                apply(empty)
              }}
            >
              Clear
            </button>
          ) : null}
        </div>
      </form>

      {page.leads.length === 0 ? (
        <p className="empty">
          {anyFilter ? 'No lead matches those filters.' : 'No leads yet. Import a sheet to start.'}
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
                <tr key={lead.id}>
                  <td className="mono">{lead.email}</td>
                  <td>{lead.full_name || '—'}</td>
                  <td>{lead.company_name || '—'}</td>
                  <td>
                    <span className="badge" data-tone={stateTone(lead.state)}>
                      {lead.state}
                    </span>
                  </td>
                  <td className="muted">
                    {lead.next_action_at ? new Date(lead.next_action_at).toLocaleString() : '—'}
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
            const back = trail.slice(0, -1)
            setTrail(back)
            setCursor(back.length ? back[back.length - 1] : null)
          }}
        >
          Previous
        </button>
        <button
          type="button"
          className="btn"
          disabled={!page.next_cursor}
          onClick={() => {
            setTrail([...trail, page.next_cursor])
            setCursor(page.next_cursor)
          }}
        >
          Next
        </button>
        <span className="spacer" />
        <span>Newest first.</span>
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ import */

function Import({ campaignId, onError, onToast }) {
  const [preview, setPreview] = useState(null)
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)

  const choose = async (event) => {
    const chosen = event.target.files?.[0]
    event.target.value = ''
    if (!chosen) return
    setFile(chosen)
    setBusy(true)
    try {
      setPreview(await uploadFile(`/campaigns/${campaignId}/imports/preview`, chosen))
    } catch (err) {
      setPreview(null)
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const commit = async () => {
    setBusy(true)
    try {
      const result = await uploadFile(`/campaigns/${campaignId}/imports/commit`, file)
      onToast(`Imported ${result.imported} lead(s). Research is queued.`, 'success')
      setPreview(null)
      setFile(null)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h1 className="page-title">Import leads</h1>
      <p className="page-note">
        The preview writes nothing. Committing re-reads the file and re-checks every row against
        the suppression list, so anyone who opted out between the two is still excluded.
      </p>

      <div className="card">
        <div className="field">
          <label htmlFor="sheet">Spreadsheet (.csv or .xlsx)</label>
          <input id="sheet" type="file" accept=".csv,.xlsx" onChange={choose} disabled={busy} />
          <p className="hint">
            Needs an email column. Name, company and website columns are used for research and
            for the draft; a missing website means the draft has less to stand on.
          </p>
        </div>
      </div>

      {preview ? (
        <div className="card">
          <div className="card-head">
            {Object.entries(preview.counts).map(([verdict, count]) => (
              <span key={verdict} className="badge" data-tone={verdictTone(verdict)}>
                {count} {verdict}
              </span>
            ))}
            {preview.truncated ? (
              <span className="badge" data-tone="warn">
                {preview.truncated} rows beyond the cap were dropped
              </span>
            ) : null}
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Row</th>
                  <th>Email</th>
                  <th>Verdict</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {preview.rows.slice(0, 50).map((row) => (
                  <tr key={row.row_number}>
                    <td className="num">{row.row_number}</td>
                    <td className="mono">{row.email || '—'}</td>
                    <td>
                      <span className="badge" data-tone={verdictTone(row.verdict)}>
                        {row.verdict}
                      </span>
                    </td>
                    <td className="muted">{row.reasons.join('; ') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="btn-row">
            <button
              type="button"
              className="btn"
              data-variant="primary"
              onClick={commit}
              disabled={busy || !preview.can_commit}
            >
              Import the importable rows
            </button>
          </div>
          {!preview.can_commit ? (
            <p className="muted" style={{ marginTop: 10 }}>
              Nothing here can be imported. Check that the sheet has email, name and company
              columns.
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  )
}

/* ---------------------------------------------------------------- campaign */

function CampaignScreen({ campaign, mayAdminister, onError, onToast, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState(null)
  const [showPersona, setShowPersona] = useState(false)
  const [attaching, setAttaching] = useState(false)
  const [mailbox, setMailbox] = useState({ from_name: '', from_email: '' })

  useEffect(() => {
    setDetail(null)
    getJson(`/campaigns/${campaign.id}`).then(setDetail).catch(onError)
  }, [campaign.id, onError])

  const setStatus = async (status) => {
    setBusy(true)
    try {
      await patchJson(`/campaigns/${campaign.id}`, { json: { status } })
      onToast(
        status === 'running'
          ? 'Campaign is running. Approved drafts will send inside the window, under the cap.'
          : `Campaign is now ${status}. Nothing further will send.`
      )
      onChanged(false)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const attachMailbox = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const created = await postJson('/mailboxes', { json: mailbox })
      await patchJson(`/campaigns/${campaign.id}`, { json: { mailbox_id: created.id } })
      onToast('Mailbox attached. Set the campaign running when you are ready.')
      setAttaching(false)
      onChanged(false)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    try {
      const result = await deleteJson(`/campaigns/${campaign.id}`)
      // An archive is not a failure: it is the API refusing to destroy the record of who was
      // contacted, and saying so.
      onToast(result.deleted ? 'Campaign deleted.' : result.detail)
      onChanged(result.deleted)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const counts = detail?.lead_counts || {}
  const persona = detail?.persona || {}

  return (
    <>
      <h1 className="page-title">{campaign.name}</h1>
      <p className="page-note">
        What this campaign is, who it comes from, and whether it is allowed to send.
      </p>

      {campaign.mailbox_id ? null : (
        <div className="banner" data-tone="warn">
          <span>
            This campaign cannot send until a mailbox is attached. It must be an address your SPF
            and DKIM records cover — otherwise mail goes out unauthenticated against a domain
            with no matching records, which is worse than no setup at all.
          </span>
        </div>
      )}

      {campaign.mailbox_id && campaign.status === 'draft' ? (
        <div className="banner" data-tone="info">
          <span>
            This campaign is a draft, so nothing will send even once a draft is approved. Set it
            running when you are ready.
          </span>
        </div>
      ) : null}

      <dl className="stat-grid">
        {Object.entries(counts).length === 0 ? (
          <div className="stat">
            <dt>Leads</dt>
            <dd className="num">0</dd>
          </div>
        ) : (
          Object.entries(counts).map(([state, count]) => (
            <div key={state} className="stat">
              <dt>{state}</dt>
              <dd className="num">{count}</dd>
            </div>
          ))
        )}
      </dl>

      {mayAdminister ? (
        <div className="btn-row">
          {campaign.mailbox_id ? null : (
            <button
              type="button"
              className="btn"
              onClick={() => setAttaching(!attaching)}
              disabled={busy}
            >
              {attaching ? 'Cancel' : 'Attach a mailbox'}
            </button>
          )}
          {campaign.mailbox_id && campaign.status !== 'running' ? (
            <button
              type="button"
              className="btn"
              data-variant="primary"
              disabled={busy}
              onClick={() => setStatus('running')}
            >
              Set running
            </button>
          ) : null}
          {campaign.status === 'running' ? (
            <button type="button" className="btn" disabled={busy} onClick={() => setStatus('paused')}>
              Pause sending
            </button>
          ) : null}
          <button type="button" className="btn" data-variant="danger" disabled={busy} onClick={remove}>
            Delete campaign
          </button>
        </div>
      ) : null}

      {attaching ? (
        <form className="card" onSubmit={attachMailbox} style={{ marginTop: 14 }}>
          <Field
            label="Send from (email)"
            value={mailbox.from_email}
            onChange={(event) => setMailbox({ ...mailbox, from_email: event.target.value })}
            required
            hint="The mailbox your SPF and DKIM cover — the same address as SMTP_EMAIL on the server."
          />
          <Field
            label="Sender name recipients see"
            value={mailbox.from_name}
            onChange={(event) => setMailbox({ ...mailbox, from_name: event.target.value })}
            required
          />
          <div className="btn-row">
            <button type="submit" className="btn" data-variant="primary" disabled={busy}>
              {busy ? 'Attaching…' : 'Attach mailbox'}
            </button>
          </div>
        </form>
      ) : null}

      <h2 className="section-title">Sender persona</h2>
      <p className="muted">
        The only things a draft is allowed to claim about us. Everything else has to come from
        cited research.
      </p>
      <div className="btn-row">
        <button type="button" className="btn" onClick={() => setShowPersona(!showPersona)}>
          {showPersona ? 'Hide persona' : 'Show what this campaign was created with'}
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
                    <td>{key.replace(/_/g, ' ')}</td>
                    <td>
                      {value === null || value === '' ? (
                        <span className="muted">not set</span>
                      ) : Array.isArray(value) ? (
                        value.length === 0 ? (
                          <span className="muted">none</span>
                        ) : (
                          <ul style={{ margin: 0, paddingLeft: 18 }}>
                            {value.map((entry, index) => (
                              <li key={index}>
                                {typeof entry === 'object' ? JSON.stringify(entry) : String(entry)}
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
  )
}

function NewCampaign({ onError, onToast, onCreated, onCancel }) {
  const [form, setForm] = useState({
    name: '',
    sender_name: '',
    sender_role: '',
    agency_name: '',
    offer: '',
    goal: 'book a 20-minute call',
    cta_text: '',
    from_name: '',
    from_email: '',
  })
  const [busy, setBusy] = useState(false)
  const [fieldErrors, setFieldErrors] = useState([])

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setFieldErrors([])
    try {
      // The mailbox first: a campaign can exist without one, but it could never send, and
      // creating them together means one less half-finished state to explain later.
      let mailboxId = null
      if (form.from_email.trim()) {
        const mailbox = await postJson('/mailboxes', {
          json: { from_name: form.from_name || form.sender_name, from_email: form.from_email },
        })
        mailboxId = mailbox.id
      }

      await postJson('/campaigns', {
        json: {
          name: form.name,
          mailbox_id: mailboxId,
          persona: {
            sender_name: form.sender_name,
            sender_role: form.sender_role,
            agency_name: form.agency_name,
            offer: form.offer,
            goal: form.goal,
            cta_text: form.cta_text,
          },
        },
      })
      onToast('Campaign created. It starts as a draft and sends nothing until you say so.')
      onCreated()
    } catch (err) {
      // The API returns which persona field is wrong; showing "invalid" alone would be useless.
      const detail = err?.detail
      if (detail && Array.isArray(detail.errors)) setFieldErrors(detail.errors)
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h1 className="page-title">New campaign</h1>
      <p className="page-note">
        A campaign carries the sender persona and the goal, so one tool serves more than one
        offer. The persona is the list of claims a draft may make about us — it is validated now
        rather than at drafting time, because a malformed one quietly produces weaker drafts.
      </p>

      <form className="card" onSubmit={submit}>
        <Field label="Campaign name" value={form.name} onChange={set('name')} required />

        <h2 className="section-title">Who it comes from</h2>
        <div className="field-grid">
          <Field label="Your name" value={form.sender_name} onChange={set('sender_name')} required />
          <Field
            label="Your role"
            value={form.sender_role}
            onChange={set('sender_role')}
            required
            hint="e.g. Founder"
          />
          <Field
            label="Agency name"
            value={form.agency_name}
            onChange={set('agency_name')}
            required
          />
        </div>

        <h2 className="section-title">What you are offering</h2>
        <Field
          label="Offer"
          value={form.offer}
          onChange={set('offer')}
          required
          hint="One line, specific. This is what every draft is built around."
        />
        <div className="field-grid">
          <Field label="Goal" value={form.goal} onChange={set('goal')} required />
          <Field
            label="Call to action"
            value={form.cta_text}
            onChange={set('cta_text')}
            hint="Optional. The ask at the end of the email."
          />
        </div>

        <h2 className="section-title">Mailbox</h2>
        <p className="muted">
          Optional now, required before anything can send. It must be an address your SPF and
          DKIM records cover.
        </p>
        <div className="field-grid">
          <Field label="Send from (email)" value={form.from_email} onChange={set('from_email')} />
          <Field
            label="Sender name recipients see"
            value={form.from_name}
            onChange={set('from_name')}
            hint="Defaults to your name."
          />
        </div>

        {fieldErrors.length ? (
          <div className="banner" data-tone="bad" style={{ marginTop: 14 }}>
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
          <button type="submit" className="btn" data-variant="primary" disabled={busy}>
            {busy ? 'Creating…' : 'Create campaign'}
          </button>
          <button type="button" className="btn" data-variant="quiet" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </>
  )
}

/* ---------------------------------------------------------------- settings */

function Settings({ campaign, onError, onToast, onChanged }) {
  const [config, setConfig] = useState(null)
  const [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(false)
  const [fieldErrors, setFieldErrors] = useState([])

  useEffect(() => {
    getJson('/admin/sending-config')
      .then(setConfig)
      .catch(onError)
  }, [onError])

  useEffect(() => {
    setDraft(campaign?.settings ? { ...campaign.settings } : null)
    setFieldErrors([])
  }, [campaign?.id, campaign?.settings])

  const set = (key, value) => setDraft({ ...draft, [key]: value })
  const number = (key) => (event) => set(key, Number(event.target.value))

  const toggleDay = (day) => {
    const days = draft?.send_days || []
    set('send_days', days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort())
  }

  const save = async (event) => {
    event.preventDefault()
    setBusy(true)
    setFieldErrors([])
    try {
      await patchJson(`/campaigns/${campaign.id}`, { json: { settings: draft } })
      onToast('Settings saved. The send loop picks them up on its next cycle.', 'success')
      onChanged()
    } catch (err) {
      const detail = err?.detail
      if (detail && Array.isArray(detail.errors)) setFieldErrors(detail.errors)
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <h1 className="page-title">Settings</h1>
      <p className="page-note">
        Sending rules are per campaign, so choosing a different one in the bar above changes what
        is edited here. Only an owner or admin can open this screen, and the API checks the role
        as well — a hidden button is not a permission.
      </p>

      <h2 className="section-title">Sending — set on the server</h2>
      <p className="muted">
        These come from the server environment and are checked in one place in the code. There is
        no switch for them here on purpose: turning sending on is an act on the server, so a
        stolen session cannot start mailing real people.
      </p>

      {config ? (
        <div className="card" style={{ marginTop: 12 }}>
          <table className="kv">
            <tbody>
              <tr>
                <td>Send mode</td>
                <td>
                  <span className="badge" data-tone={modeTone(config.send_mode)}>
                    {config.send_mode}
                  </span>{' '}
                  <span className="muted">{modeExplanation(config)}</span>
                </td>
              </tr>
              <tr>
                <td>Sending account</td>
                <td className="mono">{config.sending_account}</td>
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
                  Every first email and every reply is reviewed by a human · plain text only · no
                  tracking pixels · no link rewriting · a meeting is only ever marked booked by a
                  person
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">Loading…</p>
      )}

      <h2 className="section-title">Sending rules for {campaign.name}</h2>
      <p className="muted">
        When and how fast this campaign may send. Saved through the same validation the worker
        uses, so a value this form accepts is a value the worker can read.
      </p>

      {draft ? (
        <form className="card" onSubmit={save} style={{ marginTop: 12 }}>
          <span className="form-label">Send days</span>
          <div className="day-row">
            {WEEKDAYS.map(([day, name]) => {
              const on = (draft.send_days || []).includes(day)
              return (
                <label key={day} className="day" data-on={on}>
                  <input type="checkbox" checked={on} onChange={() => toggleDay(day)} />
                  {name}
                </label>
              )
            })}
          </div>

          <div className="field-grid">
            <div className="field">
              <label htmlFor="ws">Window opens ({draft.timezone})</label>
              <input
                id="ws"
                type="time"
                step="1"
                value={draft.window_start || ''}
                onChange={(event) => set('window_start', event.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="we">Window closes ({draft.timezone})</label>
              <input
                id="we"
                type="time"
                step="1"
                value={draft.window_end || ''}
                onChange={(event) => set('window_end', event.target.value)}
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
              value={draft.daily_cap ?? ''}
              onChange={number('daily_cap')}
            />
            <p className="hint">
              Counts every outbound email, including follow-ups and replies. At steady state each
              lead costs two sends, so 100 is roughly 50 new leads a day plus 50 follow-ups.
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
                value={draft.followup_after_days ?? ''}
                onChange={number('followup_after_days')}
              />
            </div>
            <div className="field">
              <label htmlFor="mf">Maximum follow-ups</label>
              <input
                id="mf"
                type="number"
                min="0"
                max="5"
                value={draft.max_followups ?? ''}
                onChange={number('max_followups')}
              />
              <p className="hint">0 means none. Any reply, bounce or opt-out cancels them.</p>
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
            <button type="submit" className="btn" data-variant="primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save settings'}
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

      <h2 className="section-title">Suppression list</h2>
      <Suppressions onError={onError} onToast={onToast} />
    </>
  )
}

function Suppressions({ onError, onToast }) {
  const [rows, setRows] = useState([])
  const [value, setValue] = useState('')

  const load = useCallback(() => {
    getJson('/suppressions')
      .then((data) => setRows(data.suppressions))
      .catch(onError)
  }, [onError])

  useEffect(load, [load])

  const add = async (event) => {
    event.preventDefault()
    try {
      await postJson('/suppressions', { json: { value, reason: 'manual' } })
      setValue('')
      onToast('Added. Nothing will be sent to that address again.', 'success')
      load()
    } catch (err) {
      onError(err)
    }
  }

  const remove = async (id) => {
    try {
      await deleteJson(`/suppressions/${id}`)
      load()
    } catch (err) {
      onError(err)
    }
  }

  return (
    <>
      <p className="muted">
        Checked immediately before every send. An address erased on request is kept here as a
        hash, so a later import of the same sheet still cannot reach them.
      </p>

      <form className="card" onSubmit={add} style={{ marginTop: 12 }}>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="sup">Never contact</label>
          <input
            id="sup"
            type="email"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="someone@example.com"
          />
        </div>
        <div className="btn-row">
          <button type="submit" className="btn" disabled={!value.trim()}>
            Suppress
          </button>
        </div>
      </form>

      {rows.length === 0 ? (
        <p className="empty" style={{ marginTop: 14 }}>
          Nobody suppressed yet.
        </p>
      ) : (
        <div className="table-wrap" style={{ marginTop: 14 }}>
          <table>
            <thead>
              <tr>
                <th>Value</th>
                <th>Kind</th>
                <th>Reason</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td className="mono">{row.value}</td>
                  <td>
                    <span className="badge">{row.kind}</span>
                  </td>
                  <td>{row.reason}</td>
                  <td>
                    <button
                      type="button"
                      className="btn"
                      data-variant="quiet"
                      onClick={() => remove(row.id)}
                    >
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ bits */

function Field({ label, value, onChange, required, hint, type = 'text' }) {
  const id = `f-${label.replace(/\W+/g, '-').toLowerCase()}`
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {required ? null : <span className="muted"> (optional)</span>}
      </label>
      <input id={id} type={type} value={value} onChange={onChange} required={required} />
      {hint ? <p className="hint">{hint}</p> : null}
    </div>
  )
}

function Centered({ children }) {
  return <div className="centered">{children}</div>
}

function statusTone(campaign) {
  if (campaign.paused) return 'warn'
  if (campaign.status === 'running') return 'good'
  if (campaign.status === 'archived') return 'bad'
  return undefined
}

function stateTone(state) {
  if (['SENT', 'REPLIED', 'MEETING_BOOKED'].includes(state)) return 'good'
  if (['BOUNCED', 'ERROR'].includes(state)) return 'bad'
  if (['OPTED_OUT', 'SKIPPED', 'NO_RESPONSE'].includes(state)) return 'warn'
  if (state === 'AWAITING_APPROVAL') return 'info'
  return undefined
}

function verdictTone(verdict) {
  if (verdict === 'importable' || verdict === 'ok') return 'good'
  if (verdict === 'duplicate' || verdict === 'suppressed') return 'warn'
  if (verdict === 'invalid' || verdict === 'rejected') return 'bad'
  return undefined
}

function modeTone(mode) {
  if (mode === 'live') return 'bad'
  if (mode === 'allowlist') return 'warn'
  return undefined
}

function modeExplanation(config) {
  if (config.send_mode === 'disabled') return 'nothing can be sent, to anybody'
  if (config.send_mode === 'allowlist') {
    return `only the ${config.allowlist_size} allowlisted address(es)`
  }
  return 'any recipient — real mail goes to real people'
}
