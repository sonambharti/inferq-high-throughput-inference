import React, { useEffect, useState } from 'react'
import { api, API, fmt } from '../api.js'

export default function Batches({ models }) {
  const [batches, setBatches] = useState([])
  const [size, setSize] = useState(1000)
  const [callback, setCallback] = useState('http://127.0.0.1:9100/callback')
  const [msg, setMsg] = useState('')
  const [open, setOpen] = useState(null)

  useEffect(() => {
    let stop = false
    const load = async () => {
      try {
        const r = await api.batches()
        if (!stop) setBatches(r.batches)
      } catch {}
    }
    load()
    const id = setInterval(load, 1500)
    return () => {
      stop = true
      clearInterval(id)
    }
  }, [])

  const submit = async (e) => {
    e.preventDefault()
    const n = Number(size)
    const requests = Array.from({ length: n }, (_, i) => ({
      model: models[i % models.length],
      estimated_tokens: 1000,
      payload: { prompt: `dashboard batch item ${i}` },
      request_id: `ui-${Date.now()}-${i}`,
    }))
    const t = performance.now()
    try {
      const r = await api.createBatch({ requests, callback_url: callback || undefined })
      setMsg(`Batch ${r.batch_id} acknowledged in ${Math.round(performance.now() - t)} ms`)
    } catch (err) {
      setMsg(`Error: ${err.message}`)
    }
  }

  return (
    <div className="card">
      <h2>Asynchronous batches</h2>
      <form className="limits" onSubmit={submit}>
        <label>
          Size
          <input type="number" min="1" max="100000" value={size} onChange={(e) => setSize(e.target.value)} />
        </label>
        <label className="grow">
          Callback URL
          <input value={callback} onChange={(e) => setCallback(e.target.value)} placeholder="optional" />
        </label>
        <button type="submit" disabled={!models.length}>Submit batch</button>
      </form>
      {msg && <p className="muted small">{msg}</p>}

      <table className="table">
        <thead>
          <tr>
            <th>Batch</th>
            <th>Status</th>
            <th>Total</th>
            <th>OK</th>
            <th>Failed</th>
            <th>Duration</th>
            <th>Callback</th>
          </tr>
        </thead>
        <tbody>
          {batches.map((b) => (
            <React.Fragment key={b.batch_id}>
              <tr className="click" onClick={() => setOpen(open === b.batch_id ? null : b.batch_id)}>
                <td><code>{b.batch_id}</code></td>
                <td><span className={`pill ${statusClass(b.status)}`}>{b.status}</span></td>
                <td>{fmt(b.total)}</td>
                <td>{fmt(b.successful)}</td>
                <td>{fmt(b.failed)}</td>
                <td>{b.duration_s ? `${b.duration_s}s` : `${fmt(b.remaining)} left`}</td>
                <td>
                  {b.callback ? (
                    <span className={`pill ${b.callback.status === 'delivered' ? 'ok' : b.callback.status === 'failed' ? 'bad' : 'warn'}`}>
                      {b.callback.status} ({b.callback.attempts})
                    </span>
                  ) : (
                    <span className="muted">none</span>
                  )}
                </td>
              </tr>
              {open === b.batch_id && (
                <tr>
                  <td colSpan={7} className="detail">
                    <div className="small">
                      Counts: {JSON.stringify(b.counts)} ·{' '}
                      <a href={`${API}/v1/batches/${b.batch_id}/results`} target="_blank" rel="noreferrer">results</a>
                      {b.summary_sha256 && <> · sha256 <code>{b.summary_sha256.slice(0, 16)}…</code></>}
                    </div>
                    {b.callback?.history?.length > 0 && (
                      <table className="table inner">
                        <thead>
                          <tr><th>Attempt</th><th>At</th><th>HTTP</th><th>Error</th></tr>
                        </thead>
                        <tbody>
                          {b.callback.history.map((h) => (
                            <tr key={h.attempt}>
                              <td>{h.attempt}</td>
                              <td>{new Date(h.at * 1000).toLocaleTimeString()}</td>
                              <td>{h.status_code ?? '—'}</td>
                              <td className="small">{h.error || ''}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </td>
                </tr>
              )}
            </React.Fragment>
          ))}
          {!batches.length && (
            <tr><td colSpan={7} className="muted">No batches yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  )
}

const statusClass = (s) => (s === 'completed' ? 'ok' : s === 'processing' ? 'warn' : s === 'interrupted' ? 'bad' : 'info')
