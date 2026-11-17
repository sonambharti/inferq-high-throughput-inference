import React from 'react'
import { fmt } from '../api.js'

const STATES = [
  ['submitted', 'Submitted'],
  ['succeeded', 'Succeeded'],
  ['failed', 'Failed'],
  ['waiting', 'Waiting'],
  ['in_flight', 'In flight'],
  ['expired', 'Expired'],
  ['rejected', 'Rejected'],
]

export default function Accounting({ metrics }) {
  const t = metrics.totals
  const lat = metrics.latency?._all || {}
  return (
    <section className="card">
      <div className="row-between">
        <h2>Request accounting</h2>
        <span className={`pill ${t.invariant_ok ? 'ok' : 'bad'}`}>
          submitted = waiting + in flight + succeeded + failed + expired + rejected:{' '}
          {t.invariant_ok ? 'holds' : 'BROKEN'}
        </span>
      </div>
      <div className="stats">
        {STATES.map(([k, label]) => (
          <div key={k} className={`stat s-${k}`}>
            <div className="stat-v">{fmt(t[k])}</div>
            <div className="stat-l">{label}</div>
          </div>
        ))}
        <div className="stat">
          <div className="stat-v">
            {lat.p50_ms ? `${Math.round(lat.p50_ms)} / ${Math.round(lat.p95_ms)} / ${Math.round(lat.p99_ms)}` : '—'}
          </div>
          <div className="stat-l">Latency p50 / p95 / p99 (ms)</div>
        </div>
      </div>
    </section>
  )
}
