// What the system is doing to itself, and the two controls an operator actually needs.
//
// `/admin/alerts`, `/admin/dead-jobs`, `/admin/jobs/{id}/requeue`, `/admin/worker` and
// `/admin/usage` existed for three phases with no screen, and the cost of that is recorded in
// this repository: **three dead reply-drafting jobs were recovered by hand with SQL against
// production** after a deploy, because the only way to see them was a query and the only way to
// retry one was an UPDATE. A dead job nobody can requeue from a screen is a dead job somebody
// eventually fixes with psql.
//
// Two things on this screen are deliberately not here:
//
//   * **No way to raise the send mode.** That is owner-only and lives in Settings (ADR-010),
//     because a guard that answers differently depending on which screen you are on is a guard
//     nobody can reason about.
//   * **No "resume everything" button.** ADR-034's automatic pause may only ever restrict;
//     resuming is a person's decision, taken per mailbox, where they can see why it stopped.

import { useCallback, useEffect, useState } from 'react'

import { getJson, postJson } from './api'
import { Empty, Icons, SkeletonList } from './ui'

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

export default function Operations({ onError, onToast }) {
  const worker = useLoad('/admin/worker', onError)
  const alerts = useLoad('/admin/alerts', onError)
  const dead = useLoad('/admin/dead-jobs', onError)
  const usage = useLoad('/admin/usage', onError)

  const unacked = (alerts.data?.alerts || []).filter((row) => !row.acked_at)

  return (
    <>
      <h1 className="page-title">Operations</h1>
      <p className="page-note">
        Whether the worker is alive, what it has complained about, and what it gave up on. Point
        an external monitor at <code>/ready</code> rather than at this screen —{' '}
        <code>/health</code> answers from the API&apos;s own memory and cannot see another
        process.
      </p>

      <section className="card">
        <h2>Worker</h2>
        <Loading {...worker} what="the worker status" />
        {worker.data ? (
          <>
            <p>
              <span className="badge">{worker.data.running ? 'running' : 'not running'}</span>{' '}
              {worker.data.worker_id ? <code>{worker.data.worker_id}</code> : null}
            </p>
            <p className="hint">
              {worker.data.detail ||
                `Last heartbeat ${worker.data.seen_seconds_ago}s ago, started ${new Date(
                  worker.data.started_at
                ).toLocaleString()}.`}
            </p>
            {worker.data.running ? null : (
              <p className="muted">
                Nothing is being researched, drafted or sent while this is false, and from
                outside the symptom is simply that leads stop moving.
              </p>
            )}
          </>
        ) : null}
      </section>

      <section className="card">
        <h2>Alerts {unacked.length ? <span className="badge">{unacked.length} new</span> : null}</h2>
        <p className="hint">
          One hour&apos;s cool-down per kind, and acknowledging re-arms it. A mailbox id is part of
          the kind, so the first mailbox paused does not silence every other one.
        </p>
        <Loading {...alerts} what="the alerts" />
        {alerts.data ? (
          alerts.data.alerts.length === 0 ? (
            <Empty icon={Icons.inbox} title="Nothing has raised an alert">
              Which is the expected state. The table is the record and the webhook is only the
              nudge, so an alert is never lost because a push failed.
            </Empty>
          ) : (
            <ul className="list">
              {alerts.data.alerts.map((row) => (
                <li key={row.id} className="list-row">
                  <div>
                    <strong>{row.kind}</strong> <span className="badge">{row.severity}</span>
                    <span className="badge">{row.scope}</span>
                    {row.acked_at ? <span className="badge">acknowledged</span> : null}
                    <div>{row.message}</div>
                    <p className="hint">{new Date(row.created_at).toLocaleString()}</p>
                  </div>
                  {row.acked_at ? null : (
                    <button
                      type="button"
                      className="btn"
                      onClick={() =>
                        postJson(`/admin/alerts/${row.id}/ack`, { json: {} })
                          .then(() => {
                            onToast('Acknowledged, and that kind can alert again.')
                            alerts.reload()
                          })
                          .catch(onError)
                      }
                    >
                      Acknowledge
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>

      <section className="card">
        <h2>Dead jobs</h2>
        <p className="hint">
          A job that used up its attempts. Requeueing resets them, because those retries were
          spent on a different version of the code — which is the usual reason a job here is
          worth another go.
        </p>
        <Loading {...dead} what="the dead jobs" />
        {dead.data ? (
          dead.data.jobs.length === 0 ? (
            <Empty icon={Icons.inbox} title="No dead jobs">
              Nothing has given up.
            </Empty>
          ) : (
            <ul className="list">
              {dead.data.jobs.map((job) => (
                <li key={job.id} className="list-row">
                  <div>
                    <strong>{job.kind}</strong>{' '}
                    <span className="badge">{job.attempts} attempts</span>
                    <p className="hint">{job.last_error || 'No error recorded.'}</p>
                  </div>
                  <button
                    type="button"
                    className="btn"
                    onClick={() =>
                      postJson(`/admin/jobs/${job.id}/requeue`, { json: {} })
                        .then(() => {
                          onToast('Requeued with its attempts reset.')
                          dead.reload()
                        })
                        .catch(onError)
                    }
                  >
                    Requeue
                  </button>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>

      <section className="card">
        <h2>Model usage today</h2>
        <p className="hint">
          Per entry in the chain. The free tiers are the real ceiling on this system, which is why
          a search or a draft falling back to the next entry is normal rather than a fault.
        </p>
        <Loading {...usage} what="the model usage" />
        {usage.data ? (
          usage.data.today.length === 0 ? (
            <Empty icon={Icons.inbox} title="No model calls today">
              Research and drafting have not run yet today.
            </Empty>
          ) : (
            <ul className="list">
              {usage.data.today.map((row) => (
                <li key={row.entry} className="list-row">
                  <div>
                    <strong>{row.entry}</strong>
                    <p className="hint">
                      {row.requests} requests, {row.tokens.toLocaleString()} tokens
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </section>
    </>
  )
}
