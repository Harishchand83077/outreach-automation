// The dashboard, against `/api/v1`.
//
// Rebuilt for Phase 8. Every endpoint the previous version called belonged to the prototype's API
// and no longer exists, so this is a rewrite rather than a port.
//
// Two things shape the layout. **The review queue comes first**, because it is the only screen
// where a person's attention is the bottleneck: nothing is ever sent without a human approving
// it (§13), so a day's throughput is however many drafts somebody reads. **Evidence sits beside
// the draft**, because a reviewer who cannot see what a claim rests on cannot check it, and will
// approve.
//
// Plain text throughout. No `dangerouslySetInnerHTML` anywhere — a draft body is a lead's name
// and a model's output, neither of which is trusted (hard rule 7).

import React, { useCallback, useEffect, useState } from 'react'

import { SignIn, useSession } from './Auth'
import { ApiUnreachable, NotAuthenticated, deleteJson, getJson, postJson, uploadFile } from './api'
import { signOut } from './supabase'

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
  const [attention, setAttention] = useState(null)

  const toast = useCallback((message, kind = 'info') => {
    const id = Math.random().toString(36).slice(2)
    setToasts((current) => [...current, { id, message, kind }])
    setTimeout(() => setToasts((current) => current.filter((t) => t.id !== id)), 6000)
  }, [])

  // One place that decides what an error means. An expired session is not something to show in a
  // red box — it is a reason to send somebody back to the sign-in screen.
  const handle = useCallback(
    (err) => {
      if (err instanceof NotAuthenticated) {
        signOut()
        return
      }
      setError(err instanceof ApiUnreachable ? err.message : String(err.message || err))
    },
    [setError]
  )

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

  useEffect(() => {
    if (!me) return
    refreshCampaigns()
    refreshAttention()
  }, [me, refreshCampaigns, refreshAttention])

  if (error && !me) {
    return (
      <Centered>
        <h1 style={styles.title}>Cannot load the dashboard</h1>
        <p style={styles.muted}>{error}</p>
        <button type="button" onClick={() => signOut()} style={styles.button}>
          Sign out
        </button>
      </Centered>
    )
  }

  if (!me) return <Centered>Loading…</Centered>

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <div>
          <h1 style={styles.title}>Outreach</h1>
          <p style={styles.subtitle}>
            Signed in as {me.email || session.user?.email} · {me.role}
          </p>
        </div>
        <button type="button" onClick={() => signOut()} style={styles.button}>
          Sign out
        </button>
      </header>

      {error ? (
        <div style={styles.error} role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => setError(null)} style={styles.dismiss}>
            ×
          </button>
        </div>
      ) : null}

      <NeedsAttention counts={attention} />

      <CampaignPicker
        campaigns={campaigns}
        value={campaignId}
        onChange={setCampaignId}
        onRefresh={refreshCampaigns}
      />

      {campaignId ? (
        <>
          <ReviewQueue
            campaignId={campaignId}
            onError={handle}
            onToast={toast}
            onChanged={refreshAttention}
          />
          <Leads campaignId={campaignId} onError={handle} />
          <Import campaignId={campaignId} onError={handle} onToast={toast} />
        </>
      ) : (
        <p style={styles.muted}>No campaigns yet.</p>
      )}

      <Suppressions onError={handle} onToast={toast} />

      <div style={styles.toastHost}>
        {toasts.map((t) => (
          <div key={t.id} style={{ ...styles.toast, ...toastStyle(t.kind) }} role="status">
            {t.message}
          </div>
        ))}
      </div>
    </div>
  )
}

function NeedsAttention({ counts }) {
  if (!counts) return null
  const items = [
    ['Sends needing confirmation', counts.unknown_sends, 'var(--danger)'],
    ['Dead jobs', counts.dead_jobs, 'var(--warning)'],
    ['Paused mailboxes', counts.paused_mailboxes, 'var(--warning)'],
  ].filter(([, value]) => value > 0)

  if (items.length === 0) return null

  return (
    <section style={styles.section}>
      <div style={styles.attention}>
        {items.map(([label, value, colour]) => (
          <span key={label} style={{ ...styles.attentionItem, borderColor: colour }}>
            <strong>{value}</strong> {label}
          </span>
        ))}
      </div>
      <p style={styles.muted}>
        A send that could not be confirmed is never retried automatically — a duplicate cold email
        is worse than one that needs checking. Look in the mailbox and decide.
      </p>
    </section>
  )
}

function CampaignPicker({ campaigns, value, onChange, onRefresh }) {
  return (
    <section style={styles.section}>
      <div style={styles.row}>
        <label htmlFor="campaign" style={styles.label}>
          Campaign
        </label>
        <select
          id="campaign"
          value={value || ''}
          onChange={(event) => onChange(event.target.value)}
          style={styles.select}
        >
          {campaigns.map((campaign) => (
            <option key={campaign.id} value={campaign.id}>
              {campaign.name}
              {campaign.paused ? ' (paused)' : ''}
              {campaign.has_persona ? '' : ' — no persona'}
            </option>
          ))}
        </select>
        <button type="button" onClick={onRefresh} style={styles.button}>
          Refresh
        </button>
      </div>
    </section>
  )
}

function ReviewQueue({ campaignId, onError, onToast, onChanged }) {
  const [items, setItems] = useState([])
  const [edits, setEdits] = useState({})
  const [busy, setBusy] = useState(null)

  const load = useCallback(() => {
    getJson(`/review/drafts?campaign_id=${campaignId}`)
      .then((data) => setItems(data.drafts))
      .catch(onError)
  }, [campaignId, onError])

  useEffect(load, [load])

  const approve = async (item) => {
    setBusy(item.draft_id)
    try {
      const edit = edits[item.draft_id]
      await postJson(`/drafts/${item.draft_id}/approve`, { json: edit ? edit : {} })
      onToast('Approved. The send loop decides when it actually goes.', 'success')
      load()
      onChanged()
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
      load()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(null)
    }
  }

  const setEdit = (id, patch) =>
    setEdits((current) => ({ ...current, [id]: { ...(current[id] || {}), ...patch } }))

  return (
    <section style={styles.section}>
      <h2 style={styles.heading}>
        Review queue <span style={styles.badge}>{items.length}</span>
      </h2>
      <p style={styles.muted}>
        Nothing is sent without a decision here. Approving makes a lead eligible; the send loop
        re-checks the suppression list, the window and today's cap before anything leaves.
      </p>

      {items.length === 0 ? (
        <p style={styles.muted}>Nothing waiting.</p>
      ) : (
        items.map((item) => (
          <article key={item.draft_id} style={styles.card}>
            <div style={styles.row}>
              <strong>{item.lead.full_name || item.lead.email}</strong>
              <span style={styles.badge}>{item.lead.company_name || 'no company'}</span>
              <span style={styles.badge}>{item.kind}</span>
              <span style={styles.badge}>v{item.version}</span>
              {item.status === 'validation_failed' ? (
                <span style={{ ...styles.badge, color: 'var(--danger)' }}>validation failed</span>
              ) : null}
            </div>

            <label style={styles.label}>Subject</label>
            <input
              value={(edits[item.draft_id] || {}).subject ?? item.subject}
              onChange={(event) => setEdit(item.draft_id, { subject: event.target.value })}
              style={styles.input}
            />

            <label style={styles.label}>Body</label>
            <textarea
              rows={10}
              value={(edits[item.draft_id] || {}).body ?? item.body}
              onChange={(event) => setEdit(item.draft_id, { body: event.target.value })}
              style={styles.textarea}
            />

            <Evidence item={item} />

            <label style={styles.label}>Reason, if rejecting</label>
            <input
              value={(edits[item.draft_id] || {}).feedback || ''}
              onChange={(event) => setEdit(item.draft_id, { feedback: event.target.value })}
              placeholder="Too long; lead with the pipeline rebuild"
              style={styles.input}
            />

            <div style={styles.buttons}>
              <button
                type="button"
                onClick={() => approve(item)}
                disabled={busy === item.draft_id || item.status === 'validation_failed'}
                style={{ ...styles.button, ...styles.primary }}
              >
                Approve
              </button>
              <button
                type="button"
                onClick={() => reject(item)}
                disabled={busy === item.draft_id}
                style={styles.button}
              >
                Reject
              </button>
            </div>
            {item.status === 'validation_failed' ? (
              <p style={styles.muted}>
                This draft failed the grounding checks, so it cannot be approved. Reject it with a
                note and it will be rewritten.
              </p>
            ) : null}
          </article>
        ))
      )}
    </section>
  )
}

function Evidence({ item }) {
  const hooks = item.hooks || []
  return (
    <details style={styles.evidence}>
      <summary style={styles.summary}>
        Evidence — {hooks.length} research hook{hooks.length === 1 ? '' : 's'}
        {item.thin_research ? ' · thin' : ''} · confidence{' '}
        {Number(item.research_confidence || 0).toFixed(2)}
      </summary>
      {hooks.length === 0 ? (
        <p style={styles.muted}>
          No verified research. Anything specific in this draft has nothing behind it.
        </p>
      ) : (
        <ul style={styles.hooks}>
          {hooks.map((hook) => (
            <li key={hook.id || hook.text} style={styles.hook}>
              <strong>{hook.id}</strong> — {hook.text}
              {hook.snippet ? <div style={styles.snippet}>“{hook.snippet}”</div> : null}
              {hook.source_url ? (
                <div style={styles.muted}>
                  <a href={hook.source_url} target="_blank" rel="noreferrer noopener">
                    {hook.source_url}
                  </a>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </details>
  )
}

function Leads({ campaignId, onError }) {
  const [page, setPage] = useState({ leads: [], next_cursor: null })
  const [state, setState] = useState('')
  const [cursor, setCursor] = useState(null)

  useEffect(() => {
    const params = new URLSearchParams({ limit: '25' })
    if (state) params.set('state', state)
    if (cursor) params.set('cursor', cursor)
    getJson(`/campaigns/${campaignId}/leads?${params}`).then(setPage).catch(onError)
  }, [campaignId, state, cursor, onError])

  return (
    <section style={styles.section}>
      <h2 style={styles.heading}>Leads</h2>
      <div style={styles.row}>
        <label htmlFor="state" style={styles.label}>
          State
        </label>
        <select
          id="state"
          value={state}
          onChange={(event) => {
            setCursor(null)
            setState(event.target.value)
          }}
          style={styles.select}
        >
          <option value="">all</option>
          {STATES.map((value) => (
            <option key={value} value={value}>
              {value}
            </option>
          ))}
        </select>
      </div>

      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.th}>Email</th>
            <th style={styles.th}>Name</th>
            <th style={styles.th}>Company</th>
            <th style={styles.th}>State</th>
            <th style={styles.th}>Next action</th>
          </tr>
        </thead>
        <tbody>
          {page.leads.map((lead) => (
            <tr key={lead.id} style={styles.tr}>
              <td style={styles.td}>{lead.email}</td>
              <td style={styles.td}>{lead.full_name || '—'}</td>
              <td style={styles.td}>{lead.company_name || '—'}</td>
              <td style={styles.td}>{lead.state}</td>
              <td style={styles.td}>
                {lead.next_action_at ? new Date(lead.next_action_at).toLocaleString() : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {page.leads.length === 0 ? <p style={styles.muted}>No leads in this view.</p> : null}

      <div style={styles.buttons}>
        <button type="button" onClick={() => setCursor(null)} disabled={!cursor} style={styles.button}>
          First page
        </button>
        <button
          type="button"
          onClick={() => setCursor(page.next_cursor)}
          disabled={!page.next_cursor}
          style={styles.button}
        >
          Next page
        </button>
      </div>
    </section>
  )
}

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
    <section style={styles.section}>
      <h2 style={styles.heading}>Import leads</h2>
      <p style={styles.muted}>
        The preview writes nothing. Committing re-reads the file and re-checks every row against
        the suppression list, so anyone who opted out between the two is still excluded.
      </p>
      <input type="file" accept=".csv,.xlsx" onChange={choose} disabled={busy} />

      {preview ? (
        <div style={styles.card}>
          <div style={styles.row}>
            {Object.entries(preview.counts).map(([verdict, count]) => (
              <span key={verdict} style={styles.badge}>
                {count} {verdict}
              </span>
            ))}
            {preview.truncated ? (
              <span style={{ ...styles.badge, color: 'var(--warning)' }}>
                {preview.truncated} rows beyond the cap were dropped
              </span>
            ) : null}
          </div>

          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>Row</th>
                <th style={styles.th}>Email</th>
                <th style={styles.th}>Verdict</th>
                <th style={styles.th}>Why</th>
              </tr>
            </thead>
            <tbody>
              {preview.rows.slice(0, 50).map((row) => (
                <tr key={row.row_number} style={styles.tr}>
                  <td style={styles.td}>{row.row_number}</td>
                  <td style={styles.td}>{row.email || '—'}</td>
                  <td style={{ ...styles.td, color: verdictColour(row.verdict) }}>
                    {row.verdict}
                  </td>
                  <td style={styles.td}>{row.reasons.join('; ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <button
            type="button"
            onClick={commit}
            disabled={busy || !preview.can_commit}
            style={{ ...styles.button, ...styles.primary }}
          >
            Import the importable rows
          </button>
          {!preview.can_commit ? (
            <p style={styles.muted}>
              Nothing here can be imported. Check that the sheet has email, name and company
              columns.
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
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
    <section style={styles.section}>
      <h2 style={styles.heading}>Suppression list</h2>
      <p style={styles.muted}>
        Checked immediately before every send. Removing someone is deliberate and never automatic.
      </p>
      <form onSubmit={add} style={styles.row}>
        <input
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="someone@example.com"
          style={{ ...styles.input, marginBottom: 0, maxWidth: 320 }}
        />
        <button type="submit" style={styles.button}>
          Suppress
        </button>
      </form>

      <table style={styles.table}>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} style={styles.tr}>
              <td style={styles.td}>{row.value}</td>
              <td style={styles.td}>{row.kind}</td>
              <td style={styles.td}>{row.reason}</td>
              <td style={styles.td}>
                <button type="button" onClick={() => remove(row.id)} style={styles.button}>
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 ? <p style={styles.muted}>Nobody suppressed yet.</p> : null}
    </section>
  )
}

function Centered({ children }) {
  return (
    <div style={{ display: 'grid', placeItems: 'center', minHeight: '70vh', padding: 20 }}>
      <div style={{ textAlign: 'center', maxWidth: 460 }}>{children}</div>
    </div>
  )
}

const STATES = [
  'NEW',
  'ENRICHING',
  'RESEARCHED',
  'DRAFTED',
  'AWAITING_APPROVAL',
  'APPROVED',
  'SENT',
  'FOLLOWUP_SENT',
  'REPLIED',
  'IN_CONVERSATION',
  'MEETING_BOOKED',
  'NOT_INTERESTED',
  'OPTED_OUT',
  'BOUNCED',
  'NO_RESPONSE',
  'SKIPPED',
  'FAILED',
]

function verdictColour(verdict) {
  if (verdict === 'reject') return 'var(--danger)'
  if (verdict === 'warn') return 'var(--warning)'
  return 'var(--success)'
}

function toastStyle(kind) {
  if (kind === 'success') return { borderColor: 'var(--success)' }
  if (kind === 'error') return { borderColor: 'var(--danger)' }
  return { borderColor: 'var(--accent)' }
}

const styles = {
  container: { maxWidth: 1100, margin: '0 auto', padding: '24px 20px' },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    gap: 16,
    marginBottom: 24,
  },
  title: { fontSize: 28, fontWeight: 700, margin: '0 0 8px' },
  subtitle: { color: 'var(--textMuted)', margin: 0, fontSize: 15 },
  heading: { fontSize: 20, fontWeight: 700, margin: '0 0 8px' },
  section: { marginBottom: 40 },
  row: { display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 },
  card: {
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: 10,
    padding: 18,
    marginBottom: 16,
  },
  badge: {
    fontSize: 12,
    color: 'var(--textMuted)',
    background: 'var(--surface2)',
    padding: '4px 8px',
    borderRadius: 6,
  },
  attention: { display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 8 },
  attentionItem: {
    border: '1px solid',
    borderRadius: 8,
    padding: '8px 12px',
    fontSize: 14,
  },
  label: { display: 'block', fontSize: 13, margin: '12px 0 6px', color: 'var(--textMuted)' },
  input: { width: '100%', marginBottom: 10 },
  select: { minWidth: 220 },
  textarea: { width: '100%', fontFamily: 'inherit', fontSize: 14, lineHeight: 1.6 },
  buttons: { display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' },
  button: { background: 'var(--surface2)', color: 'var(--text)' },
  primary: { background: 'var(--accent)', color: '#fff', borderColor: 'var(--accent)' },
  table: { width: '100%', borderCollapse: 'collapse', marginTop: 12 },
  th: {
    textAlign: 'left',
    padding: '10px 12px',
    background: 'var(--surface2)',
    fontWeight: 600,
    fontSize: 13,
  },
  tr: { borderBottom: '1px solid var(--border)' },
  td: { padding: '10px 12px', fontSize: 14 },
  muted: { color: 'var(--textMuted)', fontSize: 14, lineHeight: 1.6 },
  evidence: {
    background: 'var(--surface2)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    padding: 12,
    marginTop: 12,
  },
  summary: { cursor: 'pointer', fontSize: 14 },
  hooks: { margin: '12px 0 0', paddingLeft: 18 },
  hook: { fontSize: 14, marginBottom: 10, lineHeight: 1.6 },
  snippet: { color: 'var(--textMuted)', fontStyle: 'italic', margin: '4px 0' },
  error: {
    background: 'rgba(248, 81, 73, 0.15)',
    border: '1px solid var(--danger)',
    borderRadius: 8,
    padding: '12px 14px',
    marginBottom: 20,
    display: 'flex',
    justifyContent: 'space-between',
    gap: 12,
    fontSize: 14,
  },
  dismiss: { background: 'transparent', color: 'inherit', padding: '0 6px' },
  toastHost: { position: 'fixed', right: 20, bottom: 20, display: 'grid', gap: 10, maxWidth: 360 },
  toast: {
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    padding: '12px 14px',
    fontSize: 14,
  },
}
