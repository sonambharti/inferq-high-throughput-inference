import React, { useEffect, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { api, fmt } from '../api.js'

const t0 = (points) => (points.length ? points[0].t : 0)

export default function ModelPanel({ name, stats, latency, points, events }) {
  const [rpm, setRpm] = useState(stats.rpm_limit)
  const [tpm, setTpm] = useState(stats.tpm_limit)
  const [msg, setMsg] = useState('')
  const [dirty, setDirty] = useState(false)

  useEffect(() => {
    if (!dirty) {
      setRpm(stats.rpm_limit)
      setTpm(stats.tpm_limit)
    }
  }, [stats.rpm_limit, stats.tpm_limit, dirty])

  const apply = async (e) => {
    e.preventDefault()
    try {
      const r = await api.setLimits(name, Number(rpm), Number(tpm))
      setMsg(`Applied at ${new Date().toLocaleTimeString()} · ${r.expired} waiting requests expired · provider: ${r.provider_update}`)
      setDirty(false)
    } catch (err) {
      setMsg(`Error: ${err.message}`)
    }
  }

  const base = t0(points)
  const data = points.map((p) => ({
    s: Math.round(p.t - base),
    window: p.window_requests,
    limit: p.rpm_limit,
    tokWindow: p.window_tokens,
    tokLimit: p.tpm_limit,
    sent: p.sent,
    completed: p.completed,
    waiting: p.waiting,
  }))
  const reqPct = stats.rpm_limit ? stats.window_requests / stats.rpm_limit : 0
  const tokPct = stats.tpm_limit ? stats.window_tokens / stats.tpm_limit : 0

  return (
    <div className="card">
      <div className="row-between">
        <h2>{name}</h2>
        <span className="muted small">{events.length} limit change{events.length === 1 ? '' : 's'} recorded</span>
      </div>

      <div className="gauges">
        <Gauge label="Requests in trailing 60 s" value={stats.window_requests} limit={stats.rpm_limit} pct={reqPct} />
        <Gauge label="Tokens in trailing 60 s" value={stats.window_tokens} limit={stats.tpm_limit} pct={tokPct} />
      </div>

      <div className="chart">
        <ResponsiveContainer width="100%" height={210}>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#eef0f3" />
            <XAxis dataKey="s" tick={{ fontSize: 11 }} unit="s" />
            <YAxis tick={{ fontSize: 11 }} tickFormatter={fmt} width={48} />
            <Tooltip formatter={(v) => fmt(v)} labelFormatter={(l) => `t = ${l}s`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line type="stepAfter" dataKey="limit" name="RPM limit" stroke="#dc2626" strokeDasharray="6 4" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="window" name="requests / trailing 60 s" stroke="#2563eb" dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="chart">
        <ResponsiveContainer width="100%" height={150}>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="#eef0f3" />
            <XAxis dataKey="s" tick={{ fontSize: 11 }} unit="s" />
            <YAxis tick={{ fontSize: 11 }} tickFormatter={fmt} width={48} />
            <Tooltip formatter={(v) => fmt(v)} labelFormatter={(l) => `t = ${l}s`} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            <Line type="monotone" dataKey="sent" name="sent / s" stroke="#7c3aed" dot={false} isAnimationActive={false} />
            <Line type="monotone" dataKey="completed" name="completed / s" stroke="#16a34a" dot={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <div className="mini-stats">
        <span>waiting <b>{fmt(stats.waiting)}</b></span>
        <span>in flight <b>{fmt(stats.in_flight)}</b></span>
        <span>completed <b>{fmt(stats.completed)}</b></span>
        <span>expired <b>{fmt(stats.expired)}</b></span>
        <span>rejected <b>{fmt(stats.rejected)}</b></span>
        <span>p95 <b>{latency?.p95_ms ? `${Math.round(latency.p95_ms)} ms` : '—'}</b></span>
      </div>

      <form className="limits" onSubmit={apply}>
        <label>
          RPM
          <input type="number" min="0" value={rpm} onChange={(e) => { setRpm(e.target.value); setDirty(true) }} />
        </label>
        <label>
          TPM
          <input type="number" min="0" value={tpm} onChange={(e) => { setTpm(e.target.value); setDirty(true) }} />
        </label>
        <button type="submit">Apply limits live</button>
        <button type="button" className="ghost" onClick={() => { setRpm(5000); setDirty(true) }}>RPM → 5,000</button>
      </form>
      {msg && <p className="muted small">{msg}</p>}
    </div>
  )
}

function Gauge({ label, value, limit, pct }) {
  const cls = pct > 1 ? 'bad' : pct > 0.9 ? 'warn' : 'ok'
  return (
    <div className="gauge">
      <div className="gauge-top">
        <span className="small muted">{label}</span>
        <span className="small">
          <b>{fmt(value)}</b> / {fmt(limit)} ({(pct * 100).toFixed(1)}%)
        </span>
      </div>
      <div className="bar">
        <div className={`fill ${cls}`} style={{ width: `${Math.min(100, pct * 100)}%` }} />
      </div>
    </div>
  )
}
