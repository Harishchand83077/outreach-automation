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
  putJson,
  postJson,
  uploadFile,
} from './api'
import { signOut } from './supabase'
import {
  ActivityChart,
  CommandPalette,
  Drawer,
  Empty,
  Icons,
  SkeletonList,
  ThemeToggle,
  usePaletteShortcut,
  useTheme,
} from './ui'
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
  const [theme, toggleTheme] = useTheme()
  const [paletteOpen, setPaletteOpen] = usePaletteShortcut()
  const [me, setMe] = useState(null)
  const [error, setError] = useState(null)
  const [toasts, setToasts] = useState([])
  const [campaigns, setCampaigns] = useState([])
  //: **"Empty" and "not loaded yet" are different answers and must look different.** The list
  //: starts `[]`, which is indistinguishable from "this org has no campaigns" -- so for the
  //: half-second before the fetch returned, the screen said "No campaign yet" to somebody with
  //: two campaigns. A loading state that states a falsehood is worse than a spinner.
  const [campaignsLoaded, setCampaignsLoaded] = useState(false)
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
      // Loaded means "we asked and heard back", including hearing an error -- otherwise a
      // failed fetch leaves the screen on a skeleton for ever with the real error in a banner
      // above it.
      .finally(() => setCampaignsLoaded(true))
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
  const isOperator = me ? me.platform_operator === true : false

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

  if (!me) {
    return (
      <div className="content" aria-busy="true">
        <div className="skeleton" style={{ height: 30, width: 220, marginBottom: 18 }} />
        <SkeletonList count={2} rows={4} />
      </div>
    )
  }

  const nav = [
    { id: 'review', label: 'Review queue', icon: Icons.review, count: queueCount ?? undefined },
    { id: 'leads', label: 'Leads', icon: Icons.leads },
    { id: 'import', label: 'Import', icon: Icons.import },
    { id: 'campaign', label: 'Campaign', icon: Icons.campaign },
    ...(mayAdminister
      ? [
          { id: 'admin', label: 'Admin', icon: Icons.inbox },
          { id: 'settings', label: 'Settings', icon: Icons.settings },
        ]
      : []),
    // The scope above orgs (ADR-024): granted by hand, held by almost nobody. Drawn from `/me`
    // rather than by calling the route and reading a 403, which would log an error on every
    // load for every ordinary user.
    ...(isOperator ? [{ id: 'platform', label: 'Platform', icon: Icons.platform }] : []),
  ]

  const who = me.email || session.user?.email || ''

  // Every screen and every campaign, two keystrokes away. Built from the same `nav` list the
  // sidebar draws, so a screen added later appears here without anybody remembering to add it.
  const commands = [
    ...nav.map((item) => ({
      id: `view:${item.id}`,
      group: 'Go to',
      label: item.label,
      icon: item.icon,
      run: () => setView(item.id),
    })),
    ...campaigns.map((row) => ({
      id: `campaign:${row.id}`,
      group: 'Switch campaign',
      label: row.name,
      icon: Icons.campaign,
      hint: row.paused ? 'paused' : row.status,
      run: () => {
        setCampaignId(row.id)
        setView('campaign')
      },
    })),
    {
      id: 'action:new',
      group: 'Actions',
      label: 'New campaign',
      icon: Icons.plus,
      run: () => setView('new'),
    },
    {
      id: 'action:theme',
      group: 'Actions',
      label: 'Toggle light and dark',
      icon: Icons.sun,
      run: toggleTheme,
    },
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
            <item.icon className="nav-icon" size={17} />
            {item.label}
            {item.count === undefined ? null : (
              <span className="count" data-zero={item.count === 0}>
                {item.count}
              </span>
            )}
          </button>
        ))}

        <div className="sidebar-foot">
          <div className="sidebar-user">
            <span className="avatar" aria-hidden="true">
              {who.slice(0, 1) || '?'}
            </span>
            <span className="sidebar-identity">
              <strong title={who}>{who}</strong>
              <span>{me.role}</span>
            </span>
          </div>
          <button
            type="button"
            className="btn"
            data-variant="quiet"
            data-size="sm"
            style={{ width: '100%', marginTop: 6 }}
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
          >
            {campaigns.length === 0 ? (
              <option value="">{campaignsLoaded ? 'No campaigns yet' : 'Loading…'}</option>
            ) : null}
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
            data-size="sm"
            data-variant="quiet"
            onClick={() => setPaletteOpen(true)}
            title="Search (Ctrl K)"
          >
            <Icons.search size={15} />
            <span className="kbd">⌘K</span>
          </button>
          <ThemeToggle theme={theme} onToggle={toggleTheme} />
          <button
            type="button"
            className="btn"
            data-variant="primary"
            onClick={() => setView('new')}
          >
            <Icons.plus size={15} />
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
          ) : !campaign && !campaignsLoaded ? (
            <>
              <div className="skeleton" style={{ height: 30, width: 240, marginBottom: 18 }} />
              <SkeletonList count={2} rows={3} />
            </>
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
            <Leads campaignId={campaign.id} onError={handle} onToast={toast} />
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
          ) : view === 'platform' ? (
            <Platform onError={handle} />
          ) : view === 'admin' ? (
            <Admin onError={handle} onToast={toast} />
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

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        commands={commands}
      />

      <div className="toast-host">
        {toasts.map((item) => (
          <div key={item.id} className="toast" data-kind={item.kind} role="status">
            <span className="toast-icon">
              {item.kind === 'success' ? (
                <Icons.check size={16} />
              ) : item.kind === 'error' ? (
                <Icons.alert size={16} />
              ) : (
                <Icons.info size={16} />
              )}
            </span>
            <span>{item.message}</span>
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
  const [selected, setSelected] = useState([])
  const [skipped, setSkipped] = useState({})
  //: Which card the keyboard is on. Reviewing a hundred drafts with a mouse is a hundred
  //: round trips to a small button; J/K and A/R is the difference between a queue somebody
  //: works through and one they put off.
  const [focused, setFocused] = useState(0)

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

  // Whether the reviewer has changed anything. Used to decide if a draft that failed
  // validation may be approved: the check objected to something concrete in *this* text, so
  // approving it untouched is a reviewer clicking past a warning rather than answering it.
  const hasEdit = (item) => {
    const edit = edits[item.draft_id] || {}
    const subject = edit.subject
    const body = edit.body
    return (
      (subject !== undefined && subject !== item.subject) ||
      (body !== undefined && body !== item.body)
    )
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

  const toggle = (id) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((value) => value !== id) : [...current, id]
    )

  // Only drafts that passed validation can be selected. The ones that failed are exactly the
  // ones worth reading, and the API refuses them anyway — disabling the box says so before the
  // click rather than after it.
  const selectable = page.drafts
    .filter((item) => item.status !== 'validation_failed')
    .map((item) => item.draft_id)

  // The reviewer's attachment choices, keyed by draft, applied over whatever the server last
  // said. Without this the box would snap back for the moment between the click and the
  // refetch, which reads as the click not having worked.
  const [attachOverrides, setAttachOverrides] = useState({})

  const toggleAttach = async (item, next) => {
    setAttachOverrides((current) => ({ ...current, [item.draft_id]: next }))
    try {
      await postJson(`/drafts/${item.draft_id}/attach`, { json: { attach: next } })
    } catch (err) {
      // Put it back. A control that stays where the operator left it after the server refused
      // is a control that lies about what will be sent.
      setAttachOverrides((current) => ({ ...current, [item.draft_id]: !next }))
      onError(err)
    }
  }

  const bulkApprove = async () => {
    setBusy('bulk')
    try {
      const result = await postJson('/review/bulk-approve', { json: { draft_ids: selected } })
      setSkipped(result.skipped || {})
      const held = Object.keys(result.skipped || {}).length
      onToast(
        held
          ? `Approved ${result.approved.length}. ${held} still need you — see the notes below.`
          : `Approved ${result.approved.length}.`,
        held ? 'info' : 'success'
      )
      setSelected([])
      afterDecision()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(null)
    }
  }

  const shown = page.drafts.length
  const seen = trail.length * REVIEW_PAGE

  // Keyboard review. Deliberately *not* active while typing: an editor fixing a subject line
  // must be able to write "approach" without approving three drafts on the way through.
  useEffect(() => {
    const onKey = (event) => {
      const tag = event.target?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || event.target?.isContentEditable) return
      if (event.metaKey || event.ctrlKey || event.altKey) return

      const item = page.drafts[focused]
      const key = event.key.toLowerCase()

      if (key === 'j' || key === 'arrowdown') {
        event.preventDefault()
        setFocused((current) => Math.min(current + 1, page.drafts.length - 1))
      } else if (key === 'k' || key === 'arrowup') {
        event.preventDefault()
        setFocused((current) => Math.max(current - 1, 0))
      } else if (key === 'a' && item) {
        event.preventDefault()
        approve(item)
      } else if (key === 'r' && item) {
        event.preventDefault()
        reject(item)
      } else if (key === 'x' && item) {
        event.preventDefault()
        toggle(item.draft_id)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // A page that shrank under the cursor must not leave the focus past its end.
  useEffect(() => {
    setFocused((current) => Math.min(current, Math.max(page.drafts.length - 1, 0)))
  }, [page.drafts.length])

  // Keep the focused card on screen when the keyboard moves it, not when a mouse does.
  useEffect(() => {
    const node = document.querySelector('.card[data-active="true"]')
    node?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [focused])

  return (
    <>
      <h1 className="page-title">Review queue</h1>
      <p className="page-note">
        Nothing is sent without a decision here. Approving makes a lead eligible; the send loop
        re-checks the suppression list, the window and today&apos;s cap before anything leaves.
      </p>

      <div className="queue-head">
        <span className="badge" data-tone={page.total > 0 ? 'warn' : 'good'}>
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
              <span style={{ width: `${page.total ? (seen / page.total) * 100 : 0}%` }} />
            </span>
          </>
        ) : null}
        <span className="spacer" />
        <span className="muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span className="kbd">J</span>
          <span className="kbd">K</span>
          move
          <span className="kbd">A</span>
          approve
          <span className="kbd">R</span>
          reject
        </span>
      </div>

      {shown === 0 ? null : (
        <div className="filters" style={{ gridTemplateColumns: '1fr auto auto' }}>
          <span className="muted">
            {selected.length
              ? `${selected.length} selected`
              : 'Select drafts to approve several at once. Weak ones are held back, not waved through.'}
          </span>
          <button
            type="button"
            className="btn"
            onClick={() => setSelected(selected.length === selectable.length ? [] : selectable)}
          >
            {selected.length === selectable.length && selectable.length
              ? 'Clear selection'
              : 'Select all on this page'}
          </button>
          <button
            type="button"
            className="btn"
            data-variant="primary"
            disabled={!selected.length || busy === 'bulk'}
            onClick={bulkApprove}
          >
            {busy === 'bulk' ? 'Approving…' : `Approve ${selected.length || ''}`.trim()}
          </button>
        </div>
      )}

      {shown === 0 ? (
        <Empty icon={Icons.check} title="Nothing waiting">
          Every draft for this campaign has been judged. New ones appear here once research and
          drafting finish — or straight away for a template campaign.
        </Empty>
      ) : (
        page.drafts.map((item, index) => (
          <article
            key={item.draft_id}
            className="card"
            data-interactive="true"
            data-active={index === focused}
            onMouseEnter={() => setFocused(index)}
          >
            <div className="card-head">
              <input
                type="checkbox"
                aria-label={`Select the draft for ${item.lead.email}`}
                checked={selected.includes(item.draft_id)}
                disabled={item.status === 'validation_failed'}
                onChange={() => toggle(item.draft_id)}
                style={{ width: 'auto', margin: 0 }}
              />
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
                  Attach <strong>{item.attachment_filename}</strong> to this email
                </span>
              </label>
            ) : null}

            <div className="btn-row">
              <button
                type="button"
                className="btn"
                data-variant="primary"
                onClick={() => approve(item)}
                disabled={
                  busy === item.draft_id ||
                  (item.status === 'validation_failed' && !hasEdit(item))
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
            {item.status === 'validation_failed' ? (
              <p className="muted" style={{ marginTop: 10 }}>
                This draft failed the checks above, so it cannot be sent as it stands. Reject it
                with a note and it will be written again, or edit the text yourself — Approve
                becomes available once you have changed something.
              </p>
            ) : null}
            {skipped[item.draft_id] ? (
              <div className="banner" data-tone="warn" style={{ marginTop: 10 }}>
                <span>Held back from the bulk approval: {skipped[item.draft_id]}</span>
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

function Leads({ campaignId, onError, onToast }) {
  const [page, setPage] = useState({ leads: [], next_cursor: null })
  const [filters, setFilters] = useState({ email: '', name: '', company: '', state: '' })
  const [applied, setApplied] = useState({ email: '', name: '', company: '', state: '' })
  const [cursor, setCursor] = useState(null)
  const [trail, setTrail] = useState([])
  const [openLead, setOpenLead] = useState(null)
  const [reload, setReload] = useState(0)

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
  }, [campaignId, applied, cursor, onError, reload])

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
                <tr
                  key={lead.id}
                  onClick={() => setOpenLead(lead.id)}
                  style={{ cursor: 'pointer' }}
                >
                  <td className="mono">{lead.email}</td>
                  <td>{lead.full_name || '—'}</td>
                  <td>{lead.company_name || '—'}</td>
                  <td>
                    <span className="badge" data-tone={stateTone(lead.state)}>
                      {lead.state}
                    </span>
                    {lead.paused ? (
                      <>
                        {' '}
                        <span className="badge" data-tone="warn">
                          paused
                        </span>
                      </>
                    ) : null}
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
        <span>Newest first. Select a row to see the full history.</span>
      </div>

      {openLead ? (
        <LeadDetail
          leadId={openLead}
          onClose={() => setOpenLead(null)}
          onError={onError}
          onToast={onToast}
          onChanged={() => setReload((n) => n + 1)}
        />
      ) : null}
    </>
  )
}

function LeadDetail({ leadId, onClose, onError, onToast, onChanged }) {
  const [lead, setLead] = useState(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    getJson(`/leads/${leadId}`).then(setLead).catch(onError)
  }, [leadId, onError])

  useEffect(load, [load])

  const setPaused = async (paused) => {
    setBusy(true)
    try {
      await postJson(`/leads/${leadId}/${paused ? 'pause' : 'resume'}`)
      onToast(
        paused
          ? 'Paused. Nothing further is sent to this lead, and it keeps its place.'
          : 'Resumed from exactly where it was.',
        'success'
      )
      load()
      onChanged()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  if (!lead) {
    return (
      <Drawer title="Loading…" onClose={onClose}>
        <SkeletonList count={2} rows={4} />
      </Drawer>
    )
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
            <td>{lead.company_name || '—'}</td>
          </tr>
          <tr>
            <td>Next action</td>
            <td className="muted">
              {lead.next_action_at ? new Date(lead.next_action_at).toLocaleString() : '—'}
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
            <strong>This {lead.send_block.kind} email is not going out.</strong>{' '}
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
          {lead.paused ? 'Resume this lead' : 'Pause this lead'}
        </button>
      </div>

      <h3 className="section-title">Messages</h3>
      {(lead.messages || []).length === 0 ? (
        <p className="muted">Nothing sent or received yet.</p>
      ) : (
        (lead.messages || []).map((message) => (
          <details key={message.id} className="evidence">
            <summary>
              {message.direction === 'out' ? 'Sent' : 'Received'} · {message.status} ·{' '}
              {message.subject || '(no subject)'} ·{' '}
              {new Date(message.created_at).toLocaleString()}
            </summary>
            <div className="evidence-body">
              {/* Plain text, never dangerouslySetInnerHTML: this is a lead's own words. */}
              <pre style={{ whiteSpace: 'pre-wrap', margin: 0, fontSize: 13 }}>
                {message.body_text}
              </pre>
            </div>
          </details>
        ))
      )}

      <h3 className="section-title">Timeline</h3>
      <p className="muted">
        The append-only audit trail — what happened to this lead, in order, and who caused it.
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
                  <td className="muted">{new Date(event.at).toLocaleString()}</td>
                  <td>{event.type}</td>
                  <td className="muted">{event.actor || '—'}</td>
                  <td className="muted mono">
                    {event.data && Object.keys(event.data).length
                      ? JSON.stringify(event.data)
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Drawer>
  )
}

/* ------------------------------------------------------------------ import */

function Import({ campaignId, onError, onToast }) {
  const [preview, setPreview] = useState(null)
  const [file, setFile] = useState(null)
  const [busy, setBusy] = useState(false)
  //: The last import, kept so it can be undone. Only in this component's state on purpose: an
  //: undo offered for ever would eventually be pressed long after the leads were worked on, and
  //: the API refuses to remove anything already emailed regardless.
  const [lastImport, setLastImport] = useState(null)
  const [dragging, setDragging] = useState(false)

  // One path for a file however it arrives: picked, or dropped.
  const loadPreview = async (chosen) => {
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

  const choose = (event) => {
    const chosen = event.target.files?.[0]
    // Cleared so that picking the *same* file again fires a change event. Without it, a
    // cancelled import cannot be restarted with the file it was cancelled on.
    event.target.value = ''
    loadPreview(chosen)
  }

  // Dropping a sheet on the page is how anybody who has used a spreadsheet expects to start.
  // The file input stays: a drop target on its own is unreachable from a keyboard.
  const onDrop = (event) => {
    event.preventDefault()
    setDragging(false)
    loadPreview(event.dataTransfer?.files?.[0])
  }

  const commit = async () => {
    setBusy(true)
    try {
      const result = await uploadFile(`/campaigns/${campaignId}/imports/commit`, file)
      onToast(`Imported ${result.imported} lead(s). Research is queued.`, 'success')
      setLastImport({
        batchId: result.batch_id,
        imported: result.imported,
        name: file.name,
      })
      setPreview(null)
      setFile(null)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const cancel = () => {
    setPreview(null)
    setFile(null)
    // The file input keeps its value after a cancel, so choosing the *same* file again would
    // fire no change event and look like the button was broken.
    const input = document.getElementById('sheet')
    if (input) input.value = ''
  }

  const undo = async () => {
    if (!lastImport?.batchId) return
    setBusy(true)
    try {
      const result = await deleteJson(
        `/campaigns/${campaignId}/imports/${lastImport.batchId}`
      )
      onToast(result.message, result.removed ? 'success' : undefined)
      setLastImport(null)
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
        {/* The drop target is a label wrapping the real input, so clicking it opens the picker
            and a keyboard reaches it through the input's own focus — a div with an onDrop is
            invisible to anybody not using a mouse. */}
        <label
          className="dropzone"
          htmlFor="sheet"
          data-over={dragging}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <span className="empty-icon">
            <Icons.import size={20} />
          </span>
          <strong>{dragging ? 'Drop it here' : 'Drop a sheet, or choose a file'}</strong>
          <span className="muted">.csv or .xlsx, up to 5 MB and 5,000 rows</span>
          <input
            id="sheet"
            type="file"
            accept=".csv,.xlsx"
            onChange={choose}
            disabled={busy}
            style={{ position: 'absolute', width: 1, height: 1, opacity: 0, pointerEvents: 'none' }}
          />
        </label>
        <p className="hint">
          Needs an email column. Name, company and website columns are used for research and for
          the draft; a missing website means the draft has less to stand on.
        </p>
      </div>

      {lastImport && !preview ? (
        <div className="banner" data-tone="info">
          <span>
            Imported {lastImport.imported} lead(s) from <strong>{lastImport.name}</strong>. Wrong
            sheet?{' '}
            <button
              type="button"
              className="btn"
              onClick={undo}
              disabled={busy}
              style={{ marginLeft: 6 }}
            >
              Undo this import
            </button>
            <span className="muted" style={{ display: 'block', marginTop: 6 }}>
              Removes the leads this upload created, along with their research and drafts.
              Nothing is added to the suppression list — nobody here asked not to be contacted —
              so the corrected sheet imports normally. A lead that has already been emailed is
              kept, and you will be told how many.
            </span>
          </span>
        </div>
      ) : null}

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
            <button type="button" className="btn" onClick={cancel} disabled={busy}>
              Cancel
            </button>
          </div>
          <p className="muted" style={{ marginTop: 8 }}>
            Nothing has been written yet. Cancelling forgets this file entirely.
          </p>
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

/** An ISO instant as `datetime-local` wants it: the viewer's own clock, no timezone suffix.
 *  Done by hand rather than with `toISOString`, which would render UTC and show somebody in
 *  India a start time seven hours earlier than the one they chose. */
function toLocalInput(iso) {
  if (!iso) return ''
  const when = new Date(iso)
  if (Number.isNaN(when.getTime())) return ''
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}` +
    `T${pad(when.getHours())}:${pad(when.getMinutes())}`
  )
}

/** Bytes as a person reads them. Sizes here are always between 1 KB and 5 MB, so two units
 *  and one decimal place covers every case the UI can actually show. */
function formatBytes(bytes) {
  if (!bytes) return '0 KB'
  const mb = bytes / (1024 * 1024)
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`
}

/* ---------------------------------------------------------------- campaign */

// Named for the person reading them, not for the protocol. "imap" on screen is a reason to ask
// somebody what IMAP is; "receiving replies" is the thing that actually stops working.
const CHECK_LABELS = {
  smtp: 'Sending',
  imap: 'Receiving replies',
  test_message: 'Test email',
}

function CampaignScreen({ campaign, mayAdminister, onError, onToast, onChanged }) {
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState(null)
  const [showPersona, setShowPersona] = useState(false)
  const [attaching, setAttaching] = useState(false)
  const [file, setFile] = useState(null)
  const [fileBusy, setFileBusy] = useState(false)
  const [conn, setConn] = useState(null)
  // The last connection check. Cleared whenever the mailbox changes, because a result about a
  // different mailbox sitting under this one is worse than no result at all.
  const [checks, setChecks] = useState(null)
  const [mailboxes, setMailboxes] = useState([])
  const [schedule, setSchedule] = useState({ starts_at: '', ends_at: '' })
  const [mailbox, setMailbox] = useState({ from_name: '', from_email: '' })

  const [stats, setStats] = useState(null)
  const [activity, setActivity] = useState(null)

  useEffect(() => {
    setDetail(null)
    setStats(null)
    setActivity(null)
    setFile(null)
    setConn(null)
    setSchedule({
      starts_at: toLocalInput(campaign.starts_at),
      ends_at: toLocalInput(campaign.ends_at),
    })
    getJson(`/campaigns/${campaign.id}`).then(setDetail).catch(onError)
    getJson(`/campaigns/${campaign.id}/stats`).then(setStats).catch(onError)
    // `null` means not loaded; `{ attachment: null }` means loaded and there is none. The
    // difference matters: a screen that says "no file attached" before it has asked is a screen
    // stating something it does not know.
    getJson(`/campaigns/${campaign.id}/attachment`)
      .then(setFile)
      .catch(() => setFile({ attachment: null }))
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
        : '/connect/google/status'
    )
      .then(setConn)
      .catch(() => setConn({ configured: false, connected: false }))
    setChecks(null)
    // Every sending account this workspace holds, so an existing campaign can be switched to one
    // instead of its address being typed again into a second mailbox row.
    getJson('/mailboxes')
      .then((data) => setMailboxes(data.mailboxes))
      .catch(() => setMailboxes([]))
    // A chart is a nicety, so a failure to load one must not put a red banner over a screen
    // whose numbers are all fine. It simply does not draw.
    getJson(`/campaigns/${campaign.id}/activity`)
      .then(setActivity)
      .catch(() => setActivity({ series: [] }))
    // `mailbox_id` is a dependency because the connect panel is about that mailbox: attaching a
    // different one has to re-ask, or the panel describes the mailbox that was there before.
  }, [campaign.id, campaign.mailbox_id, onError])

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

  const connectGmail = async () => {
    setBusy(true)
    try {
      // The same id the status call asked about, so what gets connected is what was displayed.
      // Sending `{}` connected whichever mailbox happened to be oldest in the org.
      const { authorize_url } = await postJson('/connect/google/start', {
        json: campaign.mailbox_id ? { mailbox_id: campaign.mailbox_id } : {},
      })
      // A full navigation, not a popup: Google blocks its consent screen inside an iframe, and
      // a popup is the thing browsers and extensions are most likely to swallow silently.
      window.location.href = authorize_url
    } catch (err) {
      onError(err)
      setBusy(false)
    }
  }

  const checkConnection = async (sendTest) => {
    setBusy(true)
    try {
      const report = await postJson('/connect/google/verify', {
        json: {
          ...(campaign.mailbox_id ? { mailbox_id: campaign.mailbox_id } : {}),
          send_test: Boolean(sendTest),
        },
      })
      setChecks(report)
      // The toast says the headline; the per-check detail stays on screen, because "it failed"
      // without saying which half failed is the thing that sent people looking in the wrong
      // place -- a mailbox that sends but cannot be read looks perfectly healthy.
      onToast(
        report.ok
          ? 'This mailbox can send and be read.'
          : 'Something about this connection is not working. See the details below.'
      )
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const useMailbox = async (mailboxId) => {
    setBusy(true)
    try {
      await patchJson(`/campaigns/${campaign.id}`, { json: { mailbox_id: mailboxId } })
      onToast('This campaign now sends from that account.')
      onChanged(false)
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const disconnectGmail = async () => {
    setBusy(true)
    try {
      await deleteJson(`/connect/google/${conn.mailbox_id}`)
      setConn({ ...conn, connected: false, needs_reconnect: false })
      onToast('Disconnected. Revoke access at myaccount.google.com/permissions too.')
      onChanged()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const saveSchedule = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      await putJson(`/campaigns/${campaign.id}/schedule`, {
        json: {
          starts_at: schedule.starts_at ? new Date(schedule.starts_at).toISOString() : null,
          ends_at: schedule.ends_at ? new Date(schedule.ends_at).toISOString() : null,
        },
      })
      onToast('Schedule saved.')
      onChanged()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const chooseFile = async (event) => {
    const chosen = event.target.files?.[0]
    // Cleared so that picking the *same* file again fires a change event — otherwise a failed
    // upload cannot be retried with the file it failed on.
    event.target.value = ''
    if (!chosen) return
    setFileBusy(true)
    try {
      const result = await uploadFile(
        `/campaigns/${campaign.id}/attachment`,
        chosen,
        { method: 'PUT' },
      )
      setFile(result)
      onToast(`${result.attachment.filename} will be attached to this campaign's emails.`)
    } catch (err) {
      onError(err)
    } finally {
      setFileBusy(false)
    }
  }

  const removeFile = async () => {
    setFileBusy(true)
    try {
      await deleteJson(`/campaigns/${campaign.id}/attachment`)
      setFile({ attachment: null })
      onToast('Attachment removed. Emails already sent are unchanged.')
    } catch (err) {
      onError(err)
    } finally {
      setFileBusy(false)
    }
  }

  const attachMailbox = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      // The API is idempotent on the address, so posting one that already exists returns it
      // rather than failing. Say which happened: silently reusing a mailbox somebody thought
      // they were creating is the kind of surprise that gets noticed much later.
      const created = await postJson('/mailboxes', { json: mailbox })
      await patchJson(`/campaigns/${campaign.id}`, { json: { mailbox_id: created.id } })
      onToast(
        created.created
          ? 'Mailbox created and attached. Set the campaign running when you are ready.'
          : `Attached the existing mailbox for ${created.from_email}.`
      )
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

      {/* Every reason an approved draft would sit still, computed by the API from the same
          models the send loop validates with. These used to be two hand-written banners here
          covering the two causes somebody had thought of — so a campaign whose persona the
          worker could not read showed a clean screen, a running status and approved drafts,
          and the reason existed only in the worker log. */}
      {detail?.blockers?.length ? (
        <div className="banner" data-tone="warn">
          <span>
            <strong>Nothing will send from this campaign yet.</strong>
            <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
              {detail.blockers.map((blocker) => (
                <li key={blocker.code}>{blocker.message}</li>
              ))}
            </ul>
            {detail.blockers.some((b) => b.code === 'no_mailbox') ? (
              <span className="muted">
                A mailbox must be an address your SPF and DKIM records cover — otherwise mail
                goes out unauthenticated against a domain with no matching records, which is
                worse than no setup at all.
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
      <h2 className="section-title" style={{ marginTop: 'var(--space-5)' }}>
        Volume
      </h2>
      <dl className="stat-grid">
        <div className="stat">
          <dt>Leads</dt>
          <dd className="num">{stats ? stats.leads_total : '—'}</dd>
        </div>
        <div className="stat">
          <dt>Emails sent</dt>
          <dd className="num">{stats ? stats.emails_sent : '—'}</dd>
        </div>
        <div className="stat">
          <dt>People emailed</dt>
          <dd className="num">{stats ? stats.leads_emailed : '—'}</dd>
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

      <h2 className="section-title">Outcomes</h2>
      <dl className="stat-grid">
        <div className="stat" data-tone={stats && stats.replied ? 'good' : undefined}>
          <dt>Replied</dt>
          <dd className="num">{stats ? stats.replied : '—'}</dd>
        </div>
        <div className="stat" data-tone={stats && stats.meetings_booked ? 'good' : undefined}>
          <dt>Meetings booked</dt>
          <dd className="num">{stats ? stats.meetings_booked : '—'}</dd>
        </div>
        <div className="stat" data-tone={stats && stats.bounced ? 'bad' : undefined}>
          <dt>Bounced</dt>
          <dd className="num">{stats ? stats.bounced : '—'}</dd>
        </div>
        <div className="stat" data-tone={stats && stats.opted_out ? 'warn' : undefined}>
          <dt>Opted out</dt>
          <dd className="num">{stats ? stats.opted_out : '—'}</dd>
        </div>
      </dl>

      <h2 className="section-title">In flight right now</h2>
      <p className="muted">
        A snapshot of where each lead currently is, which is a different question from what has
        happened — a lead counted under “Replied” above is no longer in the SENT bucket here.
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
          {/* Pushed away from the routine controls. A destructive action a thumb-width from
              "Pause sending" is a destructive action somebody will eventually hit by accident. */}
          <span className="spacer" />
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
            hint="The address you will connect a Google account for. It is what recipients see, and what your SPF and DKIM records must cover."
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

      <h2 className="section-title">Sending account</h2>
      <p className="muted">
        The Gmail account this campaign sends from. Connecting it means approving us on Google's
        own screen — no password is typed here, and you can revoke access at any time from your
        Google account.
      </p>
      {conn === null ? (
        <SkeletonList count={1} rows={2} />
      ) : !conn.configured ? (
        <p className="muted" style={{ marginTop: 12 }}>
          This server has no Google credentials configured, so accounts cannot be connected yet.
          It falls back to the mailbox set in the server environment.
        </p>
      ) : (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="card-head">
            <Icons.inbox size={16} />
            <strong>{conn.account_email || conn.from_email || 'No mailbox yet'}</strong>
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
                  data-variant={conn.connected ? undefined : 'primary'}
                  disabled={busy}
                  onClick={connectGmail}
                >
                  {conn.connected ? 'Reconnect' : 'Connect Gmail'}
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
              {conn.reason} Google expires these connections about once a week while this app is
              unverified, so reconnecting is expected rather than a sign something is broken.
            </p>
          ) : null}
          {!conn.needs_reconnect && conn.expiry_notice ? (
            <p className="muted" data-tone="warn" style={{ marginTop: 10 }}>
              {conn.expiry_notice}
            </p>
          ) : null}
          {conn.connected && mayAdminister ? (
            <div style={{ marginTop: 10, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
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
                value={campaign.mailbox_id || ''}
                disabled={busy}
                onChange={(event) => useMailbox(event.target.value)}
              >
                {mailboxes.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.from_email}
                    {row.usable ? '' : ' — not connected yet'}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {checks ? (
            <ul className="muted" style={{ marginTop: 10, paddingLeft: 18 }}>
              {checks.checks.map((check) => (
                <li key={check.name}>
                  <strong>{CHECK_LABELS[check.name] || check.name}:</strong>{' '}
                  {check.ok ? 'ok' : 'failed'} — {check.detail}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      )}

      <h2 className="section-title">Schedule</h2>
      <p className="muted">
        When this campaign may run at all, as whole dates. The daily send window inside those
        days is in Settings. Leave both empty to start and stop it by hand.
        {campaign.schedule_note ? ` ${campaign.schedule_note}` : ''}
      </p>
      {mayAdminister ? (
        <form className="card" onSubmit={saveSchedule} style={{ marginTop: 12 }}>
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
              onChange={(event) => setSchedule({ ...schedule, ends_at: event.target.value })}
            />
          </div>
          <div className="btn-row">
            <button type="submit" className="btn" data-variant="primary" disabled={busy}>
              {busy ? 'Saving…' : 'Save schedule'}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => setSchedule({ starts_at: '', ends_at: '' })}
            >
              Clear
            </button>
          </div>
        </form>
      ) : null}

      <h2 className="section-title">Attachment</h2>
      <p className="muted">
        One file, carried by every first email this campaign sends. A reviewer can untick it for
        an individual email in the review queue, and follow-ups leave it off unless Settings says
        otherwise — the same document twice in one thread is noise.
      </p>
      {file === null ? (
        <SkeletonList count={1} rows={2} />
      ) : file.attachment ? (
        <div className="card" style={{ marginTop: 12 }}>
          <div className="card-head">
            <Icons.file size={16} />
            <strong>{file.attachment.filename}</strong>
            <span className="badge">{formatBytes(file.attachment.size_bytes)}</span>
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
            Uploaded by {file.attachment.uploaded_by || 'somebody'} on{' '}
            {new Date(file.attachment.created_at).toLocaleDateString()}. Replacing it changes
            every email not yet sent; the ones already gone keep what they carried.
          </p>
        </div>
      ) : (
        <p className="muted" style={{ marginTop: 12 }}>
          No file attached. Emails go as plain text on their own.
        </p>
      )}
      {mayAdminister ? (
        <div className="btn-row">
          <label className="btn" data-variant={file?.attachment ? undefined : 'primary'}>
            {fileBusy ? 'Uploading…' : file?.attachment ? 'Replace file' : 'Attach a file'}
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
        Up to 5 MB. Documents and images only — executables, macro-enabled Office files and
        archives are refused, because nothing here inspects what is inside a file.{' '}
        <strong>An attachment makes a cold email more likely to be filtered as spam</strong>, so a
        link to the document is usually the better opening.
      </p>

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
    kind: 'outreach',
    mode: 'research',
    headline: '',
    target_role: '',
    facts: '',
    profile_link: '',
    initial_subject: '',
    initial_body: '',
    followup_subject: '',
    followup_body: '',
    followup_after_days: 3,
    sender_name: '',
    sender_role: '',
    agency_name: '',
    offer: '',
    goal: 'book a 20-minute call',
    cta_text: '',
    booking_url: '',
    from_name: '',
    from_email: '',
  })
  const [busy, setBusy] = useState(false)
  const [fieldErrors, setFieldErrors] = useState([])
  // The accounts this workspace has already connected. Before this the form asked for an address
  // every time, so anybody who meant the account they had connected last week typed it again and
  // got the same row back -- or, worse, a second row they then had to connect separately.
  const [mailboxes, setMailboxes] = useState([])
  const [chosen, setChosen] = useState('')

  useEffect(() => {
    getJson('/mailboxes')
      .then((data) => {
        setMailboxes(data.mailboxes)
        // Preselect the one that can actually send. With exactly one usable account -- the
        // ordinary case -- there is nothing to decide and nothing to type.
        const usable = data.mailboxes.filter((row) => row.usable)
        if (usable.length === 1) setChosen(usable[0].id)
      })
      // Degrades to the old behaviour rather than erroring: an older server without this route
      // leaves the picker out and the address fields still work.
      .catch(() => setMailboxes([]))
  }, [])

  const set = (key) => (event) => setForm({ ...form, [key]: event.target.value })
  const setMode = (mode) => setForm({ ...form, mode })

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    // The persona validator refuses a campaign with neither a booking link nor an explicit
    // ask, because a draft has to end by asking for something. That rule used to be enforced
    // only on the server, where it arrived as a sentence naming a field this form did not have.
    if (form.mode === 'template' && !form.initial_body.trim()) {
      setFieldErrors([
        {
          field: 'templates.initial.body',
          message:
            'Write the first email. Use {{first_name}}, {{company}} or any column from your ' +
            'sheet where each lead\u2019s own details should go.',
        },
      ])
      return
    }
    if (form.mode === 'research' && !form.cta_text.trim() && !form.booking_url.trim()) {
      setFieldErrors([
        {
          field: 'persona.cta_text',
          message:
            'Fill in either a booking link or a call to action. A draft has to end by asking ' +
            'for something specific, and this is the sentence it asks with.',
        },
      ])
      setBusy(false)
      return
    }
    setFieldErrors([])
    try {
      // The mailbox first: a campaign can exist without one, but it could never send, and
      // creating them together means one less half-finished state to explain later.
      let mailboxId = chosen || null
      if (!mailboxId && form.from_email.trim()) {
        const mailbox = await postJson('/mailboxes', {
          json: { from_name: form.from_name || form.sender_name, from_email: form.from_email },
        })
        mailboxId = mailbox.id
      }

      const templates =
        form.mode === 'template'
          ? {
              initial: {
                subject: form.initial_subject || 'Quick question',
                body: form.initial_body,
              },
              // A follow-up is only sent if one was written. `max_followups` defaults to 1, and
              // the shorter of the two wins -- a campaign must never try to send an email that
              // does not exist.
              followups: form.followup_body.trim()
                ? [
                    {
                      subject: form.followup_subject || 'Re: {{company}}',
                      body: form.followup_body,
                      after_days: Number(form.followup_after_days) || 3,
                    },
                  ]
                : [],
            }
          : undefined

      await postJson('/campaigns', {
        json: {
          name: form.name,
          kind: form.kind,
          mode: form.mode,
          ...(templates ? { templates } : {}),
          mailbox_id: mailboxId,
          // A job hunt sends a *candidate profile*, not a persona: a different input form for
          // the same contract. Its facts carry the same `pp1`-style ids, so the claim checker
          // refuses an invented skill exactly as it refuses an invented statistic.
          persona: form.kind === 'job_hunt' ? {
            kind: 'job_hunt',
            full_name: form.sender_name,
            headline: form.headline,
            target_role: form.target_role,
            facts: form.facts
              .split(/\r?\n/)
              .map((line) => line.trim())
              .filter(Boolean)
              .map((text, index) => ({ id: `pp${index + 1}`, text })),
            ...(form.profile_link.trim() ? { links: [form.profile_link.trim()] } : {}),
            goal: form.goal,
            cta_text: form.cta_text,
            ...(form.booking_url.trim() ? { booking_url: form.booking_url.trim() } : {}),
          } : {
            sender_name: form.sender_name,
            // The rest are what the *research* drafter needs in order to write on your behalf.
            // A template campaign has no use for them -- you wrote the email -- and somebody
            // job-hunting has no agency and no offer, so sending empty strings would store a
            // persona that exists only to get past a form.
            ...(form.mode === 'research'
              ? {
                  sender_role: form.sender_role,
                  agency_name: form.agency_name,
                  offer: form.offer,
                  goal: form.goal,
                  cta_text: form.cta_text,
                }
              : {
                  ...(form.sender_role.trim() ? { sender_role: form.sender_role } : {}),
                  ...(form.agency_name.trim() ? { agency_name: form.agency_name } : {}),
                }),
            // Omitted rather than sent empty: the field is a URL or absent, and '' is neither.
            ...(form.booking_url.trim() ? { booking_url: form.booking_url.trim() } : {}),
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
        A campaign carries the sender persona, the goal and how its emails get written, so one
        tool serves more than one job. The persona is the list of claims a draft may make about
        us — it is validated now rather than at drafting time, because a malformed one quietly
        produces weaker drafts.
      </p>

      <form className="card" onSubmit={submit}>
        <Field label="Campaign name" value={form.name} onChange={set('name')} required />

        <h2 className="section-title">What this campaign is for</h2>
        <div className="mode-choice">
          <ModeOption
            value="outreach"
            active={form.kind === 'outreach'}
            onChange={(kind) => setForm({ ...form, kind })}
            title="Business outreach"
            note="An agency or team writing to people who could buy. Up to 100 a day, and the
              draft may cite your services and proof points."
          />
          <ModeOption
            value="job_hunt"
            active={form.kind === 'job_hunt'}
            onChange={(kind) => setForm({ ...form, kind })}
            title="Job hunting"
            note="One person writing to founders, hiring managers or recruiters. 25 a day by
              default, because a personal Gmail has no domain reputation to spend — and a draft
              may claim nothing that is not in your profile below."
          />
        </div>

        <h2 className="section-title">How the emails get written</h2>
        <div className="mode-choice">
          <ModeOption
            value="research"
            active={form.mode === 'research'}
            onChange={setMode}
            title="Research each company"
            note="Reads the company's own site, finds something specific and true, and drafts
              each email around it. Slower and better for agency outreach, where the opening
              line is the whole email."
          />
          <ModeOption
            value="template"
            active={form.mode === 'template'}
            onChange={setMode}
            title="One email, personalised"
            note="You write the email once and each lead's own details fill the blanks. No
              research and no model, so a sheet of a thousand drafts in seconds — right for a
              job hunt, or any list that should genuinely get the same message."
          />
        </div>

        {form.mode === 'template' ? (
          <>
            <h2 className="section-title">The email</h2>
            <p className="muted">
              Write it once. Put <code>{'{{first_name}}'}</code>, <code>{'{{company}}'}</code> or
              any column from your sheet where that lead's own details belong. A field with no
              value stops that one draft rather than sending a gap — give it a fallback like{' '}
              <code>{'{{first_name|there}}'}</code> if a blank is acceptable.
            </p>
            <Field
              label="Subject"
              value={form.initial_subject}
              onChange={set('initial_subject')}
              hint="Merge fields work here too."
            />
            <TextArea
              label="Body"
              value={form.initial_body}
              onChange={set('initial_body')}
              rows={10}
              required
            />

            <h2 className="section-title">The follow-up</h2>
            <p className="muted">
              Optional. Sent once, three days after the first email by default, and only to
              someone who has not replied — a reply, a bounce or an opt-out cancels it.
            </p>
            <Field
              label="Follow-up subject"
              value={form.followup_subject}
              onChange={set('followup_subject')}
              hint="Leave blank for 'Re:' plus the first subject."
            />
            <TextArea
              label="Follow-up body"
              value={form.followup_body}
              onChange={set('followup_body')}
              rows={6}
            />
            <Field
              label="Days after the first email"
              value={form.followup_after_days}
              onChange={set('followup_after_days')}
              type="number"
            />
          </>
        ) : null}

        <h2 className="section-title">Who it comes from</h2>
        {form.mode === 'template' ? (
          <p className="muted">
            Your name signs every email and appears in the footer, so a recipient can always tell
            who wrote to them. Nothing else here is needed — you wrote the email yourself.
          </p>
        ) : null}
        {form.kind === 'job_hunt' ? (
          <>
            <div className="field-grid">
              <Field
                label="Your name"
                value={form.sender_name}
                onChange={set('sender_name')}
                required
              />
              <Field
                label="One line about you"
                value={form.headline}
                onChange={set('headline')}
                required={form.mode === 'research'}
                hint="e.g. Backend engineer, 4 years"
              />
              <Field
                label="Portfolio or CV link"
                value={form.profile_link}
                onChange={set('profile_link')}
                hint="A link, not an attachment — cold mail carrying a file lands in spam far
                  more often, and a link costs the reader nothing."
              />
            </div>
            {form.mode === 'research' ? (
              <>
                <h2 className="section-title">What you have actually done</h2>
                <p className="muted">
                  One per line. <strong>A draft may claim nothing that is not here</strong> — no
                  inference, no rounding two years up to three. An email claiming experience you
                  do not have is not a weak email, it is something you will be asked about in an
                  interview.
                </p>
                <TextArea
                  label="Your facts"
                  value={form.facts}
                  onChange={set('facts')}
                  rows={5}
                  required
                  hint="e.g. Four years building Django services at Globex"
                />
                <Field
                  label="What you are looking for"
                  value={form.target_role}
                  onChange={set('target_role')}
                  required
                  hint="e.g. backend roles at early-stage startups"
                />
                <Field label="Goal" value={form.goal} onChange={set('goal')} required />
              </>
            ) : null}
          </>
        ) : (
          <div className="field-grid">
            <Field
              label="Your name"
              value={form.sender_name}
              onChange={set('sender_name')}
              required
            />
            <Field
              label="Your role"
              value={form.sender_role}
              onChange={set('sender_role')}
              required={form.mode === 'research'}
              hint="e.g. Founder"
            />
            <Field
              label="Agency name"
              value={form.agency_name}
              onChange={set('agency_name')}
              required={form.mode === 'research'}
              hint={form.mode === 'template' ? 'Leave blank if you are writing as yourself.' : ''}
            />
          </div>
        )}

        {form.mode === 'research' && form.kind === 'outreach' ? (
          <>
            <h2 className="section-title">What you are offering</h2>
            <p className="muted">
              A model writes each email from this, so it has to know what it is writing for.
            </p>
            <Field
              label="Offer"
              value={form.offer}
              onChange={set('offer')}
              required
              hint="One line, specific. This is what every draft is built around."
            />
            <Field label="Goal" value={form.goal} onChange={set('goal')} required />

            <h2 className="section-title">What the email asks for</h2>
            <p className="muted">
              Fill in at least one of these. A cold email that does not end with a concrete next
              step gets read and forgotten, so a campaign without one is refused.
            </p>
            <div className="field-grid">
              <Field
                label="Booking link"
                value={form.booking_url}
                onChange={set('booking_url')}
                type="url"
                hint="Your Cal.com or Calendly link. Must start with https://"
              />
              <Field
                label="Call to action"
                value={form.cta_text}
                onChange={set('cta_text')}
                hint="The ask, in your own words — e.g. worth a 20-minute call next week?"
              />
            </div>
          </>
        ) : (
          <>
            <h2 className="section-title">Booking link</h2>
            <p className="muted">
              Optional. If you give one, <code>{'{{booking_url}}'}</code> works as a merge field
              in your template, and it is the only link a draft is allowed to contain.
            </p>
            <Field
              label="Booking link"
              value={form.booking_url}
              onChange={set('booking_url')}
              type="url"
              hint="Must start with https://"
            />
          </>
        )}

        <h2 className="section-title">Sending account</h2>
        <p className="muted">
          Which address this campaign sends from. Pick one you have already connected, or add a
          new one and connect it on the campaign screen afterwards.
        </p>
        {mailboxes.length ? (
          <label className="field">
            <span>Send from</span>
            <select value={chosen} onChange={(event) => setChosen(event.target.value)}>
              <option value="">A new address…</option>
              {mailboxes.map((row) => (
                <option key={row.id} value={row.id} disabled={!row.usable}>
                  {row.from_email}
                  {row.usable ? '' : ' — not connected yet'}
                  {row.campaigns ? ` · ${row.campaigns} campaign(s)` : ''}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {chosen ? null : (
          <div className="field-grid">
            <Field
              label="Send from (email)"
              value={form.from_email}
              onChange={set('from_email')}
            />
            <Field
              label="Sender name recipients see"
              value={form.from_name}
              onChange={set('from_name')}
              hint="Defaults to your name."
            />
          </div>
        )}

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

/* ------------------------------------------------------------------- admin */

function Platform({ onError }) {
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(() => {
    getJson('/platform/overview')
      .then((body) => {
        setData(body)
        setFailed(false)
      })
      // "Loaded" means we asked and heard back, including hearing an error.
      .catch((err) => {
        setFailed(true)
        onError(err)
      })
  }, [onError])

  useEffect(load, [load])

  if (failed) return <p className="muted">Could not load the platform overview.</p>
  if (!data) return <p className="muted">Loading…</p>

  const { workspaces, totals, health, usage, deployment } = data
  const stalled = health.workspaces_with_no_working_mailbox || []
  const workerAge = health.worker_seen_seconds_ago

  return (
    <>
      <h1>Platform</h1>
      <p className="muted">
        Every workspace on this deployment, in aggregate. This screen deliberately shows no
        leads, drafts, replies or campaign names — a workspace owner agreed to this system
        sending their mail, not to its operator reading their pipeline. The API enforces that
        rather than the layout: nothing behind this screen can reach one org&apos;s data.
      </p>

      <dl className="stat-grid" style={{ marginTop: 14 }}>
        <div className="stat">
          <dt>Workspaces</dt>
          <dd className="num">
            {totals.workspaces}
            {deployment.max_orgs ? <span className="muted">/{deployment.max_orgs}</span> : null}
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
          {stalled.length} workspace(s) have a mailbox but none that authenticates, so they have
          silently stopped sending <em>and</em> stopped fetching replies. Nothing inside those
          workspaces says so — the owner has to press Reconnect.
        </p>
      ) : (
        <p className="muted">Every workspace with a mailbox has at least one that works.</p>
      )}
      <dl className="stat-grid">
        <div className="stat">
          <dt>Worker last seen</dt>
          <dd className="num" data-tone={workerAge == null || workerAge > 120 ? 'bad' : undefined}>
            {workerAge == null ? 'never' : workerAge + 's ago'}
          </dd>
        </div>
        <div className="stat">
          <dt>Dead jobs</dt>
          {/* Coloured only when non-zero: a red zero is an alarm about nothing. */}
          <dd className="num" data-tone={health.dead_jobs ? 'bad' : undefined}>
            {health.dead_jobs}
          </dd>
        </div>
        <div className="stat">
          <dt>Alerts unacknowledged</dt>
          <dd className="num" data-tone={health.alerts_open ? 'warn' : undefined}>
            {health.alerts_open}
          </dd>
        </div>
      </dl>

      <h2 className="section-title">Quota</h2>
      <p className="muted">
        The free-tier model chain is the real ceiling on this system — not CPU and not the
        database — so this is the number that decides whether it keeps working.
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
          <dd className="num" data-tone={usage.llm_failures_24h ? 'warn' : undefined}>
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
                <td data-tone={row.mailboxes && !row.mailboxes_connected ? 'bad' : undefined}>
                  {row.mailboxes_connected}/{row.mailboxes}
                </td>
                <td>
                  {row.campaigns_running}/{row.campaigns}
                </td>
                <td>{row.leads}</td>
                <td>{row.emails_sent}</td>
                <td>
                  <span className="badge" data-tone={row.send_mode === 'live' ? 'warn' : undefined}>
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
          {deployment.signup_mode} — only as open as Supabase is. Turning off{' '}
          <em>Allow new users to sign up</em> there leaves this invite-only, with the guest list
          in a UI rather than in SQL.
        </dd>
        <dt>Google OAuth</dt>
        <dd>{deployment.google_oauth_configured ? 'configured' : 'not configured'}</dd>
        <dt>Grant length</dt>
        <dd>
          {deployment.grant_days === 0
            ? 'never expires, so the OAuth client has been published'
            : deployment.grant_days + ' days, then every mailbox needs reconnecting'}
        </dd>
      </dl>
    </>
  )
}

function Admin({ onError, onToast }) {
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(() => {
    getJson('/admin/overview')
      .then((body) => {
        setData(body)
        setFailed(false)
      })
      // "Loaded" has to mean *we asked and heard back*, including hearing an error. A skeleton
      // that spins for ever above a real problem is the bug this project already shipped once.
      .catch((err) => {
        setFailed(true)
        onError(err)
      })
  }, [onError])

  useEffect(load, [load])

  if (failed) return <p className="muted">Could not load the admin overview.</p>
  if (!data) return <p className="muted">Loading…</p>

  const { totals, workspace, google_test_users: google } = data

  return (
    <>
      <h1>Admin</h1>
      <p className="muted">
        Only an owner or admin can open this screen, and the API checks the role itself rather
        than trusting the button being hidden.
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
          <dd className="num" data-tone={totals.needs_reconnect ? 'bad' : undefined}>
            {totals.needs_reconnect}
          </dd>
        </div>
      </dl>

      <h2 className="section-title">People</h2>
      <p className="muted">
        Whether somebody may sign in at all is decided by Supabase, which this system cannot
        read — it never joins across the identity provider&apos;s boundary. A row below means
        they have authenticated here at least once, which is the only honest signal we have.
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
                {person.email || 'no address on file'}
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
                  {row.paused ? <span className="badge" data-tone="warn">paused</span> : null}
                </td>
                <td className="muted">{row.connected_by || '—'}</td>
                <td>{row.days_left === null ? '—' : row.days_left}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">No mailbox yet. One is created when a campaign is made.</p>
      )}

      {google.required_while_unpublished ? (
        <>
          <h2 className="section-title">Before somebody can connect Gmail</h2>
          <div className="banner" data-tone="warn">
            <p>{google.note}</p>
            <p style={{ marginTop: 8 }}>
              <a href={google.console_url} target="_blank" rel="noreferrer">
                Open the Google Cloud console
              </a>{' '}
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
              <span className="badge">{workspace.signup_mode}</span>{' '}
              {workspace.signup_mode === 'open'
                ? 'anyone Supabase lets sign in gets their own workspace'
                : workspace.signup_mode === 'closed'
                  ? 'nobody new, unless invited'
                  : 'only approved email domains'}
            </td>
          </tr>
          <tr>
            <td>Gmail connections last</td>
            <td>
              {workspace.grant_days > 0
                ? `${workspace.grant_days} days, then each person reconnects`
                : 'until revoked (the OAuth client is published)'}
            </td>
          </tr>
          <tr>
            <td>Connect Gmail available</td>
            <td>{workspace.google_oauth_configured ? 'yes' : 'no — the server has no OAuth client'}</td>
          </tr>
          <tr>
            <td>Workspace limit</td>
            <td>{workspace.max_orgs}</td>
          </tr>
        </tbody>
      </table>
    </>
  )
}

/* ---------------------------------------------------------------- settings */

function Settings({ campaign, onError, onToast, onChanged }) {
  const [config, setConfig] = useState(null)
  const [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(false)
  const [fieldErrors, setFieldErrors] = useState([])

  const loadConfig = useCallback(() => {
    getJson('/admin/sending-config').then(setConfig).catch(onError)
  }, [onError])

  useEffect(loadConfig, [loadConfig])

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

      <h2 className="section-title">Sending</h2>
      <p className="muted">
        The send mode is the one setting that decides whether mail actually leaves. Restricting
        it takes effect within seconds, on the send loop&apos;s next cycle. Enabling{' '}
        <strong>live</strong> is owner-only, refused outside production, and has to be typed out
        — it means every address in a campaign can be emailed, not only the allowlist.
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
                <td>Set by</td>
                <td className="muted">
                  {config.source === 'dashboard'
                    ? `this dashboard${config.set_by ? ` — ${config.set_by}` : ''}${
                        config.set_at ? ` on ${new Date(config.set_at).toLocaleString()}` : ''
                      }`
                    : `the server environment (${config.environment_default})`}
                </td>
              </tr>
              <tr>
                <td>{(config.sending_accounts || []).length > 1 ? 'Sending accounts' : 'Sending account'}</td>
                <td className="mono">
                  {(config.sending_accounts || []).length
                    ? (config.sending_accounts || []).join(', ')
                    : config.sending_account || 'none yet — add one when you create a campaign'}
                </td>
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

          {config.editable ? (
            <SendModeControl config={config} onError={onError} onToast={onToast} onDone={loadConfig} />
          ) : (
            <p className="muted" style={{ marginTop: 14 }}>
              Only an owner can change the send mode.
            </p>
          )}
          <SendAllowlist mode={config.send_mode} onError={onError} onToast={onToast} />
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

      <h2 className="section-title">Team</h2>
      <Team onError={onError} onToast={onToast} />

      <h2 className="section-title">Suppression list</h2>
      <Suppressions onError={onError} onToast={onToast} />
    </>
  )
}

function Team({ onError, onToast }) {
  const [team, setTeam] = useState(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('member')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    // Degrades rather than erroring: an older server without this route should leave the panel
    // out, not put a red banner over a settings screen whose other sections are all fine.
    getJson('/team')
      .then(setTeam)
      .catch(() => setTeam(null))
  }, [])

  useEffect(load, [load])

  const invite = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const made = await postJson('/team/invitations', { json: { email, role } })
      setEmail('')
      onToast(
        made.already_invited
          ? `${made.email} was already invited.`
          : `${made.email} can now sign in and will land in this workspace.`
      )
      load()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (id) => {
    try {
      await deleteJson(`/team/invitations/${id}`)
      onToast('Invitation withdrawn.')
      load()
    } catch (err) {
      onError(err)
    }
  }

  const remove = async (id, who) => {
    try {
      await deleteJson(`/team/members/${id}`)
      onToast(`${who} no longer has access. Their work stays in this workspace.`)
      load()
    } catch (err) {
      onError(err)
    }
  }

  if (!team) return null

  return (
    <div className="card" style={{ marginTop: 12 }}>
      <p className="muted">
        People here share these campaigns, leads and drafts. Somebody who should have their own
        separate workspace does not belong on this list — they sign up with their own email
        instead.
      </p>
      <ul style={{ listStyle: 'none', padding: 0, margin: '12px 0' }}>
        {team.members.map((member) => (
          <li key={member.id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>{member.email || 'no address on file'}</span>
            <span className="badge">{member.role}</span>
            {member.is_you ? <span className="badge">you</span> : null}
            <span className="spacer" />
            {member.is_you ? null : (
              <button
                type="button"
                className="btn"
                data-size="sm"
                data-variant="danger"
                onClick={() => remove(member.id, member.email)}
              >
                Remove
              </button>
            )}
          </li>
        ))}
        {team.invitations.map((row) => (
          <li key={row.id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="muted">{row.email}</span>
            <span className="badge">{row.role}</span>
            <span className="badge" data-tone="warn">
              invited
            </span>
            <span className="spacer" />
            <button type="button" className="btn" data-size="sm" onClick={() => revoke(row.id)}>
              Withdraw
            </button>
          </li>
        ))}
      </ul>
      <form onSubmit={invite} className="field-row">
        <Field
          label="Invite by email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          type="email"
          required
        />
        <label className="field">
          <span>Role</span>
          <select value={role} onChange={(event) => setRole(event.target.value)}>
            <option value="member">member — reviews drafts</option>
            <option value="admin">admin — can also change the rules</option>
            <option value="owner">owner — can also change the send mode</option>
          </select>
        </label>
        <button type="submit" className="btn" data-variant="primary" disabled={busy || !email}>
          Invite
        </button>
      </form>
      <p className="muted" style={{ marginTop: 8 }}>
        They can be invited before or after they first sign in — but if they have already signed
        in they will have been given a workspace of their own, and a person belongs to one
        workspace here.
      </p>
    </div>
  )
}

function SendAllowlist({ mode, onError, onToast }) {
  const [rows, setRows] = useState(null)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    // Degrades rather than erroring: an older server without this route leaves the panel out.
    getJson('/admin/send-allowlist')
      .then((data) => setRows(data.allowlist))
      .catch(() => setRows(null))
  }, [])

  useEffect(load, [load])

  const add = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const made = await postJson('/admin/send-allowlist', { json: { email: value } })
      setValue('')
      onToast(
        made.added ? `${made.email} can now receive mail from this workspace.` : 'Already listed.'
      )
      load()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id, email) => {
    try {
      await deleteJson(`/admin/send-allowlist/${id}`)
      onToast(`${email} removed. Nothing will be sent to it.`)
      load()
    } catch (err) {
      onError(err)
    }
  }

  if (!rows) return null

  return (
    <div style={{ marginTop: 18 }}>
      <h3>Allowlist</h3>
      <p className="muted">
        While the send mode is <strong>allowlist</strong>, these are the only addresses this
        workspace can reach. It is the list for <em>this</em> workspace only — nobody else&apos;s
        rehearsal can put an address on it, and nothing here affects anybody else.
        {mode === 'allowlist' ? '' : ' The mode is not allowlist right now, so it is not in use.'}
      </p>
      {rows.length ? (
        <ul style={{ listStyle: 'none', padding: 0, margin: '10px 0' }}>
          {rows.map((row) => (
            <li key={row.id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span>{row.email}</span>
              <span className="spacer" />
              <button
                type="button"
                className="btn"
                data-size="sm"
                onClick={() => remove(row.id, row.email)}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted" data-tone="warn" style={{ marginTop: 10 }}>
          Empty, so in allowlist mode nothing can be sent at all. Add the address you want to
          rehearse against — usually one of your own.
        </p>
      )}
      <form onSubmit={add} className="field-row">
        <Field
          label="Allow an address"
          type="email"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          required
        />
        <button type="submit" className="btn" disabled={busy || !value}>
          Add
        </button>
      </form>
    </div>
  )
}

function SendModeControl({ config, onError, onToast, onDone }) {
  const [choice, setChoice] = useState(config.send_mode)
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)

  // Typing LIVE is required by the API, and asking for it here rather than only showing the
  // server's 400 means the person reads what live actually does before they commit to it.
  const needsConfirm = choice === 'live'
  const blocked = needsConfirm && (!config.live_allowed || confirm !== 'LIVE')

  const save = async (event) => {
    event.preventDefault()
    setBusy(true)
    try {
      const body = { send_mode: choice === 'environment' ? null : choice }
      if (needsConfirm) body.confirm = confirm
      const result = await patchJson('/admin/sending-config', { json: body })
      onToast(
        result.send_mode === 'live'
          ? 'Live sending is on. Every address in a running campaign can now be emailed.'
          : `Send mode is now ${result.send_mode}.`,
        result.send_mode === 'live' ? 'error' : 'success'
      )
      setConfirm('')
      onDone()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={save} style={{ marginTop: 16 }}>
      <div className="field">
        <label htmlFor="mode">Change send mode</label>
        <select id="mode" value={choice} onChange={(event) => setChoice(event.target.value)}>
          <option value="disabled">disabled — nothing is sent, to anybody</option>
          <option value="allowlist">allowlist — only this workspace&apos;s own list</option>
          <option value="live" disabled={!config.live_allowed}>
            live — any recipient{config.live_allowed ? '' : ' (production only)'}
          </option>
          <option value="environment">
            follow the server ({config.environment_default})
          </option>
        </select>
        {config.live_allowed ? null : (
          <p className="hint">
            Live is unavailable because this server is running as{' '}
            <strong>{config.environment}</strong>, not production.
          </p>
        )}
      </div>

      {needsConfirm ? (
        <div className="banner" data-tone="bad">
          <div>
            <strong>This sends real email to real people.</strong> Every lead in a running
            campaign becomes eligible, not just the allowlist. Approval is still required for
            each draft, and the window and daily cap still apply.
            <div className="field" style={{ marginTop: 10, marginBottom: 0 }}>
              <label htmlFor="confirm">Type LIVE to confirm</label>
              <input
                id="confirm"
                type="text"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                placeholder="LIVE"
              />
            </div>
          </div>
        </div>
      ) : null}

      <div className="btn-row">
        <button
          type="submit"
          className="btn"
          data-variant={needsConfirm ? 'danger' : 'primary'}
          disabled={busy || blocked || choice === config.send_mode}
        >
          {busy ? 'Saving…' : 'Apply send mode'}
        </button>
      </div>
    </form>
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

function ModeOption({ value, active, onChange, title, note }) {
  return (
    <button
      type="button"
      className="mode-option"
      data-active={active ? 'true' : undefined}
      onClick={() => onChange(value)}
      aria-pressed={active}
    >
      <strong>{title}</strong>
      <span>{note}</span>
    </button>
  )
}

function TextArea({ label, value, onChange, rows = 6, required, hint }) {
  const id = `t-${label.replace(/\W+/g, '-').toLowerCase()}`
  return (
    <div className="field">
      <label htmlFor={id}>
        {label}
        {required ? null : <span className="muted"> (optional)</span>}
      </label>
      <textarea id={id} rows={rows} value={value} onChange={onChange} required={required} />
      {hint ? <p className="hint">{hint}</p> : null}
    </div>
  )
}

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
    return "only the addresses on this workspace's allowlist"
  }
  return 'any recipient — real mail goes to real people'
}
