// A reason to write to a company today (F11, ADR-049).
//
// The screen exists in the same change as the tables, which is the standing rule from here on:
// eleven slices shipped with no screen and the operator could not see any of them.
//
// What this screen will not do is most of its design. A signal is an entry on a list somebody
// reads — there is no "draft an email about this" button, because `approval_mode = review_all`
// means a person writes and approves every first email and a signal does not get to be an
// exception. Acting on one means opening the lead it matched, or looking a contact up on your
// own provider key, which is a separate and explicit spend.

import { useCallback, useEffect, useState } from 'react'

import { deleteJson, getJson, postJson } from './api'
import { Drawer, Empty, Icons, SkeletonList } from './ui'

function Field({ label, hint, children }) {
  return (
    <div className="field">
      <label className="form-label">{label}</label>
      {children}
      {hint ? <p className="hint">{hint}</p> : null}
    </div>
  )
}

function useLoad(path, onError) {
  const [data, setData] = useState(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(() => {
    setFailed(false)
    getJson(path)
      .then(setData)
      .catch((err) => {
        setFailed(true)
        onError(err)
      })
  }, [path, onError])

  useEffect(load, [load])
  return { data, failed, reload: load }
}

function Loading({ failed, data, what }) {
  if (failed) return <p className="muted">Could not load {what}.</p>
  if (!data) return <SkeletonList count={2} rows={2} />
  return null
}

export default function Signals({ onError, onToast, mayAdminister }) {
  const watchlist = useLoad('/watchlist', onError)
  const signals = useLoad('/signals?state=new', onError)
  // What we know about one company, fetched on a click rather than per row: the panel is for
  // reading one company carefully, and loading it for every watch would be a query each to
  // answer a question nobody asked yet.
  const [known, setKnown] = useState(null)
  const [domain, setDomain] = useState('')
  const [label, setLabel] = useState('')
  const [source, setSource] = useState('')
  const [slug, setSlug] = useState('')
  const [busy, setBusy] = useState(false)

  const sources = watchlist.data?.sources || []

  const watch = (event) => {
    event.preventDefault()
    setBusy(true)
    postJson('/watchlist', {
      json: {
        domain,
        label: label || null,
        ats_source: source || null,
        ats_slug: slug || null,
      },
    })
      .then((body) => {
        setDomain('')
        setLabel('')
        setSlug('')
        onToast(body.note)
        watchlist.reload()
      })
      .catch(onError)
      .finally(() => setBusy(false))
  }

  const move = (id, state) =>
    postJson(`/signals/${id}/state`, { json: { state } })
      .then(() => {
        onToast(state === 'acted' ? 'Marked as acted on.' : 'Dismissed.')
        signals.reload()
      })
      .catch(onError)

  return (
    <>
      <h1 className="page-title">Signals</h1>
      <p className="page-note">
        Companies you watch, and what has happened at them. A new job posting is a reason to write
        to a company today — <strong>nothing here drafts or sends anything</strong>, and no signal
        ever creates a lead.
      </p>

      <section className="card">
        <h2>
          What has happened{' '}
          {signals.data?.without_a_lead ? (
            <span className="badge">{signals.data.without_a_lead} with nobody to write to</span>
          ) : null}
        </h2>
        <Loading {...signals} what="your signals" />
        {signals.data ? (
          signals.data.signals.length === 0 ? (
            <Empty icon={Icons.inbox} title="Nothing yet">
              Watch a company below. Its board is read once a day, and new roles appear here.
            </Empty>
          ) : (
            <ul className="list">
              {signals.data.signals.map((row) => (
                <li key={row.id} className="list-row">
                  <div>
                    <strong>{row.title}</strong> <span className="badge">{row.kind}</span>
                    <span className="badge">{row.source}</span>
                    <div className="hint">
                      {row.domain}
                      {row.observed_at
                        ? ` — posted ${new Date(row.observed_at).toLocaleDateString()}`
                        : ''}
                    </div>
                    {/* The distinction the screen exists to make: a company worth writing to
                        and somebody to write to are two different things. */}
                    <div className="hint">
                      {row.lead_name
                        ? `You already have a lead here: ${row.lead_name}`
                        : 'No lead at this company yet — look a contact up on the Job hunt screen, which spends one of your own credits.'}
                    </div>
                  </div>
                  <div className="btn-row">
                    {row.url ? (
                      <a className="btn" href={row.url} target="_blank" rel="noreferrer">
                        Open
                      </a>
                    ) : null}
                    <button type="button" className="btn" onClick={() => move(row.id, 'acted')}>
                      Acted on
                    </button>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => move(row.id, 'dismissed')}
                    >
                      Dismiss
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>

      {known ? (
        <Drawer
          title={`What we know about ${known.domain}`}
          badges={[`${known.statements.length} lines`]}
          onClose={() => setKnown(null)}
        >
          <p className="hint">{known.note}</p>
          {known.statements.length === 0 ? (
            <p className="muted">
              Nothing recorded yet. A line appears here when a page we can point at says
              something about them.
            </p>
          ) : (
            <ul className="list">
              {known.statements.map((one, index) => (
                <li key={index} className="list-row">
                  <div>
                    <strong>{one.sentence}</strong>
                    <p className="hint">
                      {one.observed_at
                        ? `Seen ${new Date(one.observed_at).toLocaleDateString()} — `
                        : ''}
                      {/* The source is shown with the line, never beside the set: a line whose
                          source you cannot open is a line you cannot repeat to a stranger. */}
                      {one.source_url ? (
                        <a href={one.source_url} target="_blank" rel="noreferrer">
                          source
                        </a>
                      ) : one.from_a_message ? (
                        'from a message they sent you'
                      ) : (
                        'no source'
                      )}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Drawer>
      ) : null}

      <section className="card">
        <h2>Watched companies</h2>
        <p className="hint">{watchlist.data?.note || ''}</p>
        <Loading {...watchlist} what="your watchlist" />
        {watchlist.data ? (
          watchlist.data.watchlist.length === 0 ? (
            <Empty icon={Icons.inbox} title="No companies watched">
              Add one below with its careers board, and its new roles arrive by themselves.
            </Empty>
          ) : (
            <ul className="list">
              {watchlist.data.watchlist.map((row) => (
                <li key={row.id} className="list-row">
                  <div>
                    <strong>{row.label || row.domain}</strong>
                    {row.paused ? <span className="badge">paused</span> : null}
                    {row.ats_source ? (
                      <span className="badge">
                        {row.ats_source}/{row.ats_slug}
                      </span>
                    ) : (
                      <span className="badge">no board</span>
                    )}
                    <div className="hint">
                      {row.domain}
                      {' — '}
                      {row.last_signal_at
                        ? `last signal ${new Date(row.last_signal_at).toLocaleDateString()}`
                        : row.ats_source
                          ? 'nothing has arrived yet'
                          : 'no board given, so nothing will arrive'}
                    </div>
                  </div>
                  <div className="btn-row">
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        getJson(`/graph?domain=${encodeURIComponent(row.domain)}`)
                          .then(setKnown)
                          .catch(onError)
                      }
                    >
                      What we know
                    </button>
                    {mayAdminister ? (
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          postJson(`/watchlist/${row.id}/pause`, { json: { paused: !row.paused } })
                            .then(() => {
                              onToast(row.paused ? 'Watching again.' : 'Paused.')
                              watchlist.reload()
                            })
                            .catch(onError)
                        }
                      >
                        {row.paused ? 'Resume' : 'Pause'}
                      </button>
                    ) : null}
                    {mayAdminister ? (
                      <button
                        type="button"
                        className="btn"
                        onClick={() =>
                          deleteJson(`/watchlist/${row.id}`)
                            .then((body) => {
                              onToast(body.note)
                              watchlist.reload()
                            })
                            .catch(onError)
                        }
                      >
                        Forget
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}

        {mayAdminister ? (
          <form onSubmit={watch} style={{ marginTop: 14 }}>
            <Field
              label="Company domain"
              hint="The company's own domain, not a job board's. A URL is fine — it is reduced to the domain."
            >
              <input
                value={domain}
                onChange={(event) => setDomain(event.target.value)}
                placeholder="acme.example"
              />
            </Field>
            <Field label="Name" hint="Optional. A domain is true and a poor label.">
              <input value={label} onChange={(event) => setLabel(event.target.value)} />
            </Field>
            <Field
              label="Careers board"
              hint="Without one, nothing produces signals for this company yet."
            >
              <select value={source} onChange={(event) => setSource(event.target.value)}>
                <option value="">No board</option>
                {sources.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </Field>
            {source ? (
              <Field
                label="Slug on that board"
                hint="The company's own name in the board URL — jobs.lever.co/acme is `acme`."
              >
                <input value={slug} onChange={(event) => setSlug(event.target.value)} />
              </Field>
            ) : null}
            <button
              type="submit"
              className="btn"
              data-variant="primary"
              disabled={busy || !domain}
            >
              Watch this company
            </button>
          </form>
        ) : null}
      </section>
    </>
  )
}
