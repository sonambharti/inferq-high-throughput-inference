import React, { useEffect, useRef, useState } from 'react'
import { api, API } from './api.js'
import Accounting from './components/Accounting.jsx'
import ModelPanel from './components/ModelPanel.jsx'
import Batches from './components/Batches.jsx'
import QuickLoad from './components/QuickLoad.jsx'

const KEEP_SECONDS = 600

export default function App() {
  const [metrics, setMetrics] = useState(null)
  const [series, setSeries] = useState({}) // model -> points[]
  const [limitEvents, setLimitEvents] = useState([])
  const [online, setOnline] = useState(false)
  const lastT = useRef(0)

  useEffect(() => {
    let stop = false
    const tick = async () => {
      try {
        const [m, ts] = await Promise.all([api.metrics(), api.timeseries(lastT.current)])
        if (stop) return
        setMetrics(m)
        setOnline(true)
        setLimitEvents(ts.limit_events || [])
        if (ts.points.length) {
          lastT.current = ts.points[ts.points.length - 1].t
          setSeries((prev) => {
            const next = { ...prev }
            const cutoff = Date.now() / 1000 - KEEP_SECONDS
            for (const p of ts.points) {
              const arr = next[p.model] ? [...next[p.model]] : []
              arr.push(p)
              next[p.model] = arr
            }
            for (const k of Object.keys(next)) next[k] = next[k].filter((p) => p.t >= cutoff)
            return next
          })
        }
      } catch {
        if (!stop) setOnline(false)
      }
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => {
      stop = true
      clearInterval(id)
    }
  }, [])

  const reset = async () => {
    await api.reset()
    lastT.current = 0
    setSeries({})
  }

  const models = metrics ? Object.keys(metrics.models) : []

  return (
    <div className="page">
      <header className="top">
        <div>
          <h1>inferq</h1>
          <p className="muted">High-throughput inference dispatcher · strict rolling-60s RPM/TPM limits</p>
        </div>
        <div className="top-right">
          <span className={`pill ${online ? 'ok' : 'bad'}`}>{online ? 'connected' : 'offline'}</span>
          <code className="muted small">{API}</code>
          <button className="ghost" onClick={reset} title="Clear counters and charts for a new run">
            Reset counters
          </button>
        </div>
      </header>

      {!metrics && (
        <div className="card">
          <p>
            Waiting for the control plane at <code>{API}</code>. Start it with{' '}
            <code>python -m inferq.api</code> (see README).
          </p>
        </div>
      )}

      {metrics && (
        <>
          <Accounting metrics={metrics} />
          <section className="grid">
            {models.map((m) => (
              <ModelPanel
                key={m}
                name={m}
                stats={metrics.models[m]}
                latency={metrics.latency?.[m]}
                points={series[m] || []}
                events={limitEvents.filter((e) => e.model === m)}
              />
            ))}
          </section>
          <section className="grid two">
            <QuickLoad models={models} />
            <Batches models={models} />
          </section>
        </>
      )}
      <footer className="muted small">
        Observed values come from the dispatcher's ledger; the independent auditor (CLI) re-checks them from the
        provider simulator's own records.
      </footer>
    </div>
  )
}
