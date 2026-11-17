import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

// Lightweight in-browser load source for demos (a few hundred req/s).
// For real load tests use the CLI load generator: python -m tools.loadgen
export default function QuickLoad({ models }) {
  const [rate, setRate] = useState(200)
  const [model, setModel] = useState('')
  const [running, setRunning] = useState(false)
  const [sent, setSent] = useState({ ok: 0, rejected: 0, errors: 0 })
  const timer = useRef(null)

  useEffect(() => {
    if (!model && models.length) setModel(models[0])
  }, [models, model])

  useEffect(() => {
    if (!running) return
    let i = 0
    const start = performance.now()
    timer.current = setInterval(() => {
      const due = Math.floor(((performance.now() - start) / 1000) * rate)
      while (i < due) {
        i++
        api
          .submit({ model, estimated_tokens: 1000, payload: { prompt: 'dashboard load' }, intended_at: Date.now() / 1000 })
          .then((r) => setSent((s) => (r.state === 'rejected' ? { ...s, rejected: s.rejected + 1 } : { ...s, ok: s.ok + 1 })))
          .catch(() => setSent((s) => ({ ...s, errors: s.errors + 1 })))
      }
    }, 20)
    return () => clearInterval(timer.current)
  }, [running, rate, model])

  return (
    <div className="card">
      <h2>Quick load (demo)</h2>
      <p className="muted small">
        Sends single requests from the browser. For capacity tests use <code>python -m tools.loadgen</code> or the
        scenario runner.
      </p>
      <form className="limits" onSubmit={(e) => { e.preventDefault(); setRunning(!running) }}>
        <label>
          Model
          <select value={model} onChange={(e) => setModel(e.target.value)}>
            {models.map((m) => <option key={m}>{m}</option>)}
          </select>
        </label>
        <label>
          Req / s
          <input type="number" min="1" max="1000" value={rate} onChange={(e) => setRate(Number(e.target.value))} />
        </label>
        <button type="submit" className={running ? 'danger' : ''}>{running ? 'Stop' : 'Start'}</button>
      </form>
      <div className="mini-stats">
        <span>accepted <b>{sent.ok}</b></span>
        <span>rejected <b>{sent.rejected}</b></span>
        <span>errors <b>{sent.errors}</b></span>
      </div>
    </div>
  )
}
