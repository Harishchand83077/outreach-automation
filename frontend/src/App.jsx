import React, { useState, useEffect, useCallback, useRef } from 'react'

// When deployed, VITE_API_URL is the backend origin (e.g. https://xxx.onrender.com). We need /api for routes.
const _base = import.meta.env.VITE_API_URL || ''
const API = _base ? (_base.replace(/\/api\/?$/, '') + '/api') : '/api'

// Simple toast: id, message, type ('success'|'error'|'info')
function Toast({ toast, onDismiss }) {
  const style = {
    success: { background: 'rgba(63, 185, 80, 0.2)', borderColor: 'var(--success)' },
    error: { background: 'rgba(248, 81, 73, 0.2)', borderColor: 'var(--danger)' },
    info: { background: 'rgba(88, 166, 255, 0.2)', borderColor: 'var(--accent)' },
  }[toast.type] || style.info
  return (
    <div style={{ ...styles.toast, ...style }} role="alert">
      <span>{toast.message}</span>
      <button type="button" onClick={() => onDismiss(toast.id)} style={styles.toastDismiss} aria-label="Dismiss">×</button>
    </div>
  )
}

export default function App() {
  const [leads, setLeads] = useState([])
  const [pending, setPending] = useState([])
  const [running, setRunning] = useState(false)
  const [lastRunError, setLastRunError] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState(null)
  const [rejectFeedback, setRejectFeedback] = useState({})
  const [editedContent, setEditedContent] = useState({})
  const [expandedLead, setExpandedLead] = useState(null)
  const [stats, setStats] = useState(null)
  const [toasts, setToasts] = useState([])
  const [onboardingDismissed, setOnboardingDismissed] = useState(() => {
    try { return localStorage.getItem('outreach_onboarding_done') === '1' } catch { return false }
  })
  const toastIdRef = useRef(0)

  const addToast = useCallback((message, type = 'info') => {
    const id = ++toastIdRef.current
    setToasts((t) => [...t, { id, message, type }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500)
  }, [])

  const removeToast = useCallback((id) => {
    setToasts((t) => t.filter((x) => x.id !== id))
  }, [])

  const fetchLeads = useCallback(async () => {
    try {
      const r = await fetch(`${API}/leads`)
      if (!r.ok) throw new Error(r.statusText)
      const data = await r.json()
      setLeads(data.leads || [])
    } catch (e) {
      setError(e.message)
    }
  }, [])

  const fetchPending = useCallback(async () => {
    try {
      const r = await fetch(`${API}/leads/pending`)
      if (!r.ok) return
      const data = await r.json()
      setPending(data.pending || [])
    } catch (_) {}
  }, [])

  const fetchRunStatus = useCallback(async () => {
    try {
      const r = await fetch(`${API}/run/status`)
      if (!r.ok) return
      const data = await r.json()
      setRunning(data.running === true)
      setLastRunError(data.last_error || null)
    } catch (_) {}
  }, [])

  const fetchStats = useCallback(async () => {
    try {
      const r = await fetch(`${API}/stats`)
      if (!r.ok) return
      const data = await r.json()
      setStats(data)
    } catch (_) {}
  }, [])

  useEffect(() => {
    fetchLeads()
    fetchPending()
    fetchRunStatus()
    fetchStats()
    const t = setInterval(() => {
      fetchLeads()
      fetchPending()
      fetchRunStatus()
      fetchStats()
    }, 2500)
    return () => clearInterval(t)
  }, [fetchLeads, fetchPending, fetchRunStatus, fetchStats])

  const handleUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setError(null)
    try {
      const form = new FormData()
      form.append('file', file)
      const r = await fetch(`${API}/upload`, { method: 'POST', body: form })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || data.message || r.statusText)
      await fetchLeads()
      addToast(`Uploaded ${data.count || 0} lead(s). You can run outreach now.`, 'success')
    } catch (err) {
      setError(String(err.message))
      addToast(err.message, 'error')
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  const handleRun = async () => {
    setError(null)
    try {
      const r = await fetch(`${API}/run?use_existing=true`, { method: 'POST' })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || data.message || r.statusText)
      if (data.status === 'no_leads') throw new Error('No leads in INIT status. Upload an Excel file first.')
      if (data.status === 'already_running') {
        setRunning(true)
        addToast('A run is already in progress.', 'info')
        return
      }
      setRunning(true)
      setLastRunError(null)
      addToast(`Run started for ${data.lead_count || 0} lead(s). Wait for "Pending your approval" cards below, or check the lead row for insights/draft.`, 'success')
      await fetchRunStatus()
    } catch (err) {
      setError(String(err.message))
      addToast(err.message, 'error')
    }
  }

  const handleRetryFailed = async () => {
    setError(null)
    try {
      const r = await fetch(`${API}/run/retry-failed`, { method: 'POST' })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || data.message || r.statusText)
      if (data.reset_count > 0) {
        addToast(data.message || `${data.reset_count} lead(s) reset. Click Run outreach to retry.`, 'success')
        await fetchLeads()
        await fetchStats()
      } else {
        addToast('No failed leads to retry.', 'info')
      }
    } catch (err) {
      setError(String(err.message))
      addToast(err.message, 'error')
    }
  }

  const handleExportCsv = () => {
    window.open(`${API}/leads/export/csv`, '_blank')
    addToast('CSV download started.', 'success')
  }

  const dismissOnboarding = () => {
    try { localStorage.setItem('outreach_onboarding_done', '1') } catch (_) {}
    setOnboardingDismissed(true)
  }

  const setReject = (key, value) => setRejectFeedback((s) => ({ ...s, [key]: value }))

  const pendingKey = (p) => `${p.email}-${p.type}`
  const getContent = (p) => {
    if (p.type === 'question_reply') return editedContent[pendingKey(p)] ?? ''
    return editedContent[pendingKey(p)] ?? (p.type === 'insights' ? p.insights : p.email_draft) ?? ''
  }
  const setContent = (p, value) => setEditedContent((s) => ({ ...s, [pendingKey(p)]: value }))

  const approve = async (p) => {
    const { email, type } = p
    let url, body
    if (type === 'question_reply') {
      url = `${API}/leads/${encodeURIComponent(email)}/approve-question-reply`
      body = { response_text: getContent(p) }
    } else {
      url = `${API}/leads/${encodeURIComponent(email)}/approve-${type}`
      body = type === 'insights' ? { edited_insights: getContent(p) } : { edited_email_draft: getContent(p) }
    }
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText)
      setEditedContent((s) => ({ ...s, [pendingKey(p)]: undefined }))
      await fetchPending()
      await fetchLeads()
      addToast(type === 'insights' ? 'Insights approved.' : type === 'email' ? 'Email approved. Sending…' : 'Response sent.', 'success')
    } catch (err) {
      setError(err.message)
      addToast(err.message, 'error')
    }
  }
  const reject = async (email, type) => {
    const feedback = rejectFeedback[`${email}-${type}`] || ''
    try {
      const r = await fetch(`${API}/leads/${encodeURIComponent(email)}/reject-${type}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ feedback: feedback || undefined }),
      })
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText)
      setRejectFeedback((s) => ({ ...s, [`${email}-${type}`]: '' }))
      await fetchPending()
      await fetchLeads()
      addToast('Rejected. LLM will regenerate with your feedback.', 'info')
    } catch (err) {
      setError(err.message)
      addToast(err.message, 'error')
    }
  }

  const failedCount = (stats?.by_status?.EMAIL_FAILED || 0) + (stats?.by_status?.ERROR || 0)

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <h1 style={styles.title}>Funding Outreach Automation</h1>
        <p style={styles.subtitle}>
          AI drafts. You decide. — Human-in-the-loop outreach: approve or edit every insight and email before it’s sent.
        </p>
      </header>

      {!onboardingDismissed && (
        <div style={styles.onboarding}>
          <div>
            <strong>Quick start:</strong> 1) Upload Excel (Name, Email, Company) → 2) Click &quot;Run outreach&quot; → 3) Approve or edit insights and email drafts when they appear. Nothing is sent without your approval.
          </div>
          <button type="button" onClick={dismissOnboarding} style={styles.onboardingDismiss}>Got it</button>
        </div>
      )}

      {toasts.length > 0 && (
        <div style={styles.toastContainer}>
          {toasts.map((t) => (
            <Toast key={t.id} toast={t} onDismiss={removeToast} />
          ))}
        </div>
      )}

      {error && (
        <div style={styles.error}>
          {error}
          <button type="button" onClick={() => setError(null)} style={styles.dismiss}>Dismiss</button>
        </div>
      )}

      {lastRunError && !running && (
        <div style={styles.error}>
          Last run failed: {lastRunError}
          <button type="button" onClick={() => setLastRunError(null)} style={styles.dismiss}>Dismiss</button>
        </div>
      )}

      {stats != null && (
        <section style={styles.statsStrip}>
          <span>Total leads: <strong>{stats.total_leads}</strong></span>
          <span>Emails sent: <strong>{stats.emails_sent}</strong></span>
          <span>Meetings booked: <strong>{stats.meeting_booked}</strong></span>
          {stats.last_activity && (
            <span style={styles.muted}>Last activity: {new Date(stats.last_activity).toLocaleString()}</span>
          )}
        </section>
      )}

      <section style={styles.actions}>
        <label style={styles.fileLabel}>
          <input type="file" accept=".xlsx,.xls" onChange={handleUpload} disabled={uploading} style={{ display: 'none' }} />
          <span style={styles.button}>{uploading ? 'Uploading…' : 'Upload Excel'}</span>
        </label>
        <button
          type="button"
          onClick={handleRun}
          disabled={running || uploading}
          style={{ ...styles.button, ...styles.primary }}
        >
          {running ? 'Run in progress…' : 'Run outreach'}
        </button>
        {running && (
          <span style={styles.muted}>Wait 15–30 s, then check &quot;Pending your approval&quot; or expand the lead row for insights/draft.</span>
        )}
        <button type="button" onClick={handleExportCsv} style={styles.button} title="Download leads as CSV">
          Export CSV
        </button>
        {failedCount > 0 && (
          <button type="button" onClick={handleRetryFailed} style={styles.button} title="Reset failed leads and run again">
            Retry failed ({failedCount})
          </button>
        )}
      </section>

      {pending.length > 0 && (
        <section style={styles.section}>
          <h2 style={styles.sectionTitle}>Pending your approval</h2>
          <p style={styles.muted}>
            Edit the content below if needed. Approve to continue (or Reject with feedback so the LLM regenerates).
          </p>
          <div style={styles.pendingGrid}>
            {pending.map((p) => (
              <div key={`${p.email}-${p.type}`} style={styles.card}>
                <div style={styles.cardHeader}>
                  <strong>{p.name || p.email}</strong>
                  <span style={styles.badge}>
                    {p.type === 'insights' ? 'Insights' : p.type === 'email' ? 'Email draft' : 'Reply to lead (QUESTION)'}
                  </span>
                </div>
                {p.company && <div style={styles.muted}>{p.company}</div>}

                {p.type === 'question_reply' ? (
                  <>
                    <h4 style={styles.expandedLabel}>Their reply</h4>
                    <pre style={styles.pre}>{p.reply || '—'}</pre>
                    <h4 style={styles.expandedLabel}>Your response</h4>
                    <textarea
                      style={styles.textarea}
                      value={getContent(p)}
                      onChange={(e) => setContent(p, e.target.value)}
                      placeholder="Type your reply to send…"
                      spellCheck={false}
                    />
                    <div style={styles.cardActions}>
                      <button type="button" onClick={() => approve(p)} style={styles.approveBtn}>Send response</button>
                    </div>
                  </>
                ) : (
                  <>
                    <textarea
                      style={styles.textarea}
                      value={getContent(p)}
                      onChange={(e) => setContent(p, e.target.value)}
                      placeholder={p.type === 'insights' ? 'Edit insights…' : 'Edit email draft…'}
                      spellCheck={false}
                    />
                    <div style={styles.cardActions}>
                      <input
                        type="text"
                        placeholder="Feedback (if rejecting)"
                        value={rejectFeedback[`${p.email}-${p.type}`] || ''}
                        onChange={(e) => setReject(`${p.email}-${p.type}`, e.target.value)}
                        style={styles.input}
                      />
                      <div style={styles.buttons}>
                        <button type="button" onClick={() => approve(p)} style={styles.approveBtn}>Approve</button>
                        <button type="button" onClick={() => reject(p.email, p.type)} style={styles.rejectBtn}>Reject</button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      <section style={styles.section}>
        <h2 style={styles.sectionTitle}>Leads ({leads.length})</h2>
        <div style={styles.tableWrap}>
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={{ ...styles.th, width: 36 }}></th>
                <th style={styles.th}>Name</th>
                <th style={styles.th}>Email</th>
                <th style={styles.th}>Company</th>
                <th style={styles.th}>Status</th>
                <th style={styles.th}>Classification</th>
                <th style={styles.th}>Meeting</th>
              </tr>
            </thead>
            <tbody>
              {leads.map((lead) => (
                <React.Fragment key={lead.email}>
                  <tr
                    style={{ ...styles.tr, cursor: 'pointer' }}
                    onClick={() => setExpandedLead((e) => (e === lead.email ? null : lead.email))}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(ev) => (ev.key === 'Enter' || ev.key === ' ') && setExpandedLead((e) => (e === lead.email ? null : lead.email))}
                    title="Click to view insights and email"
                  >
                    <td style={styles.td}>
                      <span style={styles.expandIcon}>{expandedLead === lead.email ? '▼' : '▶'}</span>
                    </td>
                    <td style={styles.td}>{lead.name}</td>
                    <td style={styles.td}>{lead.email}</td>
                    <td style={styles.td}>{lead.company}</td>
                    <td style={styles.td}><span style={statusStyle(lead.status)}>{lead.status}</span></td>
                    <td style={styles.td}>{lead.classification || '–'}</td>
                    <td style={styles.td}>{lead.meeting_booked ? 'Yes' : '–'}</td>
                  </tr>
                  {expandedLead === lead.email && (
                    <tr style={styles.tr}>
                      <td colSpan={7} style={styles.expandedCell}>
                        <div style={styles.expandedWrap}>
                          <div style={styles.expandedBlock}>
                            <h4 style={styles.expandedLabel}>Insights (AI research for {lead.company})</h4>
                            <p style={styles.transparencyNote}>Generated from company research. You approved or edited this before the email was drafted.</p>
                            <pre style={styles.pre}>
                              {lead.insights || '— No insights yet —'}
                            </pre>
                          </div>
                          <div style={styles.expandedBlock}>
                            <h4 style={styles.expandedLabel}>Email draft / sent</h4>
                            <p style={styles.transparencyNote}>Nothing is sent without your approval. This is what was (or will be) sent to the lead.</p>
                            <pre style={styles.pre}>
                              {lead.email_draft || '— No email draft yet —'}
                            </pre>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              ))}
            </tbody>
          </table>
        </div>
        {leads.length === 0 && <p style={styles.muted}>No leads yet. Upload an Excel file (columns: Name, Email, Company).</p>}
      </section>

      <footer style={styles.footer}>
        <p style={styles.muted}>
          Nothing is sent without your approval. Data is stored in your backend only — use Export CSV to backup. For production deploy, see DEPLOY.md. Data handling: PRIVACY.md.
        </p>
      </footer>
    </div>
  )
}

function statusStyle(status) {
  const colors = {
    INIT: '#8b949e',
    INSIGHTS_GENERATED: '#58a6ff',
    INSIGHTS_APPROVED: '#58a6ff',
    EMAIL_DRAFTED: '#58a6ff',
    EMAIL_APPROVED: '#58a6ff',
    EMAIL_SENT: '#3fb950',
    REPLIED: '#d29922',
    MEETING_BOOKED: '#3fb950',
    CLOSED: '#8b949e',
    ERROR: '#f85149',
    EMAIL_FAILED: '#f85149',
  }
  return { color: colors[status] || '#8b949e', fontWeight: 500 }
}

const styles = {
  container: { maxWidth: 1100, margin: '0 auto', padding: '24px 20px' },
  header: { marginBottom: 24 },
  title: { fontSize: 28, fontWeight: 700, margin: '0 0 8px' },
  subtitle: { color: 'var(--textMuted)', margin: 0, fontSize: 15, maxWidth: 560 },
  onboarding: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 16,
    padding: '14px 18px',
    marginBottom: 24,
    background: 'rgba(88, 166, 255, 0.1)',
    border: '1px solid var(--accent)',
    borderRadius: 10,
    fontSize: 14,
  },
  onboardingDismiss: { background: 'var(--accent)', color: '#fff', padding: '8px 14px' },
  toastContainer: { position: 'fixed', top: 16, right: 16, zIndex: 1000, display: 'flex', flexDirection: 'column', gap: 8 },
  toast: {
    padding: '12px 16px',
    borderRadius: 8,
    border: '1px solid',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    minWidth: 280,
    boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
  },
  toastDismiss: { background: 'transparent', color: 'inherit', padding: '0 4px', fontSize: 18 },
  statsStrip: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '16px 24px',
    marginBottom: 20,
    padding: '12px 16px',
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: 8,
    fontSize: 14,
  },
  error: {
    background: 'rgba(248,81,73,0.15)',
    border: '1px solid var(--danger)',
    color: 'var(--danger)',
    padding: '12px 16px',
    borderRadius: 8,
    marginBottom: 24,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
  },
  dismiss: { background: 'transparent', color: 'inherit', padding: '4px 8px' },
  actions: { display: 'flex', gap: 12, marginBottom: 32, flexWrap: 'wrap' },
  button: {
    background: 'var(--surface2)',
    color: 'var(--text)',
    border: '1px solid var(--border)',
  },
  primary: { background: 'var(--accent)', color: '#fff', borderColor: 'var(--accent)' },
  fileLabel: { display: 'inline-block' },
  section: { marginBottom: 40 },
  sectionTitle: { fontSize: 18, fontWeight: 600, marginBottom: 16 },
  pendingGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(380px, 1fr))', gap: 20 },
  card: {
    background: 'var(--surface)',
    border: '1px solid var(--border)',
    borderRadius: 12,
    padding: 20,
  },
  cardHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 },
  badge: { fontSize: 12, color: 'var(--textMuted)', background: 'var(--surface2)', padding: '4px 8px', borderRadius: 6 },
  pre: {
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: 13,
    maxHeight: 220,
    overflow: 'auto',
    background: 'var(--bg)',
    padding: 12,
    borderRadius: 8,
    margin: '12px 0',
    border: '1px solid var(--border)',
  },
  textarea: {
    width: '100%',
    minHeight: 180,
    maxHeight: 280,
    resize: 'vertical',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: 13,
    fontFamily: 'inherit',
    background: 'var(--bg)',
    color: 'var(--text)',
    padding: 12,
    borderRadius: 8,
    margin: '12px 0',
    border: '1px solid var(--border)',
    boxSizing: 'border-box',
  },
  cardActions: { marginTop: 12 },
  input: { width: '100%', marginBottom: 10 },
  buttons: { display: 'flex', gap: 8 },
  approveBtn: { background: 'var(--success)', color: '#fff' },
  rejectBtn: { background: 'var(--danger)', color: '#fff' },
  tableWrap: { overflowX: 'auto', border: '1px solid var(--border)', borderRadius: 8 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: { textAlign: 'left', padding: '12px 14px', background: 'var(--surface2)', fontWeight: 600, fontSize: 13 },
  tr: { borderBottom: '1px solid var(--border)' },
  td: { padding: '12px 14px', fontSize: 14 },
  expandIcon: { fontSize: 10, color: 'var(--textMuted)', cursor: 'pointer' },
  expandedCell: { padding: 0, verticalAlign: 'top', borderBottom: '1px solid var(--border)', background: 'var(--bg)' },
  expandedWrap: { padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: 20 },
  expandedBlock: {},
  expandedLabel: { fontSize: 12, fontWeight: 600, color: 'var(--textMuted)', margin: '0 0 8px' },
  transparencyNote: { fontSize: 11, color: 'var(--textMuted)', margin: '0 0 6px', fontStyle: 'italic' },
  muted: { color: 'var(--textMuted)', fontSize: 14 },
  footer: { marginTop: 48, paddingTop: 24, borderTop: '1px solid var(--border)' },
  code: { background: 'var(--surface2)', padding: '2px 6px', borderRadius: 4, fontSize: 13 },
}
